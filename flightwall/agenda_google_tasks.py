"""Read the titles of Chris's open Google Tasks using separate, read-only consent."""
from datetime import date, timedelta
import hashlib
import hmac
from pathlib import Path
import threading
from urllib.parse import quote, urlencode

from .calendar_client import CalendarClient, CalendarError
from .messages import GoogleReadClient, MessageError, _Unauthorized


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
        return {cls.email_scope if scope == "email" else scope for scope in scopes} == set(cls.scopes)

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
        rows = []
        for index, group in enumerate(self.pages("/users/@me/lists", {"fields": "nextPageToken,items(id)"}, 1)):
            if index >= 32:
                raise MessageError("Too many task lists.")
            key = group.get("id") if isinstance(group, dict) else None
            if not isinstance(key, str) or not 1 <= len(key) <= 256 or key in {".", ".."}:
                raise MessageError("Invalid task list.")
            path = "/lists/" + quote(key, safe="") + "/tasks"
            params = {"fields": "nextPageToken,items(title,status,due,hidden,deleted)", "showCompleted": "false",
                      "showHidden": "false", "showDeleted": "false"}
            for item in self.pages(path, params, 5):
                if (not isinstance(item, dict) or item.get("status") != "needsAction"
                        or item.get("hidden") or item.get("deleted")):
                    continue
                title = item.get("title")
                if not isinstance(title, str):
                    continue
                title = " ".join(title.split())[:100]
                if not title or not title.isprintable():
                    continue
                try:
                    due = date.fromisoformat(item["due"][:10])
                except (TypeError, KeyError, ValueError):
                    due = date.max
                rows.append((due, title))
        return list(dict.fromkeys(title for _, title in sorted(rows)))[:40]


class AgendaGoogleTasks:
    def __init__(self, data_dir, config_getter):
        self.client = TasksClient(Path(data_dir) / "accounts" / "chris")
        self.config_getter = config_getter
        self._lock = threading.Lock()
        self._updated = None
        self._titles = []
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
        found = []
        grant = self._grant_version()
        if grant and self._enabled() and self.client.connected():
            try:
                self.client._access_token()
                grant = self._grant_version()
                found = self.client.unfinished()
            except (MessageError, OSError, ValueError):
                pass
        with self._lock:
            self._updated, self._titles, self._grant = now, found, grant

    def titles(self, now):
        if not self._enabled():
            return []
        with self._lock:
            if (self._grant and self._grant == self._grant_version() and self._updated is not None
                    and timedelta(0) <= now - self._updated < timedelta(minutes=30)):
                return list(self._titles)
        return []
