"""Short-lived status receipts for resuming a matched household flight after restart."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import threading

from .config import private_json
from .flight_alerts import TIME_FIELDS, normalize_operating_status, stamp


class GateCheckpoints:
    def __init__(self, directory, group):
        self.path = Path(directory) / ("flight-gates-" + group + ".json")
        self.group = group
        self.entries = None
        self.lock = threading.Lock()

    def key(self, flight, airline):
        identity = [flight.get(field) for field in ("number", "origin", "destination", "start", "end")]
        if self.group == "operating":
            identity.append(flight.get("id"))
        return hashlib.sha256(json.dumps([airline, *identity], separators=(",", ":")).encode()).hexdigest()

    def _load(self, now):
        if self.entries is None:
            self.entries = {}
            try:
                with self.path.open("rb") as stream:
                    raw = stream.read(32769)
                values = json.loads(raw) if len(raw) <= 32768 else {}
                if isinstance(values, dict) and len(values) <= 8:
                    self.entries = {key: v for key, v in values.items() if re.fullmatch(r"[a-f0-9]{64}", key)
                                    and isinstance(v, dict) and stamp(v["expires"]) <= now + timedelta(days=4)}
            except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
                self.entries = {}
        expired = [key for key, v in self.entries.items() if not now < stamp(v["expires"])]
        for key in expired:
            self.entries.pop(key)
        if expired:
            self._write()

    def _write(self):
        try:
            if self.entries:
                private_json(self.path, self.entries)
            else:
                self.path.unlink(missing_ok=True)
        except OSError:
            pass  # Tracking can still operate in memory.

    def clear(self):
        with self.lock:
            if self.entries != {}:
                self.entries = {}
                self._write()

    def prune(self, now):
        with self.lock:
            self._load(now)

    def save(self, flight, airline, status, now):
        with self.lock:
            self._load(now)
            observed = stamp(status["fetched_at"])
            expires = min(observed + timedelta(days=3), stamp(flight["end"]) + timedelta(hours=25))
            if expires <= now:
                return
            key = self.key(flight, airline)
            self.entries[key] = {"expires": expires.isoformat(), "checked": observed.isoformat(),
                                 "provider_id": status["provider_flight_id"],
                                 "clocks": {k: v for k, v in status["times"].items() if k in TIME_FIELDS},
                                 "cancelled": status["cancelled"], "diverted": status["diverted"],
                                 "blocked": status["phase"] == "blocked"}
            if len(self.entries) > 8:
                del self.entries[min(self.entries, key=lambda k: self.entries[k]["expires"])]
            self._write()

    def get(self, flight, airline, now):
        with self.lock:
            self._load(now)
            value = self.entries.get(self.key(flight, airline))
            if not value:
                return None
            try:
                scheduled = stamp(flight["start"])
                checked = stamp(value["checked"])
                if (not scheduled - timedelta(hours=4) <= checked <= now
                        or now > stamp(flight["end"]) + timedelta(hours=25)
                        or now - checked > timedelta(days=3)
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value["provider_id"])
                        or not isinstance(value["clocks"], dict) or set(value["clocks"]) - set(TIME_FIELDS)
                        or type(value["blocked"]) is not bool
                        or any(type(value[key]) not in {bool, type(None)} for key in ("cancelled", "diverted"))):
                    return None
                return normalize_operating_status({**value["clocks"], "fa_flight_id": value["provider_id"],
                                                   **{k: value[k] for k in ("cancelled", "diverted", "blocked")}},
                                                  flight, checked)
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
