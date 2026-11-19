"""Opt-in household reservation observations, separate from duty and presence.

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

from .flight_alerts import SOURCE, fresh_status, gate_follow_window, normalize_operating_status, operating_ident, stamp
from .flight_watches import tracking_available
from .gate_checkpoint import GateCheckpoints
from .mycrew import airport_zone
from .personal_travel import MAX_RECORDS, _reservation, reservation_schedule


UTC = timezone.utc
MAX_TARGETS = 5
LIVE_MAX_AGE_SECONDS = 300
_RESERVATION_STATUSES = {"listed", "confirmed"}
_TARGET_FIELDS = {"id", "number", "origin", "destination", "start", "end", "deadhead",
                  "traveler", "reservation_status", "source"}
_PROVIDER_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


def personal_tracking_enabled(config, traveler=None):
    if traveler is None:
        return any(personal_tracking_enabled(config, who) for who in ("chris", "lisa"))
    return (isinstance(config, dict) and config.get("demo", True) is False
            and config.get("guest_mode", False) is False
            and config.get("personal_travel_cards_visible", True) is True
            and config.get("flightaware_enabled", False) is True
            and ((traveler == "lisa" and config.get("lisa_flight_tracking_enabled") is True)
                 or (traveler == "chris" and config.get("flightaware_household_gate_tracking_enabled") is True)))


def _target_id(flight):
    identity = [flight["traveler"], *(flight[key] for key in ("number", "origin", "destination", "start", "end"))]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]


def personal_tracking_targets(records, travelers=("lisa",)):
    """Reject full messages and unknown formats; project only opted-in legs."""
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        return []
    targets, conflicts = {}, set()
    for record in records:
        try:
            row = _reservation(record)
            traveler = row.get("traveler", "chris")
            if traveler not in travelers or traveler not in {"chris", "lisa"} or row["status"] not in _RESERVATION_STATUSES:
                continue
            target = {key: row[key] for key in ("number", "origin", "destination", "start", "end")}
            target["traveler"] = traveler
            target.update(id=_target_id(target), reservation_status=row["status"],
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
        start, end = reservation_schedule(value)
        if (value["traveler"] in {"lisa", "chris"} and value["reservation_status"] in _RESERVATION_STATUSES
                and value["source"] == "personal_travel" and value["deadhead"] is False
                and value["start"] == start.isoformat() and value["end"] == end.isoformat()
                and value["id"] == _target_id(value)):
            return dict(value)
        return None
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def personal_tracking_window(flight, now, provider=None, *, follow_gate=False):
    """Keep following a matched delayed flight when gate tracking is enabled."""
    try:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return False
        start, end = stamp(flight["start"]), stamp(flight["end"])
        if follow_gate and gate_follow_window(flight, now, provider):
            return True
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


def match_personal_flight(data, flight, *, provider_id=None):
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
        if not isinstance(item, dict):
            continue
        # The operating carrier may be a regional partner. Only accept a
        # marketing number explicitly returned for this flight by the provider.
        codeshares = item.get("codeshares_iata")
        if (flight["number"] not in (item.get("ident"), item.get("ident_iata"), item.get("ident_icao"))
                and not (isinstance(codeshares, list) and flight["number"] in codeshares)):
            continue
        origin, destination = item.get("origin"), item.get("destination")
        if (not isinstance(origin, dict) or not isinstance(destination, dict)
                or (origin.get("code_iata"), destination.get("code_iata")) != (flight["origin"], flight["destination"])):
            continue
        try:
            departure = stamp(item.get("scheduled_out") or item.get("scheduled_off"))
            if provider_id is None and departure != stamp(flight["start"]):
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        if not isinstance(item.get("fa_flight_id"), str) or not _PROVIDER_ID.fullmatch(item["fa_flight_id"]):
            continue
        if provider_id is None or provider_id == item["fa_flight_id"]:
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
    if not backend_available or not personal_tracking_enabled(config) or not isinstance(rows, list) or len(rows) > MAX_TARGETS:
        return []
    result, seen = [], set()
    for value in rows:
        if not isinstance(value, dict):
            continue
        flight = _safe_target(value.get("flight"))
        if (not flight or value.get("id") != flight["id"] or flight["id"] in seen
                or value.get("traveler") != flight["traveler"] or not personal_tracking_enabled(config, flight["traveler"])
                or value.get("reservation_status") != flight["reservation_status"]):
            continue
        raw_provider = value.get("provider")
        provider = fresh_status(raw_provider, now) if isinstance(raw_provider, dict) else {}
        if provider.get("status") in {"fresh", "stale"} and (
                provider.get("source") != SOURCE or provider.get("flight_id") != flight["id"]
                or any(provider.get(key) != flight[key] for key in ("number", "origin", "destination"))
                or not isinstance(provider.get("provider_flight_id"), str)
                or not _PROVIDER_ID.fullmatch(provider["provider_flight_id"])):
            provider = {}
        if not personal_tracking_window(flight, now, provider,
                                        follow_gate=config.get("flightaware_household_gate_tracking_enabled") is True):
            continue
        row = {"id": flight["id"], "traveler": flight["traveler"], "reservation_status": flight["reservation_status"],
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
        self.checkpoints = GateCheckpoints(data_dir, "reservations")
        self._cache = {}
        self._operating_observation = (None, {})
        self._cursor = 0
        self._last_request = -math.inf

    @staticmethod
    def _leg(flight):
        return flight["number"], flight["origin"], flight["destination"], stamp(flight["start"])

    def share_operating(self, config, operating, provider, live):
        """A passenger on the operating flight can reuse its verified observations."""
        observation = (None, {})
        if (config.get("flightaware_household_gate_tracking_enabled") is True and isinstance(operating, dict)
                and isinstance(provider, dict) and provider.get("source") == SOURCE and provider.get("provider_flight_id")
                and provider.get("status") in {"fresh", "stale"} and provider.get("flight_id") == operating.get("id")
                and all(provider.get(key) == operating.get(key) for key in ("number", "origin", "destination"))):
            try:
                number = operating_ident(operating, config.get("airline", ""))
                times = provider["times"]
                departure = stamp(times.get("scheduled_out") or times.get("scheduled_off"))
                key = number, operating["origin"], operating["destination"], departure
                result = {"status": "scheduled", "provider": dict(provider, number=number)}
                if (isinstance(live, dict) and live.get("flight_id") == operating["id"]
                        and live.get("provider_flight_id") == provider["provider_flight_id"]):
                    result.update(status="tracking", live=dict(live, source=SOURCE))
                observation = (key, result)
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        self._operating_observation = observation

    def _shared_result(self, flight):
        # Multiple household members may book the same physical aircraft.
        # Reuse its observations while giving each card its own traveler ID.
        key = self._leg(flight)
        duty, operating = self._operating_observation
        entry = self._cache.get(key, {})
        result = copy.deepcopy(operating if duty == key else entry.get("result", {}))
        for field in ("provider", "live"):
            if field in result and result[field].get("flight_id"):
                result[field]["flight_id"] = flight["id"]
        return result

    def _targets(self, config, records, now, operating):
        travelers = [who for who in ("chris", "lisa") if personal_tracking_enabled(config, who)]
        follow_gate = config.get("flightaware_household_gate_tracking_enabled") is True
        try:
            duty = (operating_ident(operating, config.get("airline", "")), operating["origin"],
                    operating["destination"], stamp(operating["start"])) if operating and not operating.get("deadhead") else None
        except (ValueError, TypeError, KeyError):
            duty = None
        for flight in personal_tracking_targets(records, travelers):
            if (flight["traveler"] == "chris"
                    and self._leg(flight) == duty):
                continue
            provider = self._shared_result(flight).get("provider")
            if follow_gate and not provider:
                provider = self.checkpoints.get(flight, None, now)
            if personal_tracking_window(flight, now, provider, follow_gate=follow_gate):
                yield flight

    def snapshot(self, config, records, now, *, operating=None):
        if not personal_tracking_enabled(config):
            return []
        rows = []
        available = tracking_available(self.data_dir, config)
        for flight in self._targets(config, records, now, operating):
            result = self._shared_result(flight)
            if not result and config.get("flightaware_household_gate_tracking_enabled") is True:
                saved = self.checkpoints.get(flight, None, now)
                if saved:
                    result = {"status": "scheduled", "provider": saved}
            rows.append({**result, "id": flight["id"], "traveler": flight["traveler"],
                         "reservation_status": flight["reservation_status"], "flight": flight,
                         "status": result.get("status", "pending" if available else "unconfigured")})
        return fresh_personal_tracking(rows[:MAX_TARGETS], config, now)

    def poll(self, config, records, now, tick, *, operating=None):
        if not personal_tracking_enabled(config) or not tracking_available(self.data_dir, config):
            self._cache.clear()
            return
        duty, _ = self._operating_observation
        by_leg = {self._leg(flight): flight for flight in self._targets(config, records, now, operating)
                  if self._leg(flight) != duty}
        targets = list(by_leg.values())[:MAX_TARGETS]
        keys = {self._leg(flight) for flight in targets}
        self._cache = {key: value for key, value in self._cache.items() if key in keys}
        if not targets:
            return
        interval = config.get("flightaware_poll_seconds", 120)
        if type(interval) is not int or not 60 <= interval <= 3600 or tick - self._last_request < interval:
            return
        ordered = targets[self._cursor % len(targets):] + targets[:self._cursor % len(targets)]
        for flight in ordered:
            key = self._leg(flight)
            entry = self._cache.get(key)
            if entry is None:
                entry = self._cache[key] = {"flight": flight, "result": {}, "next": "status", "retry_at": -math.inf}
                if config.get("flightaware_household_gate_tracking_enabled") is True:
                    saved = self.checkpoints.get(flight, None, now)
                    if saved:
                        entry.update(result={"status": "scheduled", "provider": saved},
                                     provider_id=saved["provider_flight_id"],
                                     terminal=saved["phase"] in {"at_gate", "blocked", "tracking_ended"})
            elif entry["flight"] != flight:
                entry["result"] = self._shared_result(flight)
                entry["flight"] = flight
            if entry.get("terminal") or tick < entry["retry_at"]:
                continue
            current_index = targets.index(flight)
            try:
                old = entry["result"]
                provider = old.get("provider", {})
                if (entry["next"] == "status" or not provider.get("provider_flight_id")
                        or fresh_status(provider, now).get("status") != "fresh"):
                    start = stamp(flight["start"])
                    params = urllib.parse.urlencode({"start": (start - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                     "end": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                     "ident_type": "designator", "max_pages": 1})
                    response = self.request("/flights/" + flight["number"] + "?" + params, config)
                    outcome, match = match_personal_flight(response, flight, provider_id=entry.get("provider_id"))
                    if match is None:
                        if entry.get("provider_id") and provider.get("fetched_at"):
                            entry["result"] = {"status": "stale", "provider": fresh_status(provider, now, False)}
                        else:
                            entry["result"] = {"status": outcome}
                        entry["retry_at"] = tick + 300
                    else:
                        provider = normalize_operating_status(match, flight, now)
                        if config.get("flightaware_household_gate_tracking_enabled") is True:
                            self.checkpoints.save(flight, None, provider, now)
                        entry["provider_id"] = provider["provider_flight_id"]
                        entry["result"] = {"status": "scheduled", "provider": provider}
                        if (old.get("live", {}).get("provider_flight_id") == provider["provider_flight_id"]
                                and old.get("live")):
                            entry["result"].update(status="tracking", live=old["live"])
                        entry["terminal"] = provider["phase"] in {"at_gate", "blocked", "tracking_ended"}
                        if entry["terminal"]:
                            entry["result"].pop("live", None)
                            entry["result"]["status"] = "scheduled"
                        status_only = (provider["phase"] == "landed" or
                                       config.get("flightaware_household_gate_tracking_enabled") is True
                                       and provider["phase"] == "scheduled")
                        entry["next"] = "status" if status_only else "position"
                else:
                    response = self.request("/flights/" + urllib.parse.quote(provider["provider_flight_id"], safe="") + "/position", config)
                    live = _live_position(response, flight, provider, now)
                    entry["result"] = {"status": "tracking" if live else "scheduled", "provider": provider}
                    if live:
                        entry["result"]["live"] = live
                    entry["next"] = "status"
            except (ValueError, OSError, urllib.error.URLError, TypeError, KeyError, AttributeError, OverflowError) as error:
                reason = getattr(error, "reason", None)
                if reason in ("household_away", "quiet_hours"):
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
