"""Private household countdowns with persisted UTC deadlines, not Alexa system timers."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets

from .personal import PersonalError, PersonalStore


MAX_TIMERS = 5
MAX_DURATION_SECONDS = 24 * 60 * 60
MAX_LABEL = 80
MAX_REQUESTS = 2000
REQUEST_TTL = timedelta(days=30)
_FIELDS = {"id", "label", "started_at", "ends_at", "duration_seconds", "source", "cancelled"}


class TimerError(ValueError):
    """Safe, actionable errors; never echo stored labels or file contents."""


def _now():
    return datetime.now(timezone.utc)


def _aware(value=None):
    value = _now() if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TimerError("Use a timezone-aware current time for timers.")
    return value.astimezone(timezone.utc)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _label(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_LABEL
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                   or c in "\u2028\u2029" for c in value)):
        raise TimerError("Give the timer a label of 1 to 80 characters on one line.")
    return value.strip()


def _duration(value):
    if type(value) is not int or not 1 <= value <= MAX_DURATION_SECONDS:
        raise TimerError("Timer duration must be a whole number of seconds from 1 second to 24 hours.")
    return value


class TimerStore:
    def __init__(self, data_dir, config_getter=None):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "timers.json"
        self.lock_path = self.data_dir / "timers.lock"
        # Reuse the established private atomic-write and cross-process-lock
        # implementation, with separate files. No personal cards are read.
        self._storage = PersonalStore(data_dir, config_getter or (lambda: {}))
        self._storage.path, self._storage.lock_path = self.path, self.lock_path

    @contextmanager
    def _locked(self):
        try:
            with self._storage._locked():
                yield
        except PersonalError:
            raise TimerError("Timer storage is unavailable. Check its local directory and permissions.") from None

    def _write(self, state):
        try:
            self._storage._write(state)
        except PersonalError:
            raise TimerError("The timer could not be saved. Check free disk space and try again.") from None

    @staticmethod
    def _check_timer(timer):
        if not isinstance(timer, dict) or not _FIELDS.issubset(timer) or set(timer) - (_FIELDS | {"cancelled_at"}):
            raise ValueError("Invalid timer fields")
        if not isinstance(timer["id"], str) or not re.fullmatch(r"[a-f0-9]{24}", timer["id"]):
            raise ValueError("Invalid timer ID")
        if _label(timer["label"]) != timer["label"] or timer["source"] not in {"manual", "alexa"}:
            raise ValueError("Invalid timer identity")
        if type(timer["cancelled"]) is not bool:
            raise ValueError("Invalid cancelled flag")
        duration = _duration(timer["duration_seconds"])
        if _stamp(timer["ends_at"]) - _stamp(timer["started_at"]) != timedelta(seconds=duration):
            raise ValueError("Invalid timer duration")
        if "cancelled_at" in timer:
            _stamp(timer["cancelled_at"])
            if not timer["cancelled"]:
                raise ValueError("Invalid cancellation timestamp")

    def _read(self):
        try:
            with self.path.open(encoding="utf-8") as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("Oversized store")
            state = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if (not isinstance(state, dict) or set(state) != {"schema_version", "timers", "requests"}
                    or type(state["schema_version"]) is not int or state["schema_version"] != 1):
                raise ValueError("Invalid timer store")
            if (not isinstance(state["timers"], list) or len(state["timers"]) > MAX_TIMERS
                    or not isinstance(state["requests"], dict) or len(state["requests"]) > MAX_REQUESTS):
                raise ValueError("Invalid store capacity")
            seen = set()
            for timer in state["timers"]:
                self._check_timer(timer)
                if timer["id"] in seen:
                    raise ValueError("Duplicate timer ID")
                seen.add(timer["id"])
            for key, record in state["requests"].items():
                if (not re.fullmatch(r"[a-f0-9]{64}", key) or not isinstance(record, dict)
                        or set(record) != {"payload_hash", "created_at", "timer"}
                        or not isinstance(record["payload_hash"], str)
                        or not re.fullmatch(r"[a-f0-9]{64}", record["payload_hash"])):
                    raise ValueError("Invalid request record")
                _stamp(record["created_at"])
                self._check_timer(record["timer"])
            return state
        except FileNotFoundError:
            return {"schema_version": 1, "timers": [], "requests": {}}
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            raise TimerError("The timer file could not be read. Restore its backup or repair it locally; existing data was not changed.") from None

    @staticmethod
    def _project(timer, now):
        result = copy.deepcopy(timer)
        remaining = max(0, math.ceil((_stamp(timer["ends_at"]) - now).total_seconds()))
        result.update(remaining_seconds=remaining,
                      status="cancelled" if timer["cancelled"] else "running" if remaining else "expired")
        return result

    def start(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"label", "duration_seconds", "source", "request_id"}:
            raise TimerError("Use only label, duration_seconds, source and request_id for a timer.")
        label, duration = _label(payload.get("label")), _duration(payload.get("duration_seconds"))
        source = payload.get("source", "manual")
        if not isinstance(source, str) or source not in {"manual", "alexa"}:
            raise TimerError("Timer source must be manual or alexa.")
        request_id = payload.get("request_id")
        if "request_id" in payload and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 512
                or not request_id.strip() or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in request_id)):
            raise TimerError("Use a nonempty request_id of at most 512 characters.")
        intent = {"label": label, "duration_seconds": duration, "source": source}
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        key = hashlib.sha256(request_id.encode()).hexdigest() if request_id is not None else None
        with self._locked():
            now, state = _aware(), self._read()
            state["requests"] = {k: v for k, v in state["requests"].items()
                                 if _stamp(v["created_at"]) + REQUEST_TTL > now}
            if key in state["requests"]:
                record = state["requests"][key]
                if record["payload_hash"] != digest:
                    raise TimerError("This request_id was used for different timer details. Use a new request_id.")
                return {"created": False, "timer": self._project(record["timer"], now), "replaced": []}
            if key is not None and len(state["requests"]) >= MAX_REQUESTS:
                raise TimerError("The timer request history is full. Retry after older requests expire; no timer was added.")
            state["timers"] = [timer for timer in state["timers"] if not timer["cancelled"]]
            replaced = []
            if len(state["timers"]) == MAX_TIMERS:
                expired = [timer for timer in state["timers"] if _stamp(timer["ends_at"]) <= now]
                if not expired:
                    raise TimerError("Five timers are already running. Cancel one before starting another.")
                oldest = min(expired, key=lambda timer: (_stamp(timer["ends_at"]), timer["id"]))
                replaced.append(oldest["id"])
                state["timers"].remove(oldest)
            try:
                end = now + timedelta(seconds=duration)
            except OverflowError:
                raise TimerError("The timer deadline is outside the supported time range.") from None
            timer = {"id": secrets.token_hex(12), **intent, "started_at": _iso(now),
                     "ends_at": _iso(end), "cancelled": False}
            state["timers"].append(timer)
            if key is not None:
                state["requests"][key] = {"payload_hash": digest, "created_at": _iso(now), "timer": copy.deepcopy(timer)}
            self._write(state)
            return {"created": True, "timer": self._project(timer, now), "replaced": replaced}

    def list_timers(self, now=None):
        now = _aware(now)
        with self._locked():
            state = self._read()
        return [self._project(timer, now) for timer in state["timers"] if not timer["cancelled"]]

    def cancel(self, item_id):
        if not isinstance(item_id, str) or not re.fullmatch(r"[a-f0-9]{24}", item_id):
            raise TimerError("Use a timer ID returned by the timer list.")
        with self._locked():
            now, state = _aware(), self._read()
            timer = next((item for item in state["timers"] if item["id"] == item_id), None)
            if timer is None:
                timer = next((r["timer"] for r in state["requests"].values()
                              if r["timer"]["id"] == item_id and r["timer"]["cancelled"]), None)
                if timer is None:
                    raise TimerError("That timer was not found. List timers to choose its current ID.")
            if timer["cancelled"]:
                return {"cancelled": False, "timer": self._project(timer, now)}
            timer.update(cancelled=True, cancelled_at=_iso(now))
            for record in state["requests"].values():
                if record["timer"]["id"] == item_id:
                    record["timer"] = copy.deepcopy(timer)
            self._write(state)
            return {"cancelled": True, "timer": self._project(timer, now)}
