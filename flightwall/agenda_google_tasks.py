"""Read the titles of Chris's open Google Tasks using separate, read-only consent."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import threading
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from .calendar_client import CalendarClient, CalendarError
from .config import private_json
from .messages import GoogleReadClient, MessageError, _Unauthorized
from .personal import PersonalStore

MAX_DISPLAY_HISTORY = 16384
MAX_TASKS_ON_BOARD = 40


class _GoogleAccountInfo(GoogleReadClient):
    label = "Google account"
    prefix = "tasks"
    base_url = "https://www.googleapis.com/oauth2/v2"


class TasksClient(GoogleReadClient):
    label = "Google Tasks"
    prefix = "tasks"
    scope = "https://www.googleapis.com/auth/tasks.readonly"
    email_scope = "https://www.googleapis.com/auth/userinfo.email"
    scopes = (scope, email_scope)
    base_url = "https://tasks.googleapis.com/tasks/v1"

    @classmethod
    def granted_scopes(cls, scopes):
        if not isinstance(scopes, (list, tuple)) or not all(isinstance(scope, str) for scope in scopes):
            return False
        granted = {cls.email_scope if scope == "email" else scope for scope in scopes}
        # Google may add this identity marker to an approved email permission.
        # Still require both requested scopes and reject every other addition.
        return granted - {"openid"} == set(cls.scopes)

    def calendar_binding(self):
        """Changing Chris's Calendar grant invalidates the previously verified Tasks identity."""
        try:
            token = self._json(self.account_dir / "google-token.json")
            grant = token["refresh_token"]
            if not isinstance(grant, str) or not 1 <= len(grant) <= 16384:
                raise ValueError()
            return hashlib.sha256(grant.encode("utf-8")).hexdigest()
        except (ValueError, OSError, KeyError):
            raise MessageError("Connect Chris's primary Google Calendar before connecting his Tasks.") from None

    def _client(self):
        client = super()._client()
        if client.get("flightwall_calendar_grant") != self.calendar_binding():
            raise MessageError("Reconnect Chris's Tasks after changing his Calendar connection.")
        return client

    def verified_client(self, client, access):
        binding = self.calendar_binding()
        account = _GoogleAccountInfo(self.account_dir)
        try:
            identity = account._request("GET", account.base_url + "/userinfo?fields=email,verified_email", token=access)
            calendar = CalendarClient(self.account_dir, {})._api(
                "/users/me/calendarList/primary", {"fields": "id,primary"})
            email, primary = identity.get("email"), calendar.get("id")
            if not (identity.get("verified_email") is True and calendar.get("primary") is True
                    and all(isinstance(value, str) and value.isascii() and value.count("@") == 1
                            and value.isprintable() and 3 <= len(value) <= 254 for value in (email, primary))
                    and hmac.compare_digest(email.casefold(), primary.casefold())
                    and self.calendar_binding() == binding):
                raise ValueError()
        except (ValueError, OSError, CalendarError, _Unauthorized):
            raise MessageError("Choose the same Google account used by Chris's connected primary calendar. Existing Tasks are unchanged.") from None
        return {**client, "flightwall_calendar_grant": binding}

    def pages(self, path, params, limit):
        token = None
        seen = set()
        for _ in range(limit):
            query = {**params, "maxResults": 100}
            if token:
                query["pageToken"] = token
            response = self._get(self.base_url + path + "?" + urlencode(query))
            items = response.get("items", [])
            if not isinstance(items, list) or len(items) > 100:
                raise MessageError("Invalid Tasks response.")
            yield from items
            token = response.get("nextPageToken")
            if not token:
                return
            if not isinstance(token, str) or len(token) > 2048 or token in seen:
                raise MessageError("Invalid Tasks page.")
            seen.add(token)
        raise MessageError("The task list exceeds the supported page limit.")

    def unfinished(self):
        rows = {}
        grant = bytes.fromhex(self.calendar_binding())
        for index, group in enumerate(self.pages("/users/@me/lists", {"fields": "nextPageToken,items(id)"}, 1)):
            if index >= 32:
                raise MessageError("Too many task lists.")
            key = group.get("id") if isinstance(group, dict) else None
            if not isinstance(key, str) or not 1 <= len(key) <= 256 or key in {".", ".."}:
                raise MessageError("Invalid task list.")
            path = "/lists/" + quote(key, safe="") + "/tasks"
            params = {"fields": "nextPageToken,items(id,title,status,due,hidden,deleted)", "showCompleted": "false",
                      "showHidden": "false", "showDeleted": "false"}
            for item in self.pages(path, params, 5):
                if (not isinstance(item, dict) or item.get("status") != "needsAction"
                        or item.get("hidden") or item.get("deleted")):
                    continue
                title = item.get("title")
                task_id = item.get("id")
                if not isinstance(title, str) or not isinstance(task_id, str) or not 1 <= len(task_id) <= 256:
                    continue
                title = " ".join(title.split())[:100]
                if not title or not title.isprintable():
                    continue
                due = item.get("due")
                if due is not None:
                    try:
                        # Google Tasks defines due as a date. Its midnight UTC
                        # representation does not shift the date in US time zones.
                        due = date.fromisoformat(due[:10])
                    except (ValueError, TypeError):
                        continue
                identity = json.dumps([key, task_id], separators=(",", ":")).encode()
                digest = hmac.digest(grant, identity, "sha256").hex()
                rows[digest] = (due, title)
        return rows


class _DisplayHistory:
    """Only opaque task identifiers and the first local date actually shown."""
    def __init__(self, directory):
        self.path = Path(directory) / "chris-google-task-display.json"
        self.version = None
        self.seen = {}

    def read(self):
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self.version, self.seen = None, {}
            return self.seen
        version = (st.st_ino, st.st_mtime_ns, st.st_size)
        if version != self.version:
            with self.path.open("rb") as stream:
                raw = stream.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("Task history exceeds its size limit.")
            state = json.loads(raw)
            if not isinstance(state, dict) or len(state) > MAX_DISPLAY_HISTORY:
                raise ValueError("Invalid task history.")
            if any(not re.fullmatch("[a-f0-9]{64}", key) or not isinstance(day, str)
                   or date.fromisoformat(day).isoformat() != day for key, day in state.items()):
                raise ValueError("Invalid task history.")
            self.version, self.seen = version, state
        return self.seen


class AgendaGoogleTasks:
    def __init__(self, data_dir, config_getter):
        self.client = TasksClient(Path(data_dir) / "accounts" / "chris")
        self.config_getter = config_getter
        self._lock = threading.Lock()
        self._store = PersonalStore(data_dir, config_getter)
        self._store.lock_path = Path(data_dir) / "chris-google-task-display.lock"
        self._history = _DisplayHistory(data_dir)
        self._updated = None
        self._rows = {}
        self._grant = None

    def _grant_version(self):
        try:
            files = (self.client.client_path.stat(), self.client.token_path.stat())
            return self.client.calendar_binding(), tuple((st.st_ino, st.st_mtime_ns, st.st_size) for st in files)
        except (OSError, MessageError):
            return None

    def _enabled(self):
        config = self.config_getter()
        return config.get("demo") is False and config.get("chris_agenda_enabled") is True

    def refresh(self, now):
        found = {}
        grant = self._grant_version()
        if grant and self._enabled() and self.client.connected():
            try:
                self.client._access_token()
                grant = self._grant_version()
                found = self.client.unfinished()
            except (MessageError, OSError, ValueError):
                pass
        with self._lock:
            self._updated, self._rows, self._grant = now, found, grant

    def _fresh(self, now):
        return (self._enabled() and self._grant and self._grant == self._grant_version()
                and self._updated is not None and timedelta(0) <= now - self._updated < timedelta(minutes=30))

    def visible_tasks(self, now):
        zone = ZoneInfo(self.config_getter().get("timezone", "UTC"))
        today = now.astimezone(zone).date()
        with self._lock:
            if not self._fresh(now):
                return []
            try:
                seen = self._history.read()
            except (OSError, ValueError, RecursionError):
                seen = None
            rows = []
            for key, (due, title) in self._rows.items():
                row = {"title": title}
                if due is not None:
                    if not today <= due < today + timedelta(days=3):
                        continue
                    end = due + timedelta(days=1)
                else:
                    if seen is None:
                        continue
                    if key in seen:
                        first = date.fromisoformat(seen[key])
                        if not first <= today < first + timedelta(days=3):
                            continue
                    else:
                        # Never evict an expired open task to make room. Its
                        # next read would restart the three-day display clock.
                        if len(seen) >= MAX_DISPLAY_HISTORY:
                            continue
                        row["display_id"] = key
                        first = today
                    end = first + timedelta(days=3)
                row["display_until"] = datetime.combine(end, time.min, zone).astimezone(timezone.utc).isoformat()
                rows.append((due or date.max, title, key, row))
            remaining = MAX_DISPLAY_HISTORY - len(seen) if seen is not None else 0
            visible = []
            for _, _, _, row in sorted(rows):
                if "display_id" in row:
                    if remaining <= 0:
                        continue
                    remaining -= 1
                visible.append(row)
                if len(visible) == MAX_TASKS_ON_BOARD:
                    break
            return visible

    def mark_displayed(self, ids, now):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_TASKS_ON_BOARD
                or any(not isinstance(key, str) or not re.fullmatch("[a-f0-9]{64}", key) for key in ids)):
            return
        today = now.astimezone(ZoneInfo(self.config_getter().get("timezone", "UTC"))).date().isoformat()
        with self._lock:
            if not self._fresh(now):
                return
            with self._store._locked():
                seen = self._history.read()
                eligible = (key for key in dict.fromkeys(ids) if key not in seen and key in self._rows
                            and self._rows[key][0] is None)
                additions = dict.fromkeys(list(eligible)[:MAX_DISPLAY_HISTORY - len(seen)], today)
                if additions:
                    private_json(self._history.path, {**seen, **additions})
