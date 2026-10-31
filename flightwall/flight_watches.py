"""Bounded, explicit public-flight requests, separate from operating calendars."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
from zoneinfo import ZoneInfo

from .personal import PersonalError, PersonalStore


MAX_WATCHES = 5
MAX_DAYS_AHEAD = 14
MAX_REQUESTS = 2000
REQUEST_TTL = timedelta(days=30)
_FIELDS = {"id", "airline", "flight_number", "ident", "date", "timezone", "source", "created_at"}
_ALIASES = {
    "delta": "DL", "delta airlines": "DL", "delta air lines": "DL",
    "american": "AA", "american airlines": "AA", "united": "UA", "united airlines": "UA",
    "allegiant": "G4", "allegiant air": "G4", "frontier": "F9", "frontier airlines": "F9",
    "air arabia": "G9",
}


class FlightWatchError(ValueError):
    """Errors contain no calendar, credential, or provider response contents."""


def _now():
    return datetime.now(timezone.utc)


def _aware(value=None):
    value = _now() if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FlightWatchError("Use a timezone-aware current time for flight requests.")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise FlightWatchError("Give an explicit flight date as YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise FlightWatchError("Give a real flight date as YYYY-MM-DD.") from None


def _airline(value):
    if not isinstance(value, str):
        raise FlightWatchError("Give the airline name or its two- or three-character code.")
    value = _ALIASES.get(" ".join(value.strip().lower().split()), value.strip().upper())
    if not re.fullmatch(r"(?:[A-Z][A-Z0-9]|[0-9][A-Z]|[A-Z]{3})", value):
        raise FlightWatchError("Use a recognized airline name or a two- or three-character airline code.")
    return value


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}", value) or int(value) == 0:
        raise FlightWatchError("Give a flight number from 1 to 9999, without an airline prefix.")
    return str(int(value))


def tracking_available(data_dir, config):
    """Configuration readiness only; never implies a successful provider lookup."""
    if config.get("demo", True) or not config.get("flightaware_enabled", False):
        return False
    try:
        with (Path(data_dir) / "flightaware.key").open(encoding="utf-8") as stream:
            key = stream.read(4097)
        return bool(key.strip()) and len(key) <= 4096
    except (OSError, UnicodeError):
        return False


def day_bounds(watch):
    """Requested board-local day converted to an end-exclusive UTC interval."""
    day, zone = _date(watch["date"]), ZoneInfo(watch["timezone"])
    return (datetime.combine(day, time.min, zone).astimezone(timezone.utc),
            datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc))


class FlightWatchStore:
    def __init__(self, data_dir, config_getter):
        self.data_dir = Path(data_dir)
        self.config_getter = config_getter
        self.path = self.data_dir / "flight-watches.json"
        self.lock_path = self.data_dir / "flight-watches.lock"
        self._storage = PersonalStore(data_dir, config_getter)
        self._storage.path, self._storage.lock_path = self.path, self.lock_path

    @contextmanager
    def _locked(self):
        try:
            with self._storage._locked():
                yield
        except PersonalError:
            raise FlightWatchError("Flight-request storage is unavailable. Check its local directory and permissions.") from None

    def _write(self, state):
        try:
            self._storage._write(state)
        except PersonalError:
            raise FlightWatchError("The flight request could not be saved. Check free disk space and try again.") from None

    @staticmethod
    def _check_watch(watch):
        if not isinstance(watch, dict) or set(watch) != _FIELDS:
            raise ValueError("Invalid watch fields")
        if not isinstance(watch["id"], str) or not re.fullmatch(r"[a-f0-9]{24}", watch["id"]):
            raise ValueError("Invalid watch ID")
        if (_airline(watch["airline"]) != watch["airline"] or _number(watch["flight_number"]) != watch["flight_number"]
                or watch["ident"] != watch["airline"] + watch["flight_number"]
                or watch["source"] not in {"manual", "alexa"}):
            raise ValueError("Invalid flight identity")
        _stamp(watch["created_at"])
        start, end = day_bounds(watch)
        if end <= start:
            raise ValueError("Invalid local day")

    def _read(self):
        try:
            with self.path.open(encoding="utf-8") as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("Oversized store")
            state = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if (not isinstance(state, dict) or set(state) != {"schema_version", "watches", "requests"}
                    or type(state["schema_version"]) is not int or state["schema_version"] != 1
                    or not isinstance(state["watches"], list) or len(state["watches"]) > MAX_WATCHES
                    or not isinstance(state["requests"], dict) or len(state["requests"]) > MAX_REQUESTS):
                raise ValueError("Invalid flight-request store")
            seen, identities = set(), set()
            for watch in state["watches"]:
                self._check_watch(watch)
                identity = watch["ident"], watch["date"], watch["timezone"]
                if watch["id"] in seen or identity in identities:
                    raise ValueError("Duplicate flight request")
                seen.add(watch["id"])
                identities.add(identity)
            for key, record in state["requests"].items():
                if (not re.fullmatch(r"[a-f0-9]{64}", key) or not isinstance(record, dict)
                        or set(record) != {"payload_hash", "created_at", "watch"}
                        or not isinstance(record["payload_hash"], str)
                        or not re.fullmatch(r"[a-f0-9]{64}", record["payload_hash"])):
                    raise ValueError("Invalid request record")
                _stamp(record["created_at"])
                self._check_watch(record["watch"])
            return state
        except FileNotFoundError:
            return {"schema_version": 1, "watches": [], "requests": {}}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise FlightWatchError("The flight-request file could not be read. Repair it locally; existing data was not changed.") from None

    @staticmethod
    def _current(watch, now):
        today = now.astimezone(ZoneInfo(watch["timezone"])).date()
        return 0 <= (_date(watch["date"]) - today).days <= MAX_DAYS_AHEAD

    def create(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"airline", "flight_number", "date", "source", "request_id"}:
            raise FlightWatchError("Use only airline, flight_number, date, source and request_id for a flight request.")
        airline, number, day = _airline(payload.get("airline")), _number(payload.get("flight_number")), _date(payload.get("date"))
        source, request_id = payload.get("source", "manual"), payload.get("request_id")
        if not isinstance(source, str) or source not in {"manual", "alexa"}:
            raise FlightWatchError("Flight-request source must be manual or alexa.")
        if "request_id" in payload and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 512
                or not request_id.strip() or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in request_id)):
            raise FlightWatchError("Use a nonempty request_id of at most 512 characters.")
        intent = {"airline": airline, "flight_number": number, "date": day.isoformat(), "source": source}
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        key = hashlib.sha256(request_id.encode()).hexdigest() if request_id is not None else None
        with self._locked():
            now, state, config = _aware(), self._read(), self.config_getter()
            state["requests"] = {k: v for k, v in state["requests"].items() if _stamp(v["created_at"]) + REQUEST_TTL > now}
            if key in state["requests"]:
                previous = state["requests"][key]
                if previous["payload_hash"] != digest:
                    raise FlightWatchError("This request_id was used for a different flight. Use a new request_id.")
                return {"created": False, "watch": copy.deepcopy(previous["watch"]),
                        "tracking_available": tracking_available(self.data_dir, config)}
            try:
                zone = ZoneInfo(config.get("timezone", "UTC"))
            except (ValueError, KeyError, TypeError):
                raise FlightWatchError("Set a valid board timezone before adding a flight.") from None
            if not 0 <= (day - now.astimezone(zone).date()).days <= MAX_DAYS_AHEAD:
                raise FlightWatchError("Choose today or a date within the next 14 days in the board timezone.")
            if key is not None and len(state["requests"]) >= MAX_REQUESTS:
                raise FlightWatchError("The request history is full. Retry after older requests expire.")
            state["watches"] = [watch for watch in state["watches"] if self._current(watch, now)]
            ident = airline + number
            watch = next((item for item in state["watches"] if (item["ident"], item["date"], item["timezone"])
                          == (ident, day.isoformat(), zone.key)), None)
            created = watch is None
            if created:
                if len(state["watches"]) >= MAX_WATCHES:
                    raise FlightWatchError("Five public flights are already on the board. Wait until an earlier requested day ends.")
                watch = {"id": secrets.token_hex(12), **intent, "ident": ident, "timezone": zone.key,
                         "created_at": now.isoformat()}
                state["watches"].append(watch)
            if key is not None:
                state["requests"][key] = {"payload_hash": digest, "created_at": now.isoformat(), "watch": copy.deepcopy(watch)}
            self._write(state)
            return {"created": created, "watch": copy.deepcopy(watch),
                    "tracking_available": tracking_available(self.data_dir, config)}

    def list_watches(self, now=None):
        now = _aware(now)
        with self._locked():
            state = self._read()
        return [copy.deepcopy(watch) for watch in state["watches"] if self._current(watch, now)]
