"""Private, local appointment and dinner cards; no calendar writes or cloud calls."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

msvcrt = None
try:
    import fcntl
except ImportError:  # Windows development only; the Batocera runtime uses flock.
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None


UTC = timezone.utc
MAX_ITEMS = 1000
MAX_REQUESTS = 2000
MAX_DINNER_DETAILS = MAX_ITEMS + MAX_REQUESTS
MAX_DETAILS_BYTES = 2_000_000
REQUEST_TTL = timedelta(days=30)
_DINNER_INPUT = {"sides", "ready_time", "ready_window_start", "ready_window_end"}
_DINNER_STORED = {"sides", "ready_at", "ready_until"}
_ALLOWED = {"kind", "title", "date", "time", "duration_minutes", "request_id", "source"} | _DINNER_INPUT
_ITEM_REQUIRED = {"id", "kind", "title", "source", "timezone", "start", "end", "display_start",
                  "display_end", "created_at", "cancelled"}
_STAMP_KEYS = ("start", "end", "display_start", "display_end", "created_at")


class PersonalError(ValueError):
    """An actionable message that can be safely returned to the owner."""


def _now():
    return datetime.now(UTC)


def _aware_now(value=None):
    value = _now() if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PersonalError("Use a timezone-aware current time for personal cards.")
    return value.astimezone(UTC)


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Missing timestamp timezone")
    return parsed.astimezone(UTC)


def _title(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160 or any(ord(char) < 32 or ord(char) == 127
            or 0xD800 <= ord(char) <= 0xDFFF or char in "\u2028\u2029" for char in value):
        raise PersonalError("Give the card a title of 1 to 160 characters on one line.")
    return value.strip()


def _sides(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 120
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                   or char in "\u2028\u2029" for char in value)):
        raise PersonalError("Give sides as 1 to 120 characters on one line, or omit them.")
    return value.strip()


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PersonalError("Give the date as YYYY-MM-DD, for example 2026-12-10.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise PersonalError("That date is not valid; use a real date as YYYY-MM-DD.") from None


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise PersonalError("Give the time in 24-hour HH:MM format, for example 14:30.")
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise PersonalError("That time is not valid; use 00:00 through 23:59.") from None


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1440:
        raise PersonalError("Appointment duration must be a whole number from 1 to 1440 minutes.")
    return value


def _local(day, clock, zone):
    """Reject DST gaps/folds instead of choosing a different time silently."""
    naive = datetime.combine(day, clock)
    instants = {}
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        try:
            utc = candidate.astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) == naive:
                instants[utc] = candidate
        except (ValueError, OverflowError):
            raise PersonalError("That date is outside the supported time range. Choose a nearer date.") from None
    when = f"{day.isoformat()} at {clock.strftime('%H:%M')} in {zone.key}"
    if not instants:
        raise PersonalError(f"{when} does not exist because the clocks change. Choose another local time or date.")
    if len(instants) > 1:
        raise PersonalError(f"{when} occurs twice because the clocks change. Choose an unambiguous local time or date.")
    return next(iter(instants))


def dinner_meal_date(item, config=None):
    """The saved meal's original local date, not a guessed serving time."""
    if not isinstance(item, dict) or item.get("kind") != "dinner":
        return None
    try:
        zone = ZoneInfo(item.get("timezone") or (config or {}).get("timezone") or "UTC")
        return _stamp(item.get("start") or item.get("display_start")).astimezone(zone).date()
    except (ValueError, TypeError, KeyError, OverflowError, ZoneInfoNotFoundError):
        return None


def dinner_display_window(item, config, now):
    """Project tomorrow's menu into today's configured household window.

    Stored meal/start/end values are never moved. Today's meal also retains
    its written boundaries; an expected-ready clock never extends visibility.
    Partial legacy UI cards keep their explicit interval instead of guessing
    which original household date they represented.
    """
    try:
        now = _aware_now(now)
        start = _stamp(item.get("display_start") or item.get("start"))
        end = _stamp(item.get("display_end") or item.get("end"))
        if end <= start:
            return None
        if config is None or not isinstance(item.get("timezone"), str):
            return start, end
        zone = ZoneInfo(config.get("timezone") or item["timezone"])
        today = now.astimezone(zone).date()
        meal_day = dinner_meal_date(item, config)
        first = _local(today, _clock(config.get("dinner_start", "14:30")), zone)
        last = _local(today, _clock(config.get("dinner_end", "19:00")), zone)
        if meal_day == today:
            first, last = max(first, start), min(last, end)
        elif meal_day != today + timedelta(days=1):
            return None
        return (first, last) if first < last else None
    except (ValueError, TypeError, KeyError, OverflowError, ZoneInfoNotFoundError):
        return None


def personal_card_visible(item, config, now):
    """Pure per-frame gate shared by storage, policy and both display paths."""
    if (not isinstance(item, dict) or item.get("kind") not in {"appointment", "dinner"}
            or item.get("cancelled") is True or config is not None and config.get("personal_enabled", True) is not True):
        return False
    try:
        now = _aware_now(now)
        window = (dinner_display_window(item, config, now) if item["kind"] == "dinner" else
                  (_stamp(item.get("display_start") or item.get("start")),
                   _stamp(item.get("display_end") or item.get("end"))))
        return bool(window and window[0] <= now < window[1])
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


class PersonalStore:
    def __init__(self, data_dir, config_getter):
        self.data_dir = Path(data_dir)
        self.config_getter = config_getter
        self.path = self.data_dir / "personal.json"
        self.details_path = self.data_dir / "personal-dinner-details.json"
        self.lock_path = self.data_dir / "personal.lock"
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self):
        try:
            with self._thread_lock:
                self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.data_dir.chmod(0o700)
                # The lock inode is stable while personal.json is atomically replaced.
                # A descriptor is opened for each transaction, including after fork.
                descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                acquired = False
                try:
                    if hasattr(os, "fchmod"):
                        os.fchmod(descriptor, 0o600)
                    else:
                        self.lock_path.chmod(0o600)
                    if fcntl is not None:
                        fcntl.flock(descriptor, fcntl.LOCK_EX)
                    elif msvcrt is not None:
                        # Windows permits locking beyond EOF. Initializing the
                        # byte before taking the lock races another process
                        # which may already hold that same byte range.
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                    else:
                        raise PersonalError("This platform does not provide file locking for personal cards.")
                    acquired = True
                    yield
                finally:
                    try:
                        if acquired and fcntl is not None:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                        elif acquired and msvcrt is not None:
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    finally:
                        os.close(descriptor)
        except OSError:
            raise PersonalError("Personal-card storage is unavailable. Check its local directory and file permissions.") from None

    def _read(self):
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Oversized store")
            def invalid_constant(_value):
                raise ValueError("Nonfinite JSON")
            state = json.loads(raw, parse_constant=invalid_constant)
            if not isinstance(state, dict) or state.get("schema_version") != 1:
                raise ValueError("Unsupported store format")
            if not isinstance(state.get("items"), list) or not isinstance(state.get("requests"), dict):
                raise ValueError("Invalid card lists")
            if len(state["items"]) > MAX_ITEMS or len(state["requests"]) > MAX_REQUESTS:
                raise ValueError("Oversized card lists")
            seen = set()
            for item in state["items"]:
                self._check_item(item)
                if item["id"] in seen:
                    raise ValueError("Duplicate stored card")
                seen.add(item["id"])
            for request_id, record in state["requests"].items():
                if not re.fullmatch(r"[a-f0-9]{64}", request_id) or not isinstance(record, dict):
                    raise ValueError("Invalid request ledger")
                if not re.fullmatch(r"[a-f0-9]{64}", record.get("payload_hash", "")):
                    raise ValueError("Invalid request hash")
                _stamp(record["created_at"])
                self._check_item(record["item"])
            return state
        except FileNotFoundError:
            return {"schema_version": 1, "items": [], "requests": {}}
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise PersonalError("The personal-card file could not be read. Restore its backup or repair it locally; existing data was not changed.") from None

    @staticmethod
    def _check_item(item):
        if not isinstance(item, dict) or not _ITEM_REQUIRED.issubset(item) or set(item) - (_ITEM_REQUIRED | {"cancelled_at"}):
            raise ValueError("Invalid stored card")
        if not isinstance(item["id"], str) or not re.fullmatch(r"[a-f0-9]{24}", item["id"]):
            raise ValueError("Invalid stored ID")
        if item["kind"] not in {"appointment", "dinner"} or item["source"] not in {"alexa", "manual"}:
            raise ValueError("Invalid stored card type")
        _title(item["title"])
        ZoneInfo(item["timezone"])
        if not isinstance(item["cancelled"], bool):
            raise ValueError("Invalid cancellation flag")
        times = {key: _stamp(item[key]) for key in _STAMP_KEYS}
        if times["end"] <= times["start"] or times["display_end"] <= times["display_start"]:
            raise ValueError("Invalid card interval")
        if "cancelled_at" in item:
            _stamp(item["cancelled_at"])

    @staticmethod
    def _check_details(item, details):
        if not details or set(details) - _DINNER_STORED or item["kind"] != "dinner":
            raise ValueError("Dinner details on another card type")
        if "sides" in details:
            _sides(details["sides"])
        if "ready_until" in details and "ready_at" not in details:
            raise ValueError("Incomplete ready window")
        ready = {key: _stamp(details[key]) for key in ("ready_at", "ready_until") if key in details}
        zone = ZoneInfo(item["timezone"])
        meal_day = _stamp(item["start"]).astimezone(zone).date()
        if any(value.astimezone(zone).date() != meal_day for value in ready.values()):
            raise ValueError("Ready time must belong to the saved meal date")
        if "ready_until" in ready and ready["ready_until"] <= ready["ready_at"]:
            raise ValueError("Invalid ready window")

    @staticmethod
    def _core_fingerprint(item):
        # Cancellation changes do not move the original meal or its details.
        # Title/date/source changes, reused IDs and hand edits cannot inherit it.
        core = {key: item[key] for key in sorted(_ITEM_REQUIRED - {"cancelled"})}
        return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=True, allow_nan=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    def _read_details(self, *, strict=False):
        empty = {"schema_version": 1, "items": {}}
        try:
            with self.details_path.open("rb") as stream:
                raw = stream.read(MAX_DETAILS_BYTES + 1)
            if len(raw) > MAX_DETAILS_BYTES:
                raise ValueError("Oversized dinner details")
            document = json.loads(raw)
            if (not isinstance(document, dict) or set(document) != {"schema_version", "items"}
                    or type(document["schema_version"]) is not int or document["schema_version"] != 1
                    or not isinstance(document["items"], dict) or len(document["items"]) > MAX_DINNER_DETAILS):
                raise ValueError("Invalid dinner details")
            for ident, entry in document["items"].items():
                if (not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{24}", ident)
                        or not isinstance(entry, dict) or set(entry) - ({"core_sha256"} | _DINNER_STORED)
                        or not _DINNER_STORED.intersection(entry)
                        or not isinstance(entry.get("core_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", entry["core_sha256"])):
                    raise ValueError("Invalid dinner detail entry")
                if "sides" in entry:
                    _sides(entry["sides"])
                clocks = {key: _stamp(entry[key]) for key in ("ready_at", "ready_until") if key in entry}
                if "ready_until" in clocks and ("ready_at" not in clocks or clocks["ready_until"] <= clocks["ready_at"]):
                    raise ValueError("Invalid dinner ready window")
            return document
        except FileNotFoundError:
            return empty
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            if strict:
                raise PersonalError("Dinner details could not be saved safely. Existing menus remain unchanged; repair the private details file locally.") from None
            # Optional details must never make the recovery release's core
            # meals unreadable, nor invent sides/times from a damaged cache.
            return empty

    def _with_details(self, item, document):
        result = copy.deepcopy(item)
        entry = document["items"].get(item["id"])
        if not entry or entry.get("core_sha256") != self._core_fingerprint(item):
            return result
        details = {key: entry[key] for key in _DINNER_STORED if key in entry}
        try:
            self._check_details(item, details)
        except (ValueError, TypeError, KeyError, OverflowError, ZoneInfoNotFoundError):
            return result
        return result | copy.deepcopy(details)

    def _write_document(self, path, state):
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".personal-", suffix=".tmp", dir=self.data_dir)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                Path(temporary).chmod(0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            if hasattr(os, "O_DIRECTORY"):
                parent = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        except (OSError, ValueError, TypeError):
            raise PersonalError("The personal card could not be saved. Check free disk space and try again.") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _write(self, state):
        self._write_document(self.path, state)

    def _save_details(self, state, item, details):
        document = self._read_details(strict=True)
        retained = {row["id"] for row in state["items"]}
        retained.update(row["item"]["id"] for row in state["requests"].values())
        entries = {ident: entry for ident, entry in document["items"].items() if ident in retained}
        if item["id"] in retained:
            self._check_details(item, details)
            entries[item["id"]] = {"core_sha256": self._core_fingerprint(item), **details}
        if len(entries) > MAX_DINNER_DETAILS:
            raise PersonalError("Dinner detail history is full; no menu was changed.")
        document = {"schema_version": 1, "items": entries}
        encoded = json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
        if len(encoded) + 1 > MAX_DETAILS_BYTES:
            raise PersonalError("Dinner detail history is full; no menu was changed.")
        self._write_document(self.details_path, document)

    @staticmethod
    def _prune(state, now):
        state["items"] = [item for item in state["items"] if _stamp(item["display_end"]) > now]
        state["requests"] = {key: record for key, record in state["requests"].items()
                             if _stamp(record["created_at"]) + REQUEST_TTL > now}

    @staticmethod
    def _cancel_item(state, item, now):
        item["cancelled"] = True
        item["cancelled_at"] = _iso(now)
        for record in state["requests"].values():
            if record["item"]["id"] == item["id"]:
                record["item"] = copy.deepcopy(item)

    def _context(self):
        config = dict(self.config_getter())
        zone_name = config.get("timezone", "UTC")
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            raise PersonalError("Set the display timezone to an IANA name such as America/New_York or UTC.") from None
        duration = _duration(config.get("appointment_duration_minutes", 60))
        start, end = config.get("dinner_start", "14:30"), config.get("dinner_end", "19:00")
        start_clock, end_clock = _clock(start), _clock(end)
        if end_clock <= start_clock:
            raise PersonalError("Set dinner_end later than dinner_start on the same day.")
        return config, zone, duration, start, end

    def voice_context(self):
        _config, zone, duration, start, end = self._context()
        return {"timezone": zone.key, "appointment_duration_minutes": duration,
                "dinner_start": start, "dinner_end": end, "dinner_tomorrow_preview": True,
                "dinner_requires_kids_home": _config.get("kids_filter_enabled") is True}

    @staticmethod
    def _intent(payload):
        if not isinstance(payload, dict) or set(payload) - _ALLOWED:
            raise PersonalError("Use only the supported personal-card fields; dinner may include sides and an exact ready time or ready window.")
        kind = payload.get("kind")
        if not isinstance(kind, str) or kind not in {"appointment", "dinner"}:
            raise PersonalError("Choose card kind appointment or dinner.")
        source = payload.get("source", "manual")
        if not isinstance(source, str) or source.strip().casefold() not in {"alexa", "manual"}:
            raise PersonalError("Personal-card source must be alexa or manual.")
        result = {"kind": kind, "title": _title(payload.get("title")), "source": source.strip().casefold()}
        if "date" in payload:
            result["date"] = _date(payload["date"]).isoformat()
        elif kind == "appointment":
            raise PersonalError("Give the appointment date as YYYY-MM-DD.")
        if kind == "appointment":
            if _DINNER_INPUT.intersection(payload):
                raise PersonalError("Sides and expected-ready times apply only to dinner cards.")
            result["time"] = _clock(payload.get("time")).strftime("%H:%M")
            if "duration_minutes" in payload:
                result["duration_minutes"] = _duration(payload["duration_minutes"])
        elif "time" in payload or "duration_minutes" in payload:
            raise PersonalError("Dinner uses the saved dinner_start and dinner_end settings; omit time and duration_minutes.")
        else:
            if "sides" in payload:
                result["sides"] = _sides(payload["sides"])
            has_exact = "ready_time" in payload
            window = {"ready_window_start", "ready_window_end"}.intersection(payload)
            if has_exact and window:
                raise PersonalError("Give either one expected-ready time or a complete ready window, not both.")
            if window and len(window) != 2:
                raise PersonalError("Give both ready_window_start and ready_window_end, or omit the window.")
            if has_exact:
                result["ready_time"] = _clock(payload["ready_time"]).strftime("%H:%M")
            elif window:
                result.update({key: _clock(payload[key]).strftime("%H:%M") for key in window})
                if result["ready_window_end"] <= result["ready_window_start"]:
                    raise PersonalError("Expected-ready window must end later on the same meal date.")
        request_id = payload.get("request_id")
        if "request_id" in payload and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 512
                                        or not request_id.strip() or any(ord(char) < 32 or ord(char) == 127
                                            or 0xD800 <= ord(char) <= 0xDFFF for char in request_id)):
            raise PersonalError("Use a nonempty request_id of at most 512 characters.")
        return result, request_id

    @staticmethod
    def _speech(item, now, duplicate=False):
        if duplicate:
            if item["cancelled"]:
                return "That request was already saved and its card was cancelled. It has not been added again."
            return "That request was already saved. No extra card was added."
        local_start = _stamp(item["start"]).astimezone(ZoneInfo(item["timezone"]))
        if item["kind"] == "dinner":
            if _stamp(item["display_end"]) <= now:
                return f"Saved dinner: {item['title']}. The dinner window for {local_start.date().isoformat()} has ended, so this card will not appear."
            end = _stamp(item["end"]).astimezone(ZoneInfo(item["timezone"]))
            return f"Saved dinner: {item['title']}. It will appear on {local_start.date().isoformat()} from {local_start.strftime('%H:%M')} to {end.strftime('%H:%M')} local time."
        local_end = _stamp(item["end"]).astimezone(ZoneInfo(item["timezone"]))
        end_description = local_end.strftime("%H:%M")
        if local_end.date() != local_start.date():
            end_description += " on " + local_end.date().isoformat()
        return f"Added {item['title']} on {local_start.date().isoformat()} at {local_start.strftime('%H:%M')}. It will stay on the display until {end_description}, when the appointment ends."

    def create(self, payload):
        intent, request_id = self._intent(payload)
        payload_hash = hashlib.sha256(json.dumps(intent, sort_keys=True, ensure_ascii=True, allow_nan=False,
                                                 separators=(",", ":")).encode("utf-8")).hexdigest()
        request_key = hashlib.sha256(request_id.encode("utf-8")).hexdigest() if request_id is not None else None
        with self._locked():
            now = _aware_now()
            state = self._read()
            self._prune(state, now)
            if request_key in state["requests"]:
                record = state["requests"][request_key]
                if record["payload_hash"] != payload_hash:
                    raise PersonalError("This request_id was already used for different card details. Use a new request_id for a new card.")
                item = self._with_details(record["item"], self._read_details())
                return {"created": False, "item": item, "speech": self._speech(item, now, duplicate=True)}
            _config, zone, default_duration, dinner_start, dinner_end = self._context()
            day = _date(intent["date"]) if "date" in intent else now.astimezone(zone).date()
            if intent["kind"] == "appointment":
                start = _local(day, _clock(intent["time"]), zone)
                try:
                    end = start + timedelta(minutes=intent.get("duration_minutes", default_duration))
                except OverflowError:
                    raise PersonalError("That appointment ends outside the supported time range. Choose a nearer date.") from None
                display_start = _local(day, time(0), zone)
                display_end = end
                if end <= now:
                    raise PersonalError("That appointment has already ended. Check the date and local time, then try again.")
            else:
                start = display_start = _local(day, _clock(dinner_start), zone)
                end = display_end = _local(day, _clock(dinner_end), zone)
            details = {}
            if intent["kind"] == "dinner":
                if "sides" in intent:
                    details["sides"] = intent["sides"]
                if "ready_time" in intent:
                    details["ready_at"] = _iso(_local(day, _clock(intent["ready_time"]), zone))
                elif "ready_window_start" in intent:
                    details["ready_at"] = _iso(_local(day, _clock(intent["ready_window_start"]), zone))
                    details["ready_until"] = _iso(_local(day, _clock(intent["ready_window_end"]), zone))
            unexpired = display_end > now
            matching_dinners = [previous for previous in state["items"]
                                if intent["kind"] == "dinner" and previous["kind"] == "dinner"
                                and not previous["cancelled"]
                                and _stamp(previous["start"]).astimezone(ZoneInfo(previous["timezone"])).date() == day]
            active_count = sum(not previous["cancelled"] for previous in state["items"]) - len(matching_dinners)
            if unexpired and active_count >= MAX_ITEMS:
                raise PersonalError("There are already 1000 unexpired personal cards. Wait for older cards to expire before adding another.")
            if request_key is not None and len(state["requests"]) >= MAX_REQUESTS:
                raise PersonalError("The 30-day request history is full. Retry after older requests expire; no card was added.")
            item = {"id": secrets.token_hex(12), "kind": intent["kind"], "title": intent["title"],
                    "source": intent["source"], "timezone": zone.key, "start": _iso(start), "end": _iso(end),
                    "display_start": _iso(display_start), "display_end": _iso(display_end),
                    "created_at": _iso(now), "cancelled": False}
            replaced = []
            for previous in matching_dinners:
                self._cancel_item(state, previous, now)
                replaced.append(previous["id"])
            if unexpired:
                state["items"].append(item)
            # Preserve cancellation rows normally, but they must not prevent
            # capacity from being reused. Request snapshots retain cancellation
            # even when a cancelled row is compacted out of the bounded list.
            while len(state["items"]) > MAX_ITEMS:
                cancelled_index = next(index for index, previous in enumerate(state["items"]) if previous["cancelled"])
                del state["items"][cancelled_index]
            if request_key is not None:
                state["requests"][request_key] = {"payload_hash": payload_hash, "created_at": _iso(now), "item": copy.deepcopy(item)}
            if details:
                # Commit optional data first. If the core write then fails,
                # its orphan is invisible and the next rich save prunes it.
                self._save_details(state, item, details)
            self._write(state)
            projected = copy.deepcopy(item) | copy.deepcopy(details)
            return {"created": True, "item": projected, "replaced": replaced, "speech": self._speech(projected, now)}

    def cancel(self, item_id):
        if not isinstance(item_id, str) or not re.fullmatch(r"[a-f0-9]{24}", item_id):
            raise PersonalError("Use the personal card ID returned by the list operation.")
        with self._locked():
            now = _aware_now()
            state = self._read()
            self._prune(state, now)
            item = next((item for item in state["items"] if item["id"] == item_id), None)
            if item is None:
                raise PersonalError("That personal card was not found or has expired. List current cards to find its ID.")
            if item["cancelled"]:
                return {"cancelled": False, "item": self._with_details(item, self._read_details()), "speech": "That personal card was already cancelled."}
            self._cancel_item(state, item, now)
            self._write(state)
            return {"cancelled": True, "item": self._with_details(item, self._read_details()), "speech": "Personal card cancelled."}

    def list_items(self, now=None, *, include_cancelled=False):
        now = _aware_now(now)
        with self._locked():
            state = self._read()
            details = self._read_details()
        items = [item for item in state["items"] if _stamp(item["display_end"]) > now
                 and (include_cancelled or not item["cancelled"])]
        return [self._with_details(item, details) for item in sorted(items, key=lambda item: (_stamp(item["start"]), item["created_at"], item["id"]))]

    def visible(self, now=None):
        now = _aware_now(now)
        config = dict(self.config_getter())
        if not config.get("personal_enabled", True):
            return []
        return [item for item in self.list_items(now) if personal_card_visible(item, config, now)]
