"""Owner-selected ground-time planning, independent of Buzz note assertions."""
from datetime import datetime, timedelta, timezone
import re

from .time_format import format_leave_clock, format_time


SOURCE = "OWNER PLAN"
_GROUND = {"drive": 25, "parking": 6, "security": 5, "gate": 2}


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant.astimezone(timezone.utc) if instant.tzinfo is not None and instant.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def owner_leave_plan(flight, config, now):
    """Subtract opted-in ground minutes from exact outbound schedule clocks.

    No arrival, phone observation, calendar prose, or inferred report is needed.
    A separately supplied roster report never moves this owner-selected window.
    """
    if (config.get("vps_leave_plan_enabled") is not True or config.get("demo") is True
            or not isinstance(flight, dict) or flight.get("origin") != "VPS"
            or not isinstance(flight.get("destination"), str)
            or not re.fullmatch(r"[A-Z]{3}", flight["destination"]) or flight["destination"] == "VPS"
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        return None
    departure, arrival = _stamp(flight.get("start")), _stamp(flight.get("end"))
    if departure is None or arrival is None or not now < departure < arrival:
        return None
    ground = [config.get(f"vps_leave_{part}_minutes", default) for part, default in _GROUND.items()]
    early = config.get("vps_leave_target_early_minutes", 60)
    late = config.get("vps_leave_target_late_minutes", 50)
    if (any(type(value) is not int or not 0 <= value <= 240 for value in ground)
            or type(early) is not int or type(late) is not int or not 0 < late < early <= 240):
        return None
    total = sum(ground)
    result = {"source": SOURCE, "departure_at": departure.isoformat(),
              "window_start_at": (departure - timedelta(minutes=early + total)).isoformat(),
              "window_end_at": (departure - timedelta(minutes=late + total)).isoformat()}
    report = _stamp(flight.get("report"))
    if report and departure - timedelta(days=1) <= report <= departure:
        result["report_at"] = report.isoformat()
    return result


def format_owner_plan(plan, zone, *, short=False):
    """Preserve dates/DST labels even when a planning window crosses midnight."""
    if not isinstance(plan, dict) or plan.get("source") != SOURCE:
        return None
    start, end, departure = (_stamp(plan.get(key)) for key in ("window_start_at", "window_end_at", "departure_at"))
    if start is None or end is None or departure is None or not start < end < departure:
        return None
    first, last, leaves = start.astimezone(zone), end.astimezone(zone), departure.astimezone(zone)
    if short and first.date() == last.date() == leaves.date() and first.tzname() == last.tzname():
        left = format_time(first)
        if first.hour // 12 == last.hour // 12:
            left = left.rsplit(" ", 1)[0]
        return left + "-" + format_time(last, include_zone=True)
    return format_leave_clock(None, start, end, zone) or (
        format_time(first, date_format="%a %m/%d", include_zone=True) + "-"
        + format_time(last, date_format="%a %m/%d", include_zone=True))
