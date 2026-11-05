"""Bounded, source-separated context for one operating leg and its household brief."""
from datetime import timedelta
import re

from .flight_alerts import operating_ident, provider_timeline, stamp, within_tracking_window
from .flight_environment import FETCH_MAX_AGE, SOURCE_MAX_AGE
from .outlook import local_visits
from .trip_weather import CACHE_MAX_AGE, METAR_MAX_AGE


def _timestamp(value):
    try:
        return stamp(value)
    except (ValueError, TypeError, OverflowError):
        return None


def _fresh(value, now, seconds):
    at = _timestamp(value)
    return at is not None and 0 <= (now - at).total_seconds() < seconds


def _comparison(data, events, actual):
    if not isinstance(data, dict):
        return None
    event, basis = data.get("event"), data.get("basis")
    if not isinstance(event, str) or event not in events or basis not in ("actual", "estimated"):
        return None
    current, scheduled = _timestamp(data.get("time")), _timestamp(data.get("scheduled"))
    if not current or current != _timestamp(data.get(basis)):
        return None
    if basis == "actual" and current != _timestamp(actual.get(events[event])):
        return None
    result = {"event": event, "basis": basis, "at": current.isoformat()}
    if scheduled:
        result.update(scheduled_at=scheduled.isoformat(), delta_minutes=(current - scheduled).total_seconds() / 60)
    return result


def _airport_advisories(status, origin, destination, phase, now, scheduled_end):
    section = status.get("airport_alerts")
    if (not isinstance(section, dict) or section.get("status") != "ready"
            or section.get("source") != "FAA airport status"
            or not _fresh(section.get("fetched_at"), now, FETCH_MAX_AGE["airport_alerts"])
            or not _fresh(section.get("issued_at"), now, SOURCE_MAX_AGE["airport_alerts"])):
        return []
    before_takeoff = phase in {"schedule_only", "scheduled", "taxi_out"}
    if phase == "schedule_only" and now >= scheduled_end:
        return []
    if not before_takeoff and phase != "airborne":
        return []
    allowed = {(destination, "arrival_delay")}
    if before_takeoff:
        allowed.update({(destination, "ground_stop"), (destination, "ground_delay"), (origin, "departure_delay")})
    events = section.get("events")
    found = {(row["airport"], row["kind"]) for row in events if isinstance(row, dict)
             and isinstance(row.get("airport"), str) and isinstance(row.get("kind"), str)
             and (row["airport"], row["kind"]) in allowed} if isinstance(events, list) else set()
    return [{"airport": airport, "kind": kind, "issued_at": section["issued_at"]} for airport, kind in sorted(found)]


def _airport_weather(status, origin, destination, now):
    section = status.get("trip_weather")
    airports = section.get("airports") if isinstance(section, dict) else None
    result = []
    for row in airports if isinstance(airports, list) else []:
        if not isinstance(row, dict) or row.get("iata") not in (origin, destination):
            continue
        station, metar = row.get("icao"), row.get("metar")
        if (not isinstance(station, str) or not re.fullmatch(r"[A-Z0-9]{4}", station)
                or not isinstance(metar, dict) or metar.get("icao") != station
                or metar.get("source") != "AviationWeather.gov" or metar.get("status") != "ready"
                or metar.get("stale") is not False or metar.get("flight_category") not in ("VFR", "MVFR", "IFR", "LIFR")
                or not _fresh(metar.get("checked_at"), now, CACHE_MAX_AGE)
                or not _fresh(metar.get("observed_at"), now, METAR_MAX_AGE)):
            continue
        result.append({"airport": row["iata"], "flight_category": metar["flight_category"],
                       "observed_at": metar["observed_at"]})
    return result[:2]


def _vps_visit(snapshot, config, flight, start, end, now):
    if config.get("local_visits_enabled") is not True or "VPS" not in (flight["origin"], flight["destination"]):
        return None
    legs = [row for row in snapshot["flights"] if isinstance(row, dict) and not row.get("deadhead")]
    matched = []
    for row in local_visits(snapshot, now, include_future=True):
        if row["airport"] != "VPS":
            continue
        arrival, departure = _timestamp(row.get("arrival")), _timestamp(row.get("departure"))
        if (not arrival or not departure or not arrival < departure <= arrival + timedelta(days=4) or now >= departure
                or not (flight.get("destination") == "VPS" and arrival == end
                        or flight.get("origin") == "VPS" and departure == start)):
            continue
        inbound = [leg for leg in legs if leg.get("destination") == "VPS" and _timestamp(leg.get("end")) == arrival]
        outbound = [leg for leg in legs if leg.get("origin") == "VPS" and _timestamp(leg.get("start")) == departure]
        if len(inbound) != 1 or len(outbound) != 1:
            continue
        result = {"scheduled_arrival_at": arrival.isoformat(), "scheduled_departure_at": departure.isoformat()}
        report = _timestamp(outbound[0].get("report"))
        if report and max(arrival, departure - timedelta(days=1)) < report <= departure:
            result["scheduled_report_at"] = report.isoformat()
        if config.get("brief_leave_times_enabled") is True:
            leave = _timestamp(row.get("leave_at"))
            if leave and max(arrival, departure - timedelta(days=1)) < leave <= departure:
                result["brief_leave_at"] = leave.isoformat()
            if row.get("leave_conflict") is True:
                result["brief_leave_conflict"] = True
        matched.append(result)
    return matched[0] if len(matched) == 1 else None


def build_flight_context(status, config, now):
    """Project current facts, keeping the roster and provider clocks independent."""
    snapshot = status.get("snapshot")
    if (not isinstance(snapshot, dict) or snapshot.get("demo") is not False or config.get("demo") is not False
            or status.get("backend_available", True) is not True):
        return None
    flight = status.get("operating_flight")
    flights = snapshot.get("flights")
    if (not isinstance(flight, dict) or not within_tracking_window(flight, now)
            or not isinstance(flights, list) or flight not in flights):
        return None
    origin, destination = flight.get("origin"), flight.get("destination")
    if any(not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code) for code in (origin, destination)) or origin == destination:
        return None
    try:
        number = operating_ident(flight, config.get("airline", ""))
    except ValueError:
        return None
    start, end = stamp(flight["start"]), stamp(flight["end"])
    schedule = {"departure_at": start.isoformat(), "arrival_at": end.isoformat()}
    report = _timestamp(flight.get("report"))
    if report and start - timedelta(days=1) <= report <= start:
        schedule["report_at"] = report.isoformat()

    status_row = status.get("operating_status")
    provider = None
    if (config.get("flightaware_enabled") is True and isinstance(status_row, dict) and flight.get("id")
            and status_row.get("provider_flight_id")
            and all(status_row.get(key) == flight.get(key) for key in ("origin", "destination", "number"))
            and status_row.get("flight_id") == flight["id"]):
        provider = provider_timeline(status_row, now)
    phase = provider.pop("phase") if provider else "schedule_only"
    if provider and phase not in {"blocked", "diverted", "tracking_ended"}:
        for key, events in (("departure", {"out": "gate_out", "off": "takeoff"}),
                            ("arrival", {"on": "landing", "in": "gate_in"})):
            comparison = _comparison(status_row.get(key), events, provider["events"])
            if comparison:
                provider[key] = comparison

    return {"flight_number": number, "origin": origin, "destination": destination, "phase": phase,
            "schedule": schedule, "provider": provider,
            "airport_advisories": _airport_advisories(status, origin, destination, phase, now, end)
            if config.get("airport_alerts_enabled") is True else [],
            "weather": _airport_weather(status, origin, destination, now) if config.get("trip_weather_enabled") is True else [],
            "vps_visit": _vps_visit(snapshot, config, flight, start, end, now)}
