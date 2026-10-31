"""Opt-in phone presence with no saved coordinates or location history.

Only the most recent state and observation time are retained per person.
Request-ID hashes support retry detection without retaining phone identifiers.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading

from .config import private_json
from .personal import PersonalStore, PersonalError

try:
    import fcntl
except ImportError:
    fcntl = None

PEOPLE = ("chris", "lisa")
STATES = ("home", "away", "unknown")
SOURCES = ("phone", "manual", "owntracks-transition", "owntracks-location")
MAX_REQUESTS = 128
FUTURE_SKEW_SECONDS = 120
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})$")


class PresenceError(ValueError):
    """A safe, actionable validation message, without location details."""


class PresenceDisabled(PresenceError):
    pass


def _person(value):
    if value not in PEOPLE:
        raise PresenceError("Choose the Chris or Lisa presence profile.")
    return value


def _now(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PresenceError("Use a timezone-aware current time.")
    return value.astimezone(timezone.utc)


def _parse_stamp(value):
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        raise PresenceError("observed_at must be an RFC3339 timestamp with a timezone.")
    try:
        result = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise PresenceError("observed_at must be a valid RFC3339 timestamp.") from None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


class PresenceStore:
    def __init__(self, data_dir, config_getter):
        self.directory = Path(data_dir) / "presence"
        self.config_getter = config_getter
        self.path = self.directory / "state.json"
        self._thread_lock = threading.RLock()

    def _prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)

    @contextmanager
    def _locked(self):
        storage = PersonalStore(self.directory, self.config_getter)
        storage.lock_path = self.directory / ".lock"
        try:
            with self._thread_lock, storage._locked():
                yield
        except PersonalError:
            raise PresenceError("Presence storage is unavailable; check local permissions.") from None

    def token_path(self, person):
        """Return the private path; merely looking it up never pairs a phone."""
        return self.directory / (_person(person) + ".token")

    def rotate(self, person):
        """Explicit owner pairing: return a new secret once and forget old state."""
        person = _person(person)
        with self._locked():
            state = self._read()
            # Clear presence before publishing a replacement credential. If the
            # token write fails, the failed pairing cannot leave fresh Home state.
            state["people"].pop(person, None)
            state["requests"].pop(person, None)
            private_json(self.path, state)
            token = secrets.token_urlsafe(48)
            path = self.token_path(person)
            temporary = self.directory / ("." + person + "." + secrets.token_hex(6))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(token + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return token

    def authenticate(self, person, token):
        person = _person(person)
        if not isinstance(token, str) or not token.isascii() or not 32 <= len(token) <= 2048:
            return False
        try:
            stored = self.token_path(person).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        return len(stored) >= 32 and hmac.compare_digest(token, stored)

    def _read(self):
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"version", "people", "requests"} or value["version"] != 1:
                raise ValueError()
            if not isinstance(value["people"], dict) or value["people"].keys() - set(PEOPLE):
                raise ValueError()
            if not isinstance(value["requests"], dict) or value["requests"].keys() - set(PEOPLE):
                raise ValueError()
            for record in value["people"].values():
                if (not isinstance(record, dict) or set(record) != {"state", "observed_at", "source"}
                        or record["state"] not in STATES or record["source"] not in SOURCES):
                    raise ValueError()
                _parse_stamp(record["observed_at"])
            for ledger in value["requests"].values():
                if not isinstance(ledger, dict) or len(ledger) > MAX_REQUESTS:
                    raise ValueError()
                for request_hash, record in ledger.items():
                    if (not re.fullmatch(r"[a-f0-9]{64}", request_hash) or not isinstance(record, dict)
                            or set(record) != {"observed_at", "fingerprint"}
                            or not re.fullmatch(r"[a-f0-9]{64}", record["fingerprint"])):
                        raise ValueError()
                    _parse_stamp(record["observed_at"])
            return value
        except FileNotFoundError:
            return {"version": 1, "people": {}, "requests": {}}
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            raise PresenceError("Presence state could not be read. Repair it locally; presence remains unknown.") from None

    @staticmethod
    def _ttl(config):
        minutes = config.get("presence_ttl_minutes", 90)
        if type(minutes) is not int or not 5 <= minutes <= 1440:
            raise PresenceError("presence_ttl_minutes must be from 5 to 1440 minutes.")
        return timedelta(minutes=minutes)

    @staticmethod
    def _normalize(payload, config, source):
        if not isinstance(payload, dict) or len(payload) > 40:
            raise PresenceError("Send a JSON presence object.")
        request_id = payload.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 512
                                       or any(ord(char) < 32 or ord(char) == 127 for char in request_id)):
            raise PresenceError("request_id must be a nonempty string of at most 512 characters.")
        kind = payload.get("_type")
        if kind in ("transition", "location"):
            timestamp = payload.get("observed_at")
            if timestamp is None:
                seconds = payload.get("tst")
                if not _number(seconds, 0, 253402300799):
                    raise PresenceError("OwnTracks reports need a valid tst observation timestamp.")
                try:
                    observed = datetime.fromtimestamp(seconds, timezone.utc)
                except (ValueError, OverflowError, OSError):
                    raise PresenceError("OwnTracks timestamp is outside the supported range.") from None
            else:
                observed = _parse_stamp(timestamp)
            if kind == "transition":
                description = payload.get("desc", payload.get("description", ""))
                if not isinstance(description, str) or description.strip().casefold() != "home":
                    raise PresenceError("Only transitions for the Home region are accepted.")
                if payload.get("event") not in ("enter", "leave"):
                    raise PresenceError("OwnTracks transition event must be enter or leave.")
                state = "home" if payload["event"] == "enter" else "away"
            else:
                latitude, longitude = payload.get("lat"), payload.get("lon")
                home_lat, home_lon = config.get("home_latitude"), config.get("home_longitude")
                radius, accuracy = config.get("home_radius_m", 200), payload.get("acc", 0)
                if not _number(latitude, -90, 90) or not _number(longitude, -180, 180):
                    raise PresenceError("OwnTracks location needs valid latitude and longitude.")
                if not _number(home_lat, -90, 90) or not _number(home_lon, -180, 180):
                    raise PresenceError("Configure the private Home geofence on the display computer first.")
                if not _number(radius, 30, 5000) or not _number(accuracy, 0, 100000):
                    raise PresenceError("The Home radius or reported location accuracy is invalid.")
                first, second = math.radians(home_lat), math.radians(latitude)
                dlat, dlon = second - first, math.radians(longitude - home_lon)
                hav = math.sin(dlat / 2) ** 2 + math.cos(first) * math.cos(second) * math.sin(dlon / 2) ** 2
                distance = 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, hav))))
                state = ("home" if distance + accuracy <= radius
                         else "away" if distance - accuracy > radius else "unknown")
            source = "owntracks-" + kind
        elif kind is None:
            if payload.keys() - {"state", "observed_at", "request_id"} or payload.get("state") not in STATES:
                raise PresenceError("Send only state (home, away or unknown), observed_at and optional request_id.")
            observed = _parse_stamp(payload.get("observed_at"))
            state = payload["state"]
            source = source or "phone"
            if source not in ("phone", "manual"):
                raise PresenceError("Invalid presence update source.")
        else:
            raise PresenceError("Use a presence state, OwnTracks transition or OwnTracks location report.")
        return {"state": state, "observed_at": _iso(observed), "source": source}, request_id

    def update(self, person, payload, now=None, *, source=None):
        person, now = _person(person), _now(now)
        config = self.config_getter()
        if not config.get("presence_enabled", False):
            raise PresenceDisabled("Presence is disabled. Enable it locally after each person opts in.")
        ttl = self._ttl(config)
        record, request_id = self._normalize(payload, config, source)
        observed = _parse_stamp(record["observed_at"])
        if observed > now + timedelta(seconds=FUTURE_SKEW_SECONDS):
            raise PresenceError("The observation is in the future. Check the phone's clock.")
        if observed <= now - ttl:
            raise PresenceError("The observation has expired. Send a fresh phone status.")
        fingerprint = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        request_hash = hashlib.sha256(request_id.encode()).hexdigest() if request_id is not None else None
        with self._locked():
            stored = self._read()
            ledger = stored["requests"].setdefault(person, {})
            for key in [key for key, value in ledger.items() if _parse_stamp(value["observed_at"]) <= now - ttl]:
                del ledger[key]
            if request_hash in ledger:
                if ledger[request_hash]["fingerprint"] != fingerprint:
                    raise PresenceError("That request_id was already used for a different observation.")
                return {"updated": False, "duplicate": True, "person": person}
            previous = stored["people"].get(person)
            if previous:
                previous_time = _parse_stamp(previous["observed_at"])
                if observed < previous_time or observed == previous_time and record != previous:
                    raise PresenceError("A newer or conflicting observation is already saved; send a fresh status.")
                if record == previous:
                    return {"updated": False, "duplicate": True, "person": person}
            stored["people"][person] = record
            if request_hash is not None:
                ledger[request_hash] = {"observed_at": record["observed_at"], "fingerprint": fingerprint}
                if len(ledger) > MAX_REQUESTS:
                    oldest = min(ledger, key=lambda key: ledger[key]["observed_at"])
                    del ledger[oldest]
            private_json(self.path, stored)
        return {"updated": True, "duplicate": False, "person": person}

    def snapshot(self, now=None):
        now, config = _now(now), self.config_getter()
        unknown = {person: {"state": "unknown", "last_seen": None, "stale": True} for person in PEOPLE}
        if not config.get("presence_enabled", False):
            return unknown
        try:
            ttl = self._ttl(config)
            with self._locked():
                stored = self._read()
            for person, record in stored["people"].items():
                observed = _parse_stamp(record["observed_at"])
                stale = observed <= now - ttl or observed > now + timedelta(seconds=FUTURE_SKEW_SECONDS)
                unknown[person] = {"state": "unknown" if stale else record["state"],
                                   "last_seen": record["observed_at"], "stale": stale}
        except PresenceError:
            pass  # Missing/corrupt/unsupported state never implies someone is home.
        return unknown
