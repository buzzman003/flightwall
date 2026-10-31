"""Opt-in shopping arrivals and bounded, at-most-once daily SMS dispatch.

No native Alexa list access is available: Amazon retired that API in 2024.
Coordinates are used only in the current call and are never persisted. Call
``consume_location`` only after authenticating and accepting Lisa's phone report.
An uncertain network outcome consumes the day's attempt; automatic retries
could otherwise send the same message twice. Provider acceptance is not delivery.
"""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

UTC = timezone.utc
DAY_ZONE = "America/Chicago"
MAX_FIX_AGE = timedelta(minutes=5)
MAX_FUTURE_SKEW = timedelta(seconds=120)
MAX_MESSAGE_CHARS = 1400
MAX_STORES = 100
MAX_FILE_BYTES = 64 * 1024
MAX_LIST_FILE_BYTES = 2 * 1024 * 1024
MAX_LIST_REQUESTS = 1000
LIST_REQUEST_TTL = timedelta(days=30)


class ShoppingError(ValueError):
    """A safe message, never including a location, recipient, or credential."""


class ShoppingUnavailable(ShoppingError):
    pass


def _now(value=None):
    value = datetime.now(UTC) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ShoppingError("Use a timezone-aware current time.")
    return value.astimezone(UTC)


def _stamp(value):
    if not isinstance(value, str):
        raise ShoppingError("Shopping state contains an invalid timestamp.")
    try:
        return _now(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError, OverflowError):
        raise ShoppingError("Shopping state contains an invalid timestamp.") from None


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _read_json(path, default=None, max_bytes=MAX_FILE_BYTES):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError
        return json.loads(raw)
    except FileNotFoundError:
        return default
    except (OSError, ValueError, UnicodeError):
        raise ShoppingError("Shopping storage is unavailable or invalid; no message was sent.") from None


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".shopping-", dir=path.parent)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            Path(temporary).chmod(0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows indexers/antivirus may briefly hold the old file.
                # Retry only this local atomic rename, never an SMS request.
                if os.name != "nt" or attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
        temporary = None
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except (OSError, ValueError, TypeError):
        raise ShoppingError("Shopping state could not be saved; stop and check its private storage.") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _items(values):
    if not isinstance(values, list) or len(values) > 100:
        raise ShoppingError("Use a shopping list with at most 100 items.")
    result = []
    for value in values:
        if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 120
                or any(ord(ch) < 32 or ord(ch) in (127, 0x2028, 0x2029)
                       or 0xD800 <= ord(ch) <= 0xDFFF for ch in value)):
            raise ShoppingError("Shopping items must be short single-line text.")
        result.append(value.strip())
    return result


def shopping_message(items):
    items = _items(items)
    if not items:
        raise ShoppingUnavailable("The selected shopping list is empty.")
    message = "Flight Wall shopping list:\n" + "\n".join("- " + item for item in items)
    if len(message) > MAX_MESSAGE_CHARS:
        raise ShoppingUnavailable("The shopping list is too long for the configured SMS limit.")
    return message


class AlexaShoppingListUnavailable:
    source_id = "alexa"

    def get_items(self):
        raise ShoppingUnavailable(
            "Amazon retired third-party access to Alexa shopping lists. Choose a supported list source first.")


class ManualShoppingList:
    """An explicitly selected local list, never represented as an Alexa sync."""
    source_id = "manual"

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "shopping" / "list.json"

    def set_items(self, items):
        _write_json(self.path, {"version": 1, "items": _items(items)})

    def get_items(self):
        state = _read_json(self.path, {})
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ShoppingUnavailable("Configure the selected local shopping list first.")
        return _items(state.get("items"))


@contextmanager
def _file_lock(directory, filename, thread_lock):
    with thread_lock:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        descriptor = os.open(directory / filename, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            elif msvcrt is not None:
                # Windows supports locking beyond EOF. Writing an initial byte
                # before acquiring the lock races a holder of this byte range.
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                raise ShoppingError("Shopping storage requires local file locking.")
            acquired = True
            yield
        finally:
            if acquired and fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif acquired and msvcrt is not None:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)


def _item_name(value):
    value = _items([value])[0]
    return " ".join(unicodedata.normalize("NFC", value).split())


class ShoppingListStore:
    """Owner-selected Flight Wall list. It never reads Alexa's native list."""
    source_id = "flightwall"

    def __init__(self, data_dir):
        self.directory = Path(data_dir) / "shopping"
        self.path = self.directory / "flightwall-list.json"
        self._thread_lock = threading.RLock()

    def _locked(self):
        return _file_lock(self.directory, ".list.lock", self._thread_lock)

    @staticmethod
    def _check_item(item):
        if (not isinstance(item, dict) or set(item) != {"id", "name", "source", "created_at"}
                or not isinstance(item["id"], str) or not re.fullmatch(r"[a-f0-9]{24}", item["id"])
                or item["source"] not in ("manual", "alexa")
                or _item_name(item["name"]) != item["name"]):
            raise ShoppingError("The private shopping list is invalid; check its storage locally.")
        _stamp(item["created_at"])

    def _read(self):
        state = _read_json(self.path, {"version": 1, "items": [], "requests": {}, "updated_at": None},
                           max_bytes=MAX_LIST_FILE_BYTES)
        if (not isinstance(state, dict) or set(state) != {"version", "items", "requests", "updated_at"}
                or state["version"] != 1 or not isinstance(state["items"], list) or len(state["items"]) > 100
                or not isinstance(state["requests"], dict) or len(state["requests"]) > MAX_LIST_REQUESTS):
            raise ShoppingError("The private shopping list is invalid; check its storage locally.")
        if state["updated_at"] is not None:
            _stamp(state["updated_at"])
        ids, names = set(), set()
        for item in state["items"]:
            self._check_item(item)
            if item["id"] in ids or item["name"].casefold() in names:
                raise ShoppingError("The private shopping list contains duplicate items.")
            ids.add(item["id"])
            names.add(item["name"].casefold())
        for key, record in state["requests"].items():
            if (not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key)
                    or not isinstance(record, dict) or set(record) != {"intent_hash", "created_at", "result"}
                    or not isinstance(record["intent_hash"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", record["intent_hash"])):
                raise ShoppingError("The shopping request ledger is invalid; check it locally.")
            _stamp(record["created_at"])
            result = record["result"]
            if (not isinstance(result, dict) or set(result) != {"action", "changed", "item"}
                    or result["action"] not in ("add", "remove", "clear") or type(result["changed"]) is not bool):
                raise ShoppingError("The shopping request ledger is invalid; check it locally.")
            if result["item"] is not None:
                self._check_item(result["item"])
        return state

    @staticmethod
    def _intent(action, payload):
        allowed = {"request_id", "source"} | ({"item"} if action == "add" else {"item", "id"}
                                                if action == "remove" else {"confirmation"})
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ShoppingError("Use only the supported shopping action fields.")
        source = payload.get("source", "manual")
        if source not in ("manual", "alexa"):
            raise ShoppingError("Choose shopping source manual or alexa.")
        intent = {"action": action, "source": source}
        if action == "add":
            intent["item"] = _item_name(payload.get("item"))
        elif action == "remove":
            if ("item" in payload) == ("id" in payload):
                raise ShoppingError("Remove one shopping item by its name or its ID.")
            if "id" in payload:
                if not isinstance(payload["id"], str) or not re.fullmatch(r"[a-f0-9]{24}", payload["id"]):
                    raise ShoppingError("Use the item ID returned by the shopping list.")
                intent["id"] = payload["id"]
            else:
                intent["item"] = _item_name(payload["item"])
        elif payload.get("confirmation") is not True:
            raise ShoppingError("Confirm before clearing the shopping list.")
        request_id = payload.get("request_id")
        if "request_id" in payload and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 512
                                        or not request_id.strip() or any(ord(ch) < 32 or ord(ch) == 127
                                            or 0xD800 <= ord(ch) <= 0xDFFF for ch in request_id)):
            raise ShoppingError("Use a nonempty request_id of at most 512 characters.")
        return intent, request_id

    @staticmethod
    def _result(result, duplicate=False):
        result = copy.deepcopy(result)
        result["duplicate"] = duplicate
        if duplicate:
            result["changed"] = False
            speech = "That shopping request was already handled."
        elif result["action"] == "add":
            speech = "Added to your shopping list." if result["changed"] else "That's already on your shopping list."
        elif result["action"] == "remove":
            speech = "Removed from your shopping list." if result["changed"] else "That item isn't on your shopping list."
        else:
            speech = "Shopping list cleared." if result["changed"] else "Your shopping list is already empty."
        result["speech"] = speech
        return result

    def _mutate(self, action, payload, now=None):
        intent, request_id = self._intent(action, payload)
        now = _now(now)
        request_key = hashlib.sha256(request_id.encode()).hexdigest() if request_id is not None else None
        intent_hash = hashlib.sha256(json.dumps(intent, sort_keys=True, ensure_ascii=True,
                                                separators=(",", ":")).encode()).hexdigest()
        with self._locked():
            state = self._read()
            state["requests"] = {key: record for key, record in state["requests"].items()
                                 if _stamp(record["created_at"]) > now - LIST_REQUEST_TTL}
            if request_key in state["requests"]:
                record = state["requests"][request_key]
                if record["intent_hash"] != intent_hash:
                    raise ShoppingError("This request_id was already used for different shopping details.")
                return self._result(record["result"], duplicate=True)
            if request_key is not None and len(state["requests"]) >= MAX_LIST_REQUESTS:
                raise ShoppingUnavailable("The shopping request limit is reached; no list change was made.")
            item = next((item for item in state["items"] if item["id"] == intent.get("id")
                         or item["name"].casefold() == intent.get("item", "").casefold()), None)
            changed = False
            if action == "add" and item is None:
                if len(state["items"]) >= 100:
                    raise ShoppingUnavailable("The shopping list is full; remove an item before adding more.")
                item = {"id": secrets.token_hex(12), "name": intent["item"],
                        "source": intent["source"], "created_at": _iso(now)}
                state["items"].append(item)
                changed = True
            elif action == "remove" and item is not None:
                state["items"].remove(item)
                changed = True
            elif action == "clear":
                changed = bool(state["items"])
                state["items"] = []
            result = {"action": action, "changed": changed, "item": item}
            if changed:
                state["updated_at"] = _iso(now)
            if request_key is not None:
                state["requests"][request_key] = {"intent_hash": intent_hash, "created_at": _iso(now),
                                                 "result": copy.deepcopy(result)}
            _write_json(self.path, state)
            return self._result(result)

    def add(self, payload, now=None):
        return self._mutate("add", payload, now)

    def remove(self, payload, now=None):
        return self._mutate("remove", payload, now)

    def clear(self, payload, now=None):
        return self._mutate("clear", payload, now)

    def snapshot(self):
        with self._locked():
            state = self._read()
        return copy.deepcopy({"items": state["items"], "updated_at": state["updated_at"]})

    def get_items(self):
        return [item["name"] for item in self.snapshot()["items"]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class TwilioSmsSender:
    """One fixed-host HTTPS POST. Does not register an account or buy a number.

    Private shopping/sms.json contains account_sid, api_key_sid, api_key_secret,
    messaging_service_sid, and to_number. No fields are returned in status/errors.
    """
    provider_id = "twilio"

    def __init__(self, data_dir, *, opener=None):
        self.path = Path(data_dir) / "shopping" / "sms.json"
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    def _credentials(self):
        value = _read_json(self.path, {})
        patterns = {"account_sid": r"AC[0-9a-fA-F]{32}",
                    "api_key_sid": r"SK[0-9a-fA-F]{32}",
                    "messaging_service_sid": r"MG[0-9a-fA-F]{32}",
                    "to_number": r"\+[1-9][0-9]{7,14}"}
        if not isinstance(value, dict) or any(
                not isinstance(value.get(key), str) or not re.fullmatch(pattern, value[key])
                for key, pattern in patterns.items()):
            raise ShoppingUnavailable("Configure the private SMS sender and Lisa's approved recipient first.")
        secret = value.get("api_key_secret")
        if not isinstance(secret, str) or not 16 <= len(secret) <= 256 or any(ord(ch) <= 32 for ch in secret):
            raise ShoppingUnavailable("The private SMS credential is missing or invalid.")
        return value

    def ready(self):
        try:
            self._credentials()
            return True
        except ShoppingError:
            return False

    def send(self, message, *, request_id):
        # request_id is an application reservation identifier. Twilio's create
        # Message API is not assumed to implement idempotency for this field.
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{64}", request_id):
            raise ShoppingError("Invalid shopping dispatch reservation.")
        if not isinstance(message, str) or not 1 <= len(message) <= MAX_MESSAGE_CHARS:
            raise ShoppingError("Shopping message exceeds its configured limit.")
        credentials = self._credentials()
        endpoint = "https://api.twilio.com/2010-04-01/Accounts/" + credentials["account_sid"] + "/Messages.json"
        body = urllib.parse.urlencode({"To": credentials["to_number"],
                                      "MessagingServiceSid": credentials["messaging_service_sid"],
                                      "Body": message}).encode("utf-8")
        auth = base64.b64encode((credentials["api_key_sid"] + ":" + credentials["api_key_secret"]).encode()).decode()
        request = urllib.request.Request(endpoint, data=body, method="POST", headers={
            "Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=5) as response:
                if response.status != 201:
                    return {"status": "unconfirmed"}
                raw = response.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                return {"status": "unconfirmed"}
            result = json.loads(raw)
            if not isinstance(result, dict) or not re.fullmatch(r"SM[0-9a-fA-F]{32}", str(result.get("sid", ""))):
                return {"status": "unconfirmed"}
            return {"status": "accepted"}
        except Exception:
            # Do not expose provider error bodies, request credentials, or text.
            # An error may follow provider acceptance: never retry automatically.
            return {"status": "unconfirmed"}


def _distance(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi, delta_lon = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lon / 2) ** 2
    return 6371008.8 * 2 * math.atan2(math.sqrt(max(0, a)), math.sqrt(max(0, 1 - a)))


def _fences(values):
    if isinstance(values, str):
        if len(values) > MAX_FILE_BYTES:
            raise ShoppingError("Configure a bounded list of verified store geofences.")
        try:
            values = json.loads(values)
        except (ValueError, TypeError):
            raise ShoppingError("Configure a bounded list of verified store geofences.") from None
    if not isinstance(values, list) or len(values) > MAX_STORES:
        raise ShoppingError("Configure a bounded list of verified store geofences.")
    result, identifiers = [], set()
    for value in values:
        if (not isinstance(value, dict) or set(value) - {"id", "latitude", "longitude", "radius_m", "verified"}
                or value.get("verified") is not True):
            raise ShoppingError("Every enabled store needs a verified geofence center.")
        identifier = value.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", identifier) or identifier in identifiers:
            raise ShoppingError("Store geofence identifiers must be unique.")
        lat, lon, radius = value.get("latitude"), value.get("longitude"), value.get("radius_m", 50)
        if not _number(lat, -90, 90) or not _number(lon, -180, 180) or not _number(radius, 30, 1000):
            raise ShoppingError("Store geofence coordinates or radius are invalid.")
        identifiers.add(identifier)
        result.append((lat, lon, radius))
    return result


def validate_stores(values):
    """Validate only enabled, verified circles; public candidate records are not config."""
    _fences(values)
    return values


class ShoppingAlerts:
    def __init__(self, data_dir, config_getter, *, list_source=None, sms_sender=None):
        self.directory = Path(data_dir) / "shopping"
        self.path = self.directory / "state.json"
        self.config_getter = config_getter
        self.list_source = list_source
        self.sms_sender = sms_sender
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self):
        with _file_lock(self.directory, ".lock", self._thread_lock):
            yield

    def _read(self):
        empty = {"version": 1, "last_observed_at": None, "inside": None,
                 "pending_arrival_date": None, "dispatch_date": None, "dispatch_status": None}
        value = _read_json(self.path, empty)
        if not isinstance(value, dict) or set(value) != set(empty) or value.get("version") != 1:
            raise ShoppingError("Shopping dispatch state is invalid; no message was sent.")
        if value["last_observed_at"] is not None:
            _stamp(value["last_observed_at"])
        if value["inside"] is not None and type(value["inside"]) is not bool:
            raise ShoppingError("Shopping dispatch state is invalid; no message was sent.")
        for key in ("pending_arrival_date", "dispatch_date"):
            if value[key] is not None:
                try:
                    if date.fromisoformat(value[key]).isoformat() != value[key]:
                        raise ValueError
                except (ValueError, TypeError):
                    raise ShoppingError("Shopping dispatch state is invalid; no message was sent.") from None
        if value["dispatch_status"] not in (None, "reserved", "accepted", "unconfirmed"):
            raise ShoppingError("Shopping dispatch state is invalid; no message was sent.")
        if (value["dispatch_status"] is None) != (value["dispatch_date"] is None):
            raise ShoppingError("Shopping dispatch state is invalid; no message was sent.")
        return value

    def status(self):
        """Sanitized configuration/delivery state; no Lisa location or list data."""
        config = self.config_getter()
        result = {"enabled": config.get("shopping_enabled") is True,
                  "consented": config.get("shopping_lisa_opt_in") is True,
                  "list_source": config.get("shopping_list_source", "alexa"),
                  "native_alexa_list_supported": False,
                  "last_dispatch_date": None, "last_dispatch_status": None}
        if result["list_source"] not in ("alexa", "manual", "flightwall"):
            result["list_source"] = "external"
        if not self.path.exists():
            return result
        try:
            with self._locked():
                state = self._read()
            result.update(last_dispatch_date=state["dispatch_date"], last_dispatch_status=state["dispatch_status"])
        except (ShoppingError, OSError):
            result["error"] = "Shopping dispatch storage needs local attention."
        return result

    def consume_location(self, person, payload, now=None):
        """Evaluate an already authenticated phone report; never call for manual presence."""
        config = self.config_getter()
        if (config.get("shopping_enabled") is not True or config.get("shopping_lisa_opt_in") is not True
                or config.get("presence_enabled") is not True):
            return {"status": "disabled"}
        if person != "lisa":
            return {"status": "wrong_person"}
        if not isinstance(payload, dict) or payload.get("_type") != "location":
            return {"status": "unsupported_observation"}
        now = _now(now)
        lat, lon, accuracy, epoch = (payload.get(key) for key in ("lat", "lon", "acc", "tst"))
        if (not _number(lat, -90, 90) or not _number(lon, -180, 180)
                or not _number(accuracy, 0, 100000) or not _number(epoch, 0, 253402300799)):
            raise ShoppingError("A store arrival needs valid coordinates, accuracy, and a phone timestamp.")
        try:
            observed = datetime.fromtimestamp(epoch, UTC)
        except (ValueError, OverflowError, OSError):
            raise ShoppingError("The phone timestamp is invalid.") from None
        if observed > now + MAX_FUTURE_SKEW or observed <= now - MAX_FIX_AGE:
            return {"status": "stale_or_future_observation"}
        fences = _fences(config.get("shopping_stores", []))
        if not fences:
            return {"status": "stores_unconfigured"}
        distances = [(_distance(lat, lon, a, b), radius) for a, b, radius in fences]
        inside = (True if any(distance + accuracy <= radius for distance, radius in distances)
                  else False if all(distance - accuracy > radius for distance, radius in distances) else None)
        local_day = now.astimezone(ZoneInfo(DAY_ZONE)).date().isoformat()
        with self._locked():
            state = self._read()
            if state["last_observed_at"] is not None and observed <= _stamp(state["last_observed_at"]):
                return {"status": "duplicate_or_old_observation"}
            state["last_observed_at"] = _iso(observed)
            if inside is None:
                _write_json(self.path, state)
                return {"status": "uncertain_location"}
            if inside and state["inside"] is not True:
                state["pending_arrival_date"] = local_day
            elif not inside:
                state["pending_arrival_date"] = None
            state["inside"] = inside
            _write_json(self.path, state)
            if not inside:
                return {"status": "outside"}
            if state["dispatch_date"] is not None and state["dispatch_date"] >= local_day:
                return {"status": "daily_limit"}
            if state["pending_arrival_date"] != local_day:
                return {"status": "no_new_arrival"}
            source = self.list_source
            if source is None and config.get("shopping_list_source", "alexa") == "flightwall":
                source = ShoppingListStore(self.directory.parent)
            if source is None and config.get("shopping_list_source", "alexa") == "manual":
                source = ManualShoppingList(self.directory.parent)
            if source is None:
                source = AlexaShoppingListUnavailable()
            if getattr(source, "source_id", None) != config.get("shopping_list_source", "alexa"):
                return {"status": "list_source_unavailable"}
            try:
                message = shopping_message(source.get_items())
            except ShoppingUnavailable:
                return {"status": "list_source_unavailable"}
            sender = self.sms_sender or TwilioSmsSender(self.directory.parent)
            if not sender.ready():
                return {"status": "sms_unconfigured"}
            # Consent may have changed while a provider fetched its list.
            current = self.config_getter()
            if (current.get("shopping_enabled") is not True or current.get("shopping_lisa_opt_in") is not True
                    or current.get("presence_enabled") is not True
                    or current.get("shopping_list_source", "alexa") != config.get("shopping_list_source", "alexa")):
                return {"status": "disabled"}
            # The durable reservation precedes external I/O. A crash, timeout,
            # or provider rejection cannot trigger a second automatic attempt.
            reservation = hashlib.sha256(("flightwall-shopping:lisa:" + local_day).encode()).hexdigest()
            state.update(dispatch_date=local_day, dispatch_status="reserved")
            _write_json(self.path, state)
            try:
                result = sender.send(message, request_id=reservation)
                status = "accepted" if isinstance(result, dict) and result.get("status") == "accepted" else "unconfirmed"
            except Exception:
                status = "unconfirmed"
            state["dispatch_status"] = status
            _write_json(self.path, state)
            return {"status": status}
