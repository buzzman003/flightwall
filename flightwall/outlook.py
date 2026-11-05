"""Calendar-derived outlooks; schedules never establish physical presence."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib

UTC = timezone.utc
ACTIVITIES_PER_PAGE = 6


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Missing timezone")
    return result.astimezone(UTC)


def calendar_fresh(last_sync, now, minutes, *, failed=False):
    try:
        return not failed and -300 <= (now - stamp(last_sync)).total_seconds() <= minutes * 60
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


def today_activities(outlook, config, now, *, backend_available=True):
    """Return today's private agenda, or None when it cannot be shown."""
    if (not backend_available or config.get("guest_mode") or config.get("demo")
            or config.get("kids_filter_enabled", True) is not True
            or not isinstance(outlook, dict) or outlook.get("stale") is not False):
        return None
    minutes = config.get("kids_stale_minutes", 30)
    if not (calendar_fresh(outlook.get("last_sync"), now, minutes)
            and calendar_fresh(outlook.get("activity_last_sync"), now, minutes)):
        return None
    source = outlook.get("activities")
    if not isinstance(source, list):
        return None
    zone = ZoneInfo(config.get("timezone") or outlook.get("timezone") or "UTC")
    day = now.astimezone(zone).date()
    start_of_day = datetime.combine(day, time(), zone).astimezone(UTC)
    end_of_day = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
    rows = []
    for item in source[:48]:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
            continue
        try:
            start, end = stamp(item.get("start")), stamp(item.get("end"))
        except (ValueError, TypeError, AttributeError, OverflowError):
            continue
        if start < end and start < end_of_day and end > start_of_day:
            rows.append((start.astimezone(zone), end.astimezone(zone), item["title"], item.get("all_day") is True))
    return sorted(rows, key=lambda row: (row[0].astimezone(UTC), row[1].astimezone(UTC), row[2]))


def agenda_focus(rows, now):
    """Mark active and next timed rows without changing the calendar order."""
    now = now.astimezone(UTC)
    timed = [(index, start.astimezone(UTC), end.astimezone(UTC))
             for index, (start, end, _, all_day) in enumerate(rows) if not all_day]
    next_start = min((start for _, start, _ in timed if start > now), default=None)
    labels = [""] * len(rows)
    for index, start, end in timed:
        if start <= now < end:
            labels[index] = "NOW"
        elif start == next_start:
            labels[index] = "NEXT"
    return labels


def custody_outlook(intervals, last_sync, config, now, *, failed=False):
    zone = ZoneInfo(config.get("timezone", "UTC"))
    fresh = calendar_fresh(last_sync, now, config.get("kids_stale_minutes", 30), failed=failed)
    spans = []
    if fresh:
        for item in intervals:
            try:
                start, end = stamp(item["start"]), stamp(item["end"])
                parent = item["parent"]
                if end > start and parent in {"Mom", "Dad"}:
                    spans.append((start, end, "home" if parent == "Mom" else "away"))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
    rows = []
    for offset in range(config.get("custody_outlook_days", 10)):
        day = now.astimezone(zone).date() + timedelta(days=offset)
        start = datetime.combine(day, time(), zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
        points = sorted({start, end} | {max(start, min(end, point)) for a, b, _ in spans for point in (a, b)})
        segments = []
        for a, b in zip(points, points[1:]):
            states = {state for s, e, state in spans if s <= a < e}
            state = next(iter(states)) if len(states) == 1 else "unknown"
            if segments and segments[-1]["state"] == state:
                segments[-1]["end"] = b.isoformat()
            else:
                segments.append({"state": state, "start": a.isoformat(), "end": b.isoformat()})
        states = {item["state"] for item in segments}
        # Partial coverage or a conflict is explicitly unknown, never guessed.
        state = "unknown" if "unknown" in states else next(iter(states)) if len(states) == 1 else "mixed"
        rows.append({"date": day.isoformat(), "state": state, "segments": segments})
    return {"days": rows, "stale": not fresh, "last_sync": last_sync,
            "timezone": str(zone), "error": "Calendar unavailable or stale" if not fresh else None}


def local_visits(snapshot, now, *, include_future=False):
    """Highlight today's local arrivals, including an overnight VPS stay.

    Match only the very next non-deadhead operating leg. A later flight from
    the same airport does not establish an uninterrupted layover.
    """
    if snapshot.get("demo"):
        return []
    zone = ZoneInfo("America/Chicago")
    legs = []
    for flight in snapshot.get("flights", []):
        try:
            start, end = stamp(flight["start"]), stamp(flight["end"])
            if start < end:
                legs.append((start, end, flight))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    legs.sort(key=lambda row: (row[0], row[1]))
    result = []
    for index, (_, arrival, flight) in enumerate(legs):
        airport = flight.get("destination")
        if airport not in {"VPS", "PNS", "ECP"} or flight.get("deadhead"):
            continue
        local_day = arrival.astimezone(zone).date()
        display_start = datetime.combine(local_day, time(), zone).astimezone(UTC)
        display_end = datetime.combine(local_day + timedelta(days=1), time(), zone).astimezone(UTC)
        item = {"id": hashlib.sha256((str(flight.get("id")) + arrival.isoformat()).encode()).hexdigest()[:20],
                "airport": airport, "arrival": arrival.isoformat(), "timezone": str(zone),
                "source": "Calendar schedule"}
        if airport == "VPS" and index + 1 < len(legs):
            departure, _, outbound = legs[index + 1]
            if not outbound.get("deadhead") and outbound.get("origin") == airport and arrival < departure <= arrival + timedelta(days=4):
                item.update(departure=departure.isoformat(), duration_minutes=int((departure - arrival).total_seconds() / 60))
                try:
                    leave = stamp(outbound.get("leave_at"))
                    if max(arrival, departure - timedelta(days=1)) < leave <= departure:
                        item["leave_at"] = leave.isoformat()
                except (ValueError, TypeError, AttributeError, OverflowError):
                    pass
                if outbound.get("leave_conflict") is True:
                    item["leave_conflict"] = True
                display_end = max(display_end, departure)
        item.update(display_start=display_start.isoformat(), display_end=display_end.isoformat())
        if display_start <= now < display_end or include_future and now < display_end:
            result.append(item)
    return result if include_future else result[:6]


def planned_buzz_days(snapshot, config, now):
    """Project scheduled duty and matched VPS stays into the seven-day family card.

    An arrival clock is a VPS arrival. Only a verified leave clock supplies
    a home departure time. An uncovered roster interval has no planned state.
    """
    if (snapshot.get("demo") or not calendar_fresh(snapshot.get("last_sync"), now,
            config.get("kids_stale_minutes", 30), failed=bool(snapshot.get("error")))):
        return {}
    zone = ZoneInfo(config.get("timezone", "UTC"))
    today = now.astimezone(zone).date()
    midnight = datetime.combine(today, time(), zone).astimezone(UTC)
    include_leaves = config.get("brief_leave_times_enabled") is True
    chosen_leave_policy = config.get("brief_leave_policy") in {"recommended", "report_safe"}
    stays, leave_by_departure = [], {}
    for visit in local_visits(snapshot, midnight, include_future=True):
        if visit["airport"] != "VPS" or "departure" not in visit:
            continue
        arrival, departure = stamp(visit["arrival"]), stamp(visit["departure"])
        conflict = include_leaves and visit.get("leave_conflict") is True
        leave = stamp(visit["leave_at"]) if include_leaves and "leave_at" in visit and (not conflict or chosen_leave_policy) else None
        stays.append((arrival, leave or departure, leave, conflict))
        if leave:
            leave_by_departure[departure] = leave

    flights = []
    for row in snapshot.get("flights", []):
        try:
            start, end = stamp(row["start"]), stamp(row["end"])
            if start < end:
                flights.append((start, end, row))
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            continue
    flights.sort(key=lambda leg: (leg[0], leg[1]))
    duty = []
    for index, (departure, arrival, row) in enumerate(flights):
        if row.get("deadhead") and not config.get("show_deadheads", True):
            continue
        start, basis = departure, "flight"
        previous = flights[index - 1] if index else None
        if row.get("origin") == "VPS" and departure in leave_by_departure:
            start, basis = leave_by_departure[departure], "leave"
        elif (previous and previous[2].get("destination") == row.get("origin") != "VPS"
                and departure - timedelta(days=4) <= previous[1] <= departure
                and (config.get("show_deadheads", True) or not previous[2].get("deadhead"))):
            start, basis = previous[1], "connection"
        else:
            try:
                report = stamp(row.get("report"))
                if (departure - timedelta(days=1) <= report <= departure
                        and (previous is None or previous[1] <= report)):
                    start, basis = report, "report"
            except (ValueError, TypeError, AttributeError, OverflowError):
                pass
        duty.append((start, arrival, basis))

    days = {}
    for offset in range(7):
        day = today + timedelta(days=offset)
        start = datetime.combine(day, time(), zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
        home = next((item for item in stays if item[0] < end and start < item[1]), None)
        away = [item for item in duty if item[0] < end and start < item[1]]
        if not home and not away:
            continue
        row = {"home": bool(home), "away": bool(away)}
        if home:
            arrival, _, leave, conflict = home
            if start <= arrival < end:
                row["arrival"] = arrival.isoformat()
            if leave and start <= leave < end:
                row["leave"] = leave.isoformat()
            if conflict and (leave is None or start <= leave < end):
                row["leave_conflict"] = True
            elif leave is None:
                row["leave_unknown"] = True
        if away:
            first, _, basis = min(away, key=lambda item: item[0])
            if start <= first < end and basis != "connection":
                row.update(away_start=first.isoformat(), away_basis=basis)
        days[day.isoformat()] = row
    return days


def trip_preview(snapshot, config, now):
    """The next five calendar days with flights, starting at the next leg.

    A date range is deliberate: individual flight titles don't establish a
    roster rotation boundary. The display must not invent one from gaps.
    """
    if snapshot.get("demo"):
        return {}
    zone = ZoneInfo(config.get("timezone", "UTC"))
    flights = []
    for flight in snapshot.get("flights", []):
        try:
            start, end = stamp(flight["start"]), stamp(flight["end"])
            if end > start and end > now and (config.get("show_deadheads", True) or not flight.get("deadhead")):
                flights.append((start, end, flight))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    flights.sort(key=lambda row: (row[0], row[1]))
    if not flights or flights[0][0] > now + timedelta(days=config.get("flight_preview_days", 5)):
        return {}
    first_day = flights[0][0].astimezone(zone).date()
    end_day = first_day + timedelta(days=5)
    days, selected = {}, []
    for start, _, flight in flights:
        day = start.astimezone(zone).date()
        if day >= end_day:
            break
        legs = days.setdefault(day.isoformat(), [])
        item = {**flight, "leg_number": len(legs) + 1}
        legs.append(item)
        selected.append(item)
    return {"start_date": first_day.isoformat(), "end_date": max(days), "timezone": str(zone),
            "days": [{"date": day, "legs": legs} for day, legs in days.items()],
            "flights": selected, "local_visits": [item for item in local_visits(snapshot, now, include_future=True)
                if first_day <= stamp(item["arrival"]).astimezone(zone).date() < end_day],
            "last_sync": snapshot.get("last_sync"), "error": snapshot.get("error"),
            "source": "Calendar schedule"}
