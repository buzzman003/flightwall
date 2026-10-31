"""Read-only operating-flight status, association and per-frame freshness.

Times are provider observations/estimates, independent of the owner's roster.
No network calls, provider payloads, phone locations or credentials live here.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import math
import re

UTC = timezone.utc
SOURCE = "FlightAware AeroAPI"
MAX_STATUS_AGE = 300
TIME_FIELDS = tuple(kind + "_" + event for event in ("out", "off", "on", "in") for kind in ("scheduled", "estimated", "actual"))
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})")


def stamp(value):
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ValueError("A provider timestamp must include an explicit timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("A provider timestamp must include an explicit timezone")
    return result.astimezone(UTC)


def operating_ident(flight, airline=""):
    number = str(flight.get("number") or "").upper().strip()
    if not re.fullmatch(r"(?:[A-Z][A-Z0-9]|[0-9][A-Z]|[A-Z]{3})?\s*[0-9]{1,5}[A-Z]?", number):
        raise ValueError("The selected operating flight needs a supported flight number")
    number = number.replace(" ", "")
    if re.fullmatch(r"[0-9]{1,5}[A-Z]?", number):
        if not isinstance(airline, str) or not re.fullmatch(r"[A-Z][A-Z0-9]|[0-9][A-Z]|[A-Z]{3}", airline):
            raise ValueError("Set the operating airline code before enabling tracking")
        number = airline + number
    return number


def flight_identity(flight, config):
    if not isinstance(flight, dict):
        return None
    return tuple(flight.get(key) for key in ("id", "number", "origin", "destination", "start", "end", "deadhead")) + (config.get("airline"),)


def within_tracking_window(flight, now):
    try:
        start, end = stamp(flight["start"]), stamp(flight["end"])
        return (not flight.get("deadhead") and start < end <= start + timedelta(hours=48)
                and start - timedelta(hours=4) <= now.astimezone(UTC) <= end + timedelta(hours=6))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return False


def choose_tracking_flight(snapshot, config, now, current_status=None):
    """A provider-confirmed departed leg can outlast its roster arrival time."""
    if not isinstance(snapshot, dict) or snapshot.get("demo") or config.get("demo"):
        return None
    candidates = [flight for flight in snapshot.get("flights", []) if isinstance(flight, dict) and within_tracking_window(flight, now)]
    current = fresh_status(current_status, now)
    if current.get("status") in {"fresh", "stale"} and current.get("phase") in {"departed_gate", "airborne", "landed"}:
        match = next((flight for flight in candidates if flight.get("id") == current.get("flight_id")), None)
        if match:
            return copy.deepcopy(match)
    active = [flight for flight in candidates if stamp(flight["start"]) <= now < stamp(flight["end"])]
    future = [flight for flight in candidates if now < stamp(flight["start"])]
    past = [flight for flight in candidates if stamp(flight["end"]) <= now]
    selected = (min(active, key=lambda flight: stamp(flight["start"])) if active else
                min(future, key=lambda flight: stamp(flight["start"])) if future else
                max(past, key=lambda flight: stamp(flight["end"])) if past else None)
    return copy.deepcopy(selected)


def match_operating_flight(data, selected, config):
    """One canonical airline/route/departure-window match; never nearest wins."""
    if not isinstance(data, dict) or not isinstance(data.get("flights"), list) or len(data["flights"]) > 1000:
        raise ValueError("Invalid operating flight result")
    links = data.get("links") or {}
    if not isinstance(links, dict):
        raise ValueError("Invalid flight pagination")
    if links.get("next") or data.get("num_pages", 1) != 1:
        return "ambiguous", None
    ident, start = operating_ident(selected, config.get("airline", "")), stamp(selected["start"])
    matches = []
    for item in data["flights"]:
        if not isinstance(item, dict) or ident not in {item.get("ident"), item.get("ident_iata"), item.get("ident_icao")}:
            continue
        origin, destination = item.get("origin"), item.get("destination")
        if not isinstance(origin, dict) or not isinstance(destination, dict):
            continue
        if (origin.get("code_iata"), destination.get("code_iata")) != (selected.get("origin"), selected.get("destination")):
            continue
        try:
            departure = stamp(item.get("scheduled_out") or item.get("scheduled_off"))
            if abs((departure - start).total_seconds()) > 6 * 3600:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        identifier = item.get("fa_flight_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", identifier):
            continue
        matches.append(item)
    if len(matches) != 1:
        return ("ambiguous" if matches else "not_found"), None
    return "matched", matches[0]


def _clock_group(times, events):
    # Gate vs runway clocks must not be mixed. Prefer the gate's complete pair,
    # then a runway pair, then one schedule without claiming a current estimate.
    chosen = next((event for event in events if times.get("scheduled_" + event)
                   and (times.get("actual_" + event) or times.get("estimated_" + event))), None)
    chosen = chosen or next((event for event in events if times.get("actual_" + event) or times.get("estimated_" + event)), None)
    chosen = chosen or next((event for event in events if times.get("scheduled_" + event)), events[0])
    scheduled, estimated, actual = (times.get(kind + "_" + chosen) for kind in ("scheduled", "estimated", "actual"))
    current, basis = (actual, "actual") if actual else (estimated, "estimated") if estimated else (None, None)
    delta = (stamp(current) - stamp(scheduled)).total_seconds() / 60 if current and scheduled else None
    return {"event": chosen, "scheduled": scheduled, "estimated": estimated, "actual": actual,
            "time": current, "basis": basis, "delta_minutes": delta,
            "comparison": "early" if delta is not None and delta < 0 else "late" if delta is not None and delta > 0
                          else "on_time" if delta == 0 else "unknown"}


def normalize_operating_status(item, selected, now):
    """Project only verified timestamps and booleans from one matched object."""
    start = stamp(selected["start"])
    now = now.astimezone(UTC)
    times = {}
    for field in TIME_FIELDS:
        try:
            value = stamp(item.get(field))
            if abs((value - start).total_seconds()) > 72 * 3600 or field.startswith("actual_") and value > now:
                continue
            times[field] = value.isoformat()
        except (ValueError, TypeError, OverflowError):
            continue
    actual = [times.get("actual_" + event) for event in ("out", "off", "on", "in")]
    actual = [stamp(value) for value in actual if value]
    if any(a > b for a, b in zip(actual, actual[1:])):
        times = {field: value for field, value in times.items() if not field.startswith("actual_")}
    cancelled = item.get("cancelled") if isinstance(item.get("cancelled"), bool) else None
    diverted = item.get("diverted") if isinstance(item.get("diverted"), bool) else None
    blocked = item.get("blocked") is True
    phase = ("blocked" if blocked else "tracking_ended" if cancelled else "at_gate" if times.get("actual_in") else
             "landed" if times.get("actual_on") else "airborne" if times.get("actual_off") else
             "departed_gate" if times.get("actual_out") else "scheduled")
    return {"status": "fresh", "flight_id": selected["id"], "provider_flight_id": item["fa_flight_id"],
            "number": selected["number"], "origin": selected["origin"], "destination": selected["destination"],
            "source": SOURCE, "fetched_at": now.isoformat(), "times": times,
            "departure": _clock_group(times, ("out", "off")), "arrival": _clock_group(times, ("in", "on")),
            "cancelled": cancelled, "diverted": diverted, "phase": phase}


def fresh_status(status, now, backend_available=True):
    """Age a copied provider result each frame, even during pause/outage."""
    if not isinstance(status, dict):
        return {"status": "unavailable", "source": SOURCE}
    result = copy.deepcopy(status)
    if result.get("status") in {"fresh", "stale"}:
        try:
            age = (now.astimezone(UTC) - stamp(result.get("fetched_at"))).total_seconds()
            if not backend_available or not 0 <= age <= MAX_STATUS_AGE:
                result["status"] = "stale"
        except (ValueError, TypeError, AttributeError, OverflowError):
            result["status"] = "stale"
    return result


def alert_card(status, now):
    result = fresh_status(status, now)
    if result.get("status") != "fresh" or result.get("phase") == "blocked":
        return None
    if result.get("diverted") is True:
        kind = "diverted"
    elif result.get("cancelled") is True:
        kind = "tracking_ended"
    elif any(isinstance(result.get(key), dict) and type(result[key].get("delta_minutes")) in (int, float)
             and math.isfinite(result[key]["delta_minutes"]) and abs(result[key]["delta_minutes"]) >= 1
             for key in ("departure", "arrival")):
        kind = "timing_change"
    else:
        return None
    return {**result, "id": "operating-alert-" + str(result.get("flight_id", "")), "kind": kind}
