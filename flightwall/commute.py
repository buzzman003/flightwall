"""Owner-requested VPS/ATL schedule planning, separate from roster and presence."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from .config import private_json
from .flight_alerts import stamp
from .personal import PersonalStore
from .outlook import calendar_fresh


ZONE = ZoneInfo("America/Chicago")
MAX_CANDIDATES = 8
SOURCE_MAX_AGE = 48 * 3600
_FIELDS = {"number", "origin", "destination", "start", "end", "source_url", "checked_at"}
_BINDING = ("id", "number", "origin", "destination", "start", "end", "report")


def _now(now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Commute planning needs a timezone-aware clock")
    return now.astimezone(timezone.utc)


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use a YYYY-MM-DD commute date")
    return date.fromisoformat(value)


def schedule_idents(value):
    if isinstance(value, str):
        if len(value) > 256:
            raise ValueError("Choose at most eight exact Delta flight numbers")
        value = json.loads(value)
    if (not isinstance(value, list) or len(value) > MAX_CANDIDATES
            or any(not isinstance(ident, str) or not re.fullmatch(r"DL[0-9]{1,5}", ident) for ident in value)
            or len(set(value)) != len(value)):
        raise ValueError("Choose at most eight distinct exact Delta flight numbers")
    return value


def report_leg(snapshot, travel_date, now, config=None):
    """Only the first explicitly timed roster leg can establish the report clock."""
    target = _day(travel_date)
    now, config = _now(now), config or {}
    flights = snapshot.get("flights") if isinstance(snapshot, dict) else None
    if not isinstance(flights, list) or snapshot.get("demo") is not False:
        return None
    if not calendar_fresh(snapshot.get("last_sync"), now, config.get("kids_stale_minutes", 30), failed=bool(snapshot.get("error"))):
        return None
    legs = []
    for flight in flights[:2000]:
        try:
            start = stamp(flight["start"])
            if start.astimezone(ZONE).date() == target:
                legs.append((start, flight))
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    if not legs:
        return None
    legs.sort(key=lambda row: row[0])
    first = legs[0][1]
    if len([row for row in legs if row[0] == legs[0][0]]) != 1 or first.get("origin") != "ATL":
        return None
    try:
        deadline = stamp(first.get("report"))
        start, end = stamp(first["start"]), stamp(first["end"])
        if not start - timedelta(days=1) <= deadline <= start < end or deadline.astimezone(ZONE).date() != target:
            return None
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    return {key: first.get(key) for key in _BINDING}


def _candidate(row):
    if (not isinstance(row, dict) or set(row) != _FIELDS or row.get("origin") != "VPS"
            or row.get("destination") != "ATL" or not isinstance(row.get("number"), str)
            or not re.fullmatch(r"DL[0-9]{1,5}", row["number"])):
        raise ValueError("Commute candidates need exact Delta VPS/ATL scheduled clocks and source dates")
    start, end, checked = (stamp(row[key]) for key in ("start", "end", "checked_at"))
    if not timedelta(0) < end - start <= timedelta(days=1):
        raise ValueError("Invalid scheduled commute duration")
    url = row["source_url"]
    if not isinstance(url, str) or len(url) > 2048 or any(ord(character) < 32 for character in url):
        raise ValueError("A public HTTPS schedule source is required")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("A public HTTPS schedule source is required")
    return {**row, "start": start.isoformat(), "end": end.isoformat(), "checked_at": checked.isoformat()}


def plan_commute(candidates, anchor, now):
    """Latest primary that leaves one later viable backup, with no invented buffer."""
    now = _now(now)
    if not isinstance(anchor, dict) or anchor.get("origin") != "ATL":
        return {"status": "missing_report"}
    try:
        deadline, start = stamp(anchor.get("report")), stamp(anchor.get("start"))
        if not start - timedelta(days=1) <= deadline <= start:
            return {"status": "missing_report"}
    except (ValueError, TypeError, OverflowError):
        return {"status": "missing_report"}
    if now >= deadline:
        return {"status": "expired"}
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        return {"status": "insufficient_schedule"}
    rows = [_candidate(row) for row in candidates]
    if any(not 0 <= (now - stamp(row["checked_at"])).total_seconds() < SOURCE_MAX_AGE for row in rows):
        return {"status": "schedule_stale"}
    day = deadline.astimezone(ZONE).date()
    viable = [row for row in rows if stamp(row["start"]).astimezone(ZONE).date() == day
              and stamp(row["end"]) <= deadline]
    unique = {(row["number"], row["start"], row["end"]): row for row in viable}
    viable = sorted(unique.values(), key=lambda row: (row["start"], row["end"], row["number"]))
    if len(viable) < 2:
        return {"status": "insufficient_schedule", "viable_count": len(viable)}
    # Different observations of the same marketed flight, or multiple flights
    # at one departure clock, cannot establish an unambiguous later fallback.
    primary, backup = viable[-2:]
    if (primary["number"] == backup["number"] or primary["start"] == backup["start"]
            or any(row["number"] in {primary["number"], backup["number"]}
                   and row not in (primary, backup) for row in viable)):
        return {"status": "ambiguous_schedule"}
    return {"status": "ready", "date": day.isoformat(), "report_deadline": deadline.isoformat(),
            "basis": "Published schedule / seat availability unverified", "viable_count": len(viable),
            "primary": dict(primary, role="primary"), "backup": dict(backup, role="backup")}


def _scheduled_rows(response, ident, target, now):
    if (not isinstance(response, dict) or not isinstance(response.get("flights"), list)
            or len(response["flights"]) > 1000 or response.get("num_pages", 1) != 1
            or not isinstance(response.get("links") or {}, dict) or (response.get("links") or {}).get("next")):
        raise ValueError("Schedule response is incomplete or ambiguous")
    result = []
    for row in response["flights"]:
        if (not isinstance(row, dict) or ident not in (row.get("ident"), row.get("ident_iata"), row.get("ident_icao"))
                or row.get("cancelled") is True or row.get("blocked") is True
                or not isinstance(row.get("origin"), dict) or not isinstance(row.get("destination"), dict)
                or row["origin"].get("code_iata") != "VPS" or row["destination"].get("code_iata") != "ATL"):
            continue
        try:
            candidate = _candidate({"number": ident, "origin": "VPS", "destination": "ATL",
                "start": row.get("scheduled_out"), "end": row.get("scheduled_in"),
                "source_url": "https://aeroapi.flightaware.com/aeroapi/flights/" + ident, "checked_at": now.isoformat()})
            if stamp(candidate["start"]).astimezone(ZONE).date() == target:
                result.append(candidate)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return result


def lookup_schedules(feeds, config, travel_date, idents, now):
    """One existing budgeted /flights lookup per explicit flight number."""
    now, target, idents = _now(now), _day(travel_date), schedule_idents(idents)
    if config.get("demo") is not False or config.get("flightaware_enabled") is not True or not idents:
        raise ValueError("Enable configured FlightAware tracking before schedule lookup")
    lower = datetime.combine(target, datetime.min.time(), ZONE).astimezone(timezone.utc)
    upper = datetime.combine(target + timedelta(days=1), datetime.min.time(), ZONE).astimezone(timezone.utc)
    if target < now.astimezone(ZONE).date() or upper > now + timedelta(days=2):
        raise ValueError("Commute schedule lookups cover today or tomorrow only")
    params = urlencode({"start": lower.isoformat(), "end": upper.isoformat(), "max_pages": 1})
    candidates = []
    for ident in idents:
        response = feeds._flightaware("/flights/" + ident + "?" + params, config)
        candidates.extend(_scheduled_rows(response, ident, target, now))
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError("Schedule candidates are ambiguous")
    return candidates


class CommuteStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "commute-plan.json"
        self.usage = self.data_dir / "commute-schedule-attempts.json"
        self.storage = PersonalStore(self.data_dir, lambda: {})
        self.storage.lock_path = self.data_dir / "commute.lock"

    @staticmethod
    def _read(path):
        if path.is_symlink() or path.stat().st_size > 32768:
            raise ValueError("Commute plan storage is unavailable")
        if not path.is_file():
            raise ValueError("Commute plan storage is unavailable")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, candidates, anchor, now, *, idents=None):
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            raise ValueError("Save at most eight exact scheduled commute candidates")
        result = plan_commute(candidates, anchor, now)
        if result["status"] in {"missing_report", "expired", "schedule_stale"}:
            raise ValueError("A current exact roster report and dated schedule candidates are required")
        document = {"schema": 1, "report": {key: anchor.get(key) for key in _BINDING},
                    "candidates": [_candidate(row) for row in candidates],
                    "idents": schedule_idents(idents) if idents is not None else None}
        with self.storage._locked():
            private_json(self.path, document)
        return result

    def status(self, snapshot, config, now):
        if config.get("demo") is not False:
            return {"status": "demo"}
        try:
            document = self._read(self.path)
            if (not isinstance(document, dict) or set(document) != {"schema", "report", "candidates", "idents"}
                    or document["schema"] != 1 or not isinstance(document["report"], dict)):
                return {"status": "unavailable"}
            if document["idents"] is not None and document["idents"] != schedule_idents(config.get("commute_schedule_idents", [])):
                return {"status": "selection_changed"}
            day = stamp(document["report"].get("report")).astimezone(ZONE).date().isoformat()
            anchor = report_leg(snapshot, day, now, config)
            if anchor is None or anchor != document["report"]:
                return {"status": "report_changed"}
            return plan_commute(document["candidates"], anchor, now)
        except FileNotFoundError:
            return {"status": "unconfigured"}
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return {"status": "unavailable"}

    def poll(self, snapshot, config, feeds, now, *, presence=None):
        from .trip_weather import trip_preview_eligible
        now = _now(now)
        gate = {"snapshot": snapshot, "presence": presence or {}}
        if not trip_preview_eligible(gate, config | {"trip_weather_enabled": True}, now):
            return self.status(snapshot, config, now)
        idents = schedule_idents(config.get("commute_schedule_idents", []))
        if not idents or config.get("flightaware_enabled") is not True:
            return self.status(snapshot, config, now)
        day = now.astimezone(ZONE).date()
        current = self.status(snapshot, config, now)
        if (current.get("status") == "ready"
                and all(stamp(current[role]["checked_at"]).astimezone(ZONE).date() == day for role in ("primary", "backup"))):
            return current
        target = (day + timedelta(days=1)).isoformat()
        anchor = report_leg(snapshot, target, now, config)
        if anchor is None:
            return {"status": "missing_report"}
        fingerprint = hashlib.sha256(json.dumps([anchor, idents], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            with self.storage._locked():
                try:
                    attempts = self._read(self.usage)
                except FileNotFoundError:
                    attempts = {"day": day.isoformat(), "anchors": []}
                if (not isinstance(attempts, dict) or set(attempts) != {"day", "anchors"}
                        or not isinstance(attempts["anchors"], list) or len(attempts["anchors"]) > 8
                        or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in attempts["anchors"])
                        or _day(attempts["day"]) > day):
                    return {"status": "usage_unknown"}
                if _day(attempts["day"]) < day:
                    attempts = {"day": day.isoformat(), "anchors": []}
                if fingerprint in attempts["anchors"] or len(attempts["anchors"]) >= 8:
                    return self.status(snapshot, config, now)
                attempts["anchors"].append(fingerprint)
                private_json(self.usage, attempts)
            candidates = lookup_schedules(feeds, config, target, idents, now)
            return self.save(candidates, anchor, now, idents=idents)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return {"status": "lookup_unavailable"}


def attach_commute_plan(snapshot, data_dir, config, now):
    result = dict(snapshot)
    result["commute_plan"] = CommuteStore(data_dir).status(snapshot, config, now)
    return result


def preview_commute_legs(snapshot, config, now):
    """A separate planned layer, never entries in the operating flight roster."""
    plan = snapshot.get("commute_plan") if isinstance(snapshot, dict) else None
    if not isinstance(plan, dict) or plan.get("status") != "ready" or config.get("demo") is not False:
        return []
    try:
        anchor = report_leg(snapshot, plan["date"], now, config)
        candidates = [{key: plan[role][key] for key in _FIELDS} for role in ("primary", "backup")]
        current = plan_commute(candidates, anchor, now)
        if current.get("status") != "ready" or current["report_deadline"] != plan.get("report_deadline"):
            return []
        return [(stamp(current[role]["start"]), stamp(current[role]["end"]),
                 dict(current[role], commute_role=role)) for role in ("primary", "backup")]
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return []
