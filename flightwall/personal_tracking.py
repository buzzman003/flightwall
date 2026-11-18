"""Opt-in Lisa flight observations, separate from duty and phone presence.

Only narrowed personal-travel records enter this module. A matching aircraft
never proves that a listed or confirmed traveler boarded it.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import urllib.error
import urllib.parse

from .flight_alerts import SOURCE, fresh_status, normalize_operating_status, stamp
from .flight_watches import tracking_available
from .mycrew import airport_zone
from .personal_travel import MAX_RECORDS, _reservation


UTC = timezone.utc
MAX_TARGETS = 5
LIVE_MAX_AGE_SECONDS = 300
_RESERVATION_STATUSES = {"listed", "confirmed"}
_TARGET_FIELDS = {"id", "number", "origin", "destination", "start", "end", "deadhead",
                  "traveler", "reservation_status", "source"}
_PROVIDER_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


def _enabled(config):
    return (isinstance(config, dict) and config.get("demo", True) is False
            and config.get("guest_mode", False) is False
            and config.get("personal_travel_cards_visible", True) is True
            and config.get("flightaware_enabled", False) is True
            and config.get("lisa_flight_tracking_enabled", False) is True)


def _target_id(flight):
    identity = ["lisa", *(flight[key] for key in ("number", "origin", "destination", "start", "end"))]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]


def personal_tracking_targets(records):
    """Reject full messages/unknown formats; project only exact Lisa legs."""
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        return []
    targets, conflicts = {}, set()
    for record in records:
        try:
            row = _reservation(record)
            if row.get("traveler", "chris") != "lisa" or row["status"] not in _RESERVATION_STATUSES:
                continue
            target = {key: row[key] for key in ("number", "origin", "destination", "start", "end")}
            target.update(id=_target_id(target), traveler="lisa", reservation_status=row["status"],
                          source="personal_travel", deadhead=False)
            old = targets.get(target["id"])
            if old is not None and old != target:
                conflicts.add(target["id"])
            targets[target["id"]] = target
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return sorted((target for key, target in targets.items() if key not in conflicts),
                  key=lambda target: (stamp(target["start"]), target["id"]))


def _safe_target(value):
    if not isinstance(value, dict) or set(value) != _TARGET_FIELDS:
        return None
    try:
        record = {key: value[key] for key in ("number", "origin", "destination", "start", "end", "traveler")}
        record.update(status=value["reservation_status"], source="delta_email", updated_at=value["start"])
        result = personal_tracking_targets([record])
        return result[0] if result and result[0] == value else None
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def personal_tracking_window(flight, now, provider=None):
    """One hour around the schedule; observed delays have a two-hour hard cap."""
    try:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return False
        start, end = stamp(flight["start"]), stamp(flight["end"])
        last = end + timedelta(hours=1)
        current = fresh_status(provider, now)
        observed = stamp(current["fetched_at"]) if current.get("fetched_at") else None
        if (current.get("status") in {"fresh", "stale"} and current.get("source") == SOURCE
                and current.get("flight_id") == flight.get("id")
                and all(current.get(key) == flight.get(key) for key in ("number", "origin", "destination"))
                and observed is not None and start - timedelta(hours=1) <= observed <= now
                and observed <= end + timedelta(hours=2)):
            # A previously observed estimate can keep the bounded request window
            # open after its freshness expires. Its status/position still age out
            # independently; this is eligibility to refresh, not a live claim.
            for field in ("estimated_in", "estimated_on"):
                value = current.get("times", {}).get(field)
                if value:
                    estimated = stamp(value)
                    if end < estimated <= end + timedelta(hours=24):
                        last = max(last, min(estimated + timedelta(hours=1), end + timedelta(hours=2)))
        return start - timedelta(hours=1) <= now.astimezone(UTC) <= last
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def match_personal_flight(data, flight):
    """Number, both airports and exact scheduled departure must all agree."""
    if not isinstance(data, dict) or not isinstance(data.get("flights"), list) or len(data["flights"]) > 1000:
        raise ValueError("Invalid personal flight response")
    links = data.get("links") or {}
    if not isinstance(links, dict):
        raise ValueError("Invalid personal flight pagination")
    if links.get("next") or type(data.get("num_pages", 1)) is not int or data.get("num_pages", 1) != 1:
        return "ambiguous", None
    matches = []
    for item in data["flights"]:
        if not isinstance(item, dict) or flight["number"] not in (
                item.get("ident"), item.get("ident_iata"), item.get("ident_icao")):
            continue
        origin, destination = item.get("origin"), item.get("destination")
        if (not isinstance(origin, dict) or not isinstance(destination, dict)
                or (origin.get("code_iata"), destination.get("code_iata")) != (flight["origin"], flight["destination"])):
            continue
        try:
            if stamp(item.get("scheduled_out") or item.get("scheduled_off")) != stamp(flight["start"]):
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        if not isinstance(item.get("fa_flight_id"), str) or not _PROVIDER_ID.fullmatch(item["fa_flight_id"]):
            continue
        matches.append(item)
    if len(matches) != 1:
        return "ambiguous" if matches else "not_found", None
    return "matched", matches[0]


def _live_position(data, flight, provider, now):
    if (not isinstance(data, dict) or not isinstance(data.get("last_position"), dict)
            or data.get("fa_flight_id", provider["provider_flight_id"]) != provider["provider_flight_id"]):
        return None
    point = data["last_position"]
    try:
        observed = stamp(point.get("timestamp"))
        if not 0 <= (now - observed).total_seconds() <= LIVE_MAX_AGE_SECONDS:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    def number(value, low, high):
        return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None
    lat, lon = number(point.get("latitude"), -90, 90), number(point.get("longitude"), -180, 180)
    if lat is None or lon is None:
        return None
    altitude = number(point.get("altitude"), -20, 1000)
    return {"latitude": lat, "longitude": lon, "observed_at": observed.isoformat(),
            "flight_id": flight["id"], "provider_flight_id": provider["provider_flight_id"],
            "altitude": altitude * 100 if altitude is not None else None, "altitude_units": "feet",
            "groundspeed": number(point.get("groundspeed"), 0, 2000),
            "heading": number(point.get("heading"), 0, 360), "source": SOURCE}


def fresh_personal_tracking(rows, config, now, backend_available=True):
    """Recheck privacy, identity, window and observation age on every frame."""
    if not backend_available or not _enabled(config) or not isinstance(rows, list) or len(rows) > MAX_TARGETS:
        return []
    result, seen = [], set()
    for value in rows:
        if not isinstance(value, dict):
            continue
        flight = _safe_target(value.get("flight"))
        if (not flight or value.get("id") != flight["id"] or flight["id"] in seen
                or value.get("traveler") != "lisa" or value.get("reservation_status") != flight["reservation_status"]):
            continue
        raw_provider = value.get("provider")
        provider = fresh_status(raw_provider, now) if isinstance(raw_provider, dict) else {}
        if provider.get("status") in {"fresh", "stale"} and (
                provider.get("source") != SOURCE or provider.get("flight_id") != flight["id"]
                or any(provider.get(key) != flight[key] for key in ("number", "origin", "destination"))
                or not isinstance(provider.get("provider_flight_id"), str)
                or not _PROVIDER_ID.fullmatch(provider["provider_flight_id"])):
            provider = {}
        if not personal_tracking_window(flight, now, provider):
            continue
        row = {"id": flight["id"], "traveler": "lisa", "reservation_status": flight["reservation_status"],
               "ident": flight["number"], "date": stamp(flight["start"]).astimezone(airport_zone(flight["origin"])).date().isoformat(),
               "flight": flight, "provider": provider,
               "status": value.get("status") if value.get("status") in {
                   "pending", "unconfigured", "not_found", "ambiguous", "scheduled", "tracking", "stale", "unavailable"} else "unavailable"}
        live = value.get("live")
        if (isinstance(live, dict) and provider.get("status") == "fresh"
                and provider.get("phase") not in {"landed", "at_gate", "blocked", "tracking_ended"}
                and live.get("source") == SOURCE
                and live.get("flight_id") == flight["id"]
                and live.get("provider_flight_id") == provider.get("provider_flight_id")):
            # Reuse the same strict measurement validation for cached rows.
            cached = {"fa_flight_id": live["provider_flight_id"], "last_position": {
                "latitude": live.get("latitude"), "longitude": live.get("longitude"),
                "timestamp": live.get("observed_at"), "altitude": None,
                "groundspeed": live.get("groundspeed"), "heading": live.get("heading")}}
            altitude = live.get("altitude")
            if live.get("altitude_units") == "feet" and type(altitude) in (int, float) and math.isfinite(altitude):
                cached["last_position"]["altitude"] = altitude / 100
            current = _live_position(cached, flight, provider, now)
            if current:
                row["live"] = current
        if isinstance(value.get("reason"), str) and value["reason"] in {
                "monthly_limit", "daily_limit", "pricing_expired", "usage_unknown", "provider_unavailable"}:
            row["reason"] = value["reason"]
        if row["status"] == "tracking" and "live" not in row:
            row["status"] = ("scheduled" if provider.get("status") == "fresh" and provider.get("phase") in {
                "landed", "at_gate", "blocked", "tracking_ended"} else "stale" if provider else "unavailable")
        elif provider.get("status") == "stale":
            row["status"] = "stale"
        seen.add(flight["id"])
        result.append(row)
    return result


class PersonalFlightTracker:
    """One bounded paid request per interval, after the operating flight."""
    def __init__(self, request, data_dir):
        self.request, self.data_dir = request, data_dir
        self._cache = {}
        self._cursor = 0
        self._last_request = -math.inf

    def snapshot(self, config, records, now):
        if not _enabled(config):
            return []
        rows = []
        available = tracking_available(self.data_dir, config)
        for flight in personal_tracking_targets(records):
            entry = self._cache.get(flight["id"], {})
            result = entry.get("result", {}) if entry.get("flight") == flight else {}
            if personal_tracking_window(flight, now, result.get("provider")):
                rows.append({**copy.deepcopy(result), "id": flight["id"], "traveler": "lisa",
                             "reservation_status": flight["reservation_status"], "flight": flight,
                             "status": result.get("status", "pending" if available else "unconfigured")})
        return fresh_personal_tracking(rows[:MAX_TARGETS], config, now)

    def poll(self, config, records, now, tick):
        if not _enabled(config) or not tracking_available(self.data_dir, config):
            self._cache.clear()
            return
        targets = []
        for flight in personal_tracking_targets(records):
            entry = self._cache.get(flight["id"], {})
            provider = entry.get("result", {}).get("provider") if entry.get("flight") == flight else None
            if personal_tracking_window(flight, now, provider):
                targets.append(flight)
        targets = targets[:MAX_TARGETS]
        self._cache = {key: value for key, value in self._cache.items() if key in {flight["id"] for flight in targets}}
        if not targets:
            return
        interval = config.get("flightaware_poll_seconds", 120)
        if type(interval) is not int or not 60 <= interval <= 3600 or tick - self._last_request < interval:
            return
        ordered = targets[self._cursor % len(targets):] + targets[:self._cursor % len(targets)]
        for flight in ordered:
            entry = self._cache.get(flight["id"])
            if entry is None or entry["flight"] != flight:
                entry = self._cache[flight["id"]] = {"flight": flight, "result": {}, "next": "status", "retry_at": -math.inf}
            if entry.get("terminal") or tick < entry["retry_at"]:
                continue
            current_index = targets.index(flight)
            try:
                old = entry["result"]
                provider = old.get("provider", {})
                if (entry["next"] == "status" or not provider.get("provider_flight_id")
                        or fresh_status(provider, now).get("status") != "fresh"):
                    start = stamp(flight["start"])
                    params = urllib.parse.urlencode({"start": (start - timedelta(hours=1)).isoformat(),
                                                     "end": (start + timedelta(hours=1)).isoformat(),
                                                     "ident_type": "designator", "max_pages": 1})
                    response = self.request("/flights/" + flight["number"] + "?" + params, config)
                    outcome, match = match_personal_flight(response, flight)
                    if match is None:
                        entry["result"] = {"status": outcome}
                        entry["retry_at"] = tick + 300
                    else:
                        provider = normalize_operating_status(match, flight, now)
                        entry["result"] = {"status": "scheduled", "provider": provider}
                        if (old.get("live", {}).get("provider_flight_id") == provider["provider_flight_id"]
                                and old.get("live")):
                            entry["result"].update(status="tracking", live=old["live"])
                        entry["terminal"] = provider["phase"] in {"at_gate", "blocked", "tracking_ended"}
                        if entry["terminal"]:
                            entry["result"].pop("live", None)
                            entry["result"]["status"] = "scheduled"
                        entry["next"] = "status" if provider["phase"] == "landed" else "position"
                else:
                    response = self.request("/flights/" + urllib.parse.quote(provider["provider_flight_id"], safe="") + "/position", config)
                    live = _live_position(response, flight, provider, now)
                    entry["result"] = {"status": "tracking" if live else "scheduled", "provider": provider}
                    if live:
                        entry["result"]["live"] = live
                    entry["next"] = "status"
            except (ValueError, OSError, urllib.error.URLError, TypeError, KeyError, AttributeError, OverflowError) as error:
                reason = getattr(error, "reason", None)
                if reason == "household_away":
                    return
                provider = fresh_status(entry["result"].get("provider"), now, backend_available=False)
                reason = reason if isinstance(reason, str) and reason in {
                    "monthly_limit", "daily_limit", "pricing_expired", "usage_unknown"} else "provider_unavailable"
                entry["result"] = {"status": "stale" if provider.get("fetched_at") else "unavailable",
                                   "provider": provider, "reason": reason}
                entry["next"], entry["retry_at"] = "status", tick + 300
            self._last_request = tick
            self._cursor = (current_index + 1) % len(targets) if (
                entry["next"] != "position" or entry.get("terminal")) else current_index
            break
