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
NEARBY_ROUTE_MAX_AGE = 600
TIME_FIELDS = tuple(kind + "_" + event for event in ("out", "off", "on", "in") for kind in ("scheduled", "estimated", "actual"))
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})")
_NEARBY_AIRPORT = re.compile(r"(?=[A-Z0-9]*[A-Z])[A-Z0-9]{3,4}")
_US_REGISTRATION = re.compile(r"N[1-9](?:[0-9]{0,4}|[0-9]{0,3}[A-HJ-NP-Z]|[0-9]{0,2}[A-HJ-NP-Z]{2})")


def _nearby_airport(airport):
    for key, pattern in (("code_iata", r"[A-Z]{3}"), ("code_icao", r"[A-Z]{4}"), ("code_lid", _NEARBY_AIRPORT)):
        code = airport.get(key)
        if isinstance(code, str) and re.fullmatch(pattern, code):
            return code
    return None


def valid_us_registration(value):
    return isinstance(value, str) and _US_REGISTRATION.fullmatch(value) is not None


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


def confirmed_gate_arrival(status, flight, now):
    """An exact final gate clock remains factual after live estimates age out."""
    if (not isinstance(status, dict) or not isinstance(flight, dict)
            or status.get("source") != SOURCE or status.get("status") not in {"fresh", "stale"}
            or status.get("phase") != "at_gate" or status.get("cancelled") is True or status.get("diverted") is True
            or not status.get("provider_flight_id") or not flight.get("id") or not within_tracking_window(flight, now)
            or status.get("flight_id") != flight.get("id")
            or any(status.get(key) != flight.get(key) for key in ("number", "origin", "destination"))):
        return None
    try:
        arrival = stamp(status["times"]["actual_in"])
        observed = stamp(status["fetched_at"])
        if (arrival <= observed <= now and abs(arrival - stamp(flight["start"])) <= timedelta(hours=72)):
            return arrival, observed
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


def provider_timeline(status, now):
    """Use only observed OOOI clocks, without inferring surface movement."""
    current = fresh_status(status, now)
    if current.get("status") != "fresh" or current.get("source") != SOURCE:
        return None
    observed = stamp(current["fetched_at"])
    times = current.get("times")
    times = times if isinstance(times, dict) else {}
    actual = {}
    for key, event in (("out", "gate_out"), ("off", "takeoff"), ("on", "landing"), ("in", "gate_in")):
        try:
            when = stamp(times.get("actual_" + key))
        except (ValueError, TypeError, OverflowError):
            continue
        if when <= observed:
            actual[event] = when
    ordered = list(actual.values())
    if any(first > second for first, second in zip(ordered, ordered[1:])):
        actual.clear()
    phase = "scheduled"
    for event, value in (("gate_out", "taxi_out"), ("takeoff", "airborne"),
                         ("landing", "taxi_in"), ("gate_in", "at_gate")):
        if event in actual:
            phase = value
    blocked = current.get("phase") == "blocked"
    diverted = current.get("diverted") is True
    ended = current.get("cancelled") is True or current.get("phase") == "tracking_ended"
    if blocked or diverted or ended:
        # The service need not confirm a diversion airport. Do not attach
        # observed landing/gate clocks to the original destination in that case.
        phase = "blocked" if blocked else "diverted" if diverted else "tracking_ended"
        actual.clear()
    result = {"phase": phase, "fetched_at": observed.isoformat(), "diverted": diverted,
              "tracking_ended": ended, "events": {key: value.isoformat() for key, value in actual.items()}}
    for key, first, second in (("taxi_out_minutes", "gate_out", "takeoff"),
                               ("taxi_in_minutes", "landing", "gate_in")):
        if first in actual and second in actual:
            result[key] = (actual[second] - actual[first]).total_seconds() / 60
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


def nearby_commercial_ident(aircraft):
    value = aircraft.get("callsign") if isinstance(aircraft, dict) else None
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    # ADS-B airline callsigns normally carry a three-letter ICAO operator.
    # Do not interpret an N-registration or a hex address as a flight number.
    if value in (str(aircraft.get("hex", "")).upper(), str(aircraft.get("registration", "")).upper()):
        return None
    return value if re.fullmatch(r"[A-Z]{3}[0-9]{1,5}[A-Z]?", value) else None


def nearby_lookup_identity(aircraft):
    designator = nearby_commercial_ident(aircraft)
    if designator:
        return "designator", designator
    registration = aircraft.get("registration") if isinstance(aircraft, dict) else None
    if valid_us_registration(registration):
        return "registration", registration
    return None


def match_nearby_route(data, aircraft, now):
    """One actively airborne provider flight matching the observed identity.

    This buys metadata only, never positions, telemetry, tracks or filed route.
    Unsupported, ambiguous, landed and mismatched registrations stay unknown.
    """
    identity = nearby_lookup_identity(aircraft)
    if not identity or not isinstance(data, dict) or not isinstance(data.get("flights"), list) or len(data["flights"]) > 1000:
        return None
    ident_type, ident = identity
    if not isinstance(aircraft.get("hex"), str) or not re.fullmatch(r"~?[0-9a-fA-F]{6}", aircraft["hex"]):
        return None
    try:
        observed = stamp(aircraft.get("observed_at"))
        if not 0 <= (now - observed).total_seconds() <= 120:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    links, pages = data.get("links"), data.get("num_pages", 1)
    links = {} if links is None else links
    if not isinstance(links, dict) or links.get("next") or type(pages) is not int or pages != 1:
        return None
    matches = []
    for item in data["flights"]:
        if not isinstance(item, dict):
            continue
        if ident_type == "registration":
            # Require the provider's explicit tail field, even if its flight
            # ident happens to look like this registration.
            if item.get("registration") != ident:
                continue
        elif not any(ident == item.get(key) for key in ("ident", "ident_icao", "atc_ident")):
            continue
        if any(item.get(key) is True for key in ("blocked", "cancelled", "diverted", "position_only")):
            continue
        if item.get("actual_on") is not None or item.get("actual_in") is not None:
            continue
        try:
            takeoff = stamp(item.get("actual_off"))
            if not observed - timedelta(hours=24) <= takeoff <= observed:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        identifier = item.get("fa_flight_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", identifier):
            continue
        origin, destination = item.get("origin"), item.get("destination")
        if not isinstance(origin, dict) or not isinstance(destination, dict):
            continue
        origin, destination = _nearby_airport(origin), _nearby_airport(destination)
        if not origin or not destination or origin == destination:
            continue
        provider_tail, observed_tail = item.get("registration"), aircraft.get("registration")
        if provider_tail is not None and (not isinstance(provider_tail, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,15}", provider_tail)):
            continue
        if provider_tail and observed_tail and provider_tail != observed_tail:
            continue
        matches.append({"origin": origin, "destination": destination, "source": SOURCE,
                        "fetched_at": now.astimezone(UTC).isoformat(), "provider_flight_id": identifier,
                        "callsign": ident if ident_type == "designator" else None, "registration": observed_tail or None,
                        "aircraft_hex": aircraft.get("hex"), "actual_off": takeoff.isoformat()})
    return matches[0] if len(matches) == 1 else None


def fresh_nearby_route(aircraft, now):
    """Per-frame metadata guard; source position and lookup age are separate."""
    route = aircraft.get("route") if isinstance(aircraft, dict) else None
    identity = nearby_lookup_identity(aircraft)
    if not isinstance(route, dict) or not identity:
        return None
    ident_type, ident = identity
    if route.get("callsign") != (ident if ident_type == "designator" else None):
        return None
    if not isinstance(aircraft.get("hex"), str) or not re.fullmatch(r"~?[0-9a-fA-F]{6}", aircraft["hex"]):
        return None
    if route.get("aircraft_hex") != aircraft.get("hex") or route.get("registration") != (aircraft.get("registration") or None):
        return None
    try:
        observed, fetched, takeoff = (stamp(value) for value in (aircraft.get("observed_at"), route.get("fetched_at"), route.get("actual_off")))
        if (not 0 <= (now - observed).total_seconds() <= 120
                or not 0 <= (now - fetched).total_seconds() <= NEARBY_ROUTE_MAX_AGE
                or not observed - timedelta(hours=24) <= takeoff <= observed):
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    if (route.get("source") != SOURCE
            or not isinstance(route.get("provider_flight_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", route["provider_flight_id"])
            or not all(isinstance(route.get(key), str) and _NEARBY_AIRPORT.fullmatch(route[key]) for key in ("origin", "destination"))
            or route["origin"] == route["destination"]):
        return None
    fields = {"origin", "destination", "source", "fetched_at", "provider_flight_id", "callsign", "registration", "aircraft_hex", "actual_off"}
    return {key: copy.deepcopy(route[key]) for key in fields if key in route}
