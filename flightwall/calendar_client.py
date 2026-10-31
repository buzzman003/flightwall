"""Read-only Google Calendar sync and privacy-preserving flight parsing.

Only individual flight event titles are parsed. Descriptions are excluded by
default; the optional hotel reader can request them temporarily to extract
explicit hotel labels. Raw descriptions, attendees, crew, and organizer details
are never cached by this module. Calendar timestamps are authoritative.
"""

from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import webbrowser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3"
UTC = timezone.utc
_MAX_RESPONSE = 8 * 1024 * 1024


class CalendarError(Exception):
    """An error whose message is safe to display and log."""


class _HTTPFailure(CalendarError):
    def __init__(self, status: int, reason: str = "") -> None:
        self.status = status
        self.reason = reason
        if reason == "invalid_grant":
            message = "Google authorization expired or was revoked; run authorize again."
        elif status == 401:
            message = "Google authorization is invalid; run authorize again."
        elif status == 403:
            message = "Google Calendar access denied; check the Calendar API, consent, and calendar permissions."
        elif status == 404:
            message = "The selected Google calendar is unavailable; check the calendar selection."
        elif status == 429:
            message = "Google Calendar rate limit reached; retry later."
        elif status >= 500:
            message = "Google Calendar is temporarily unavailable; using the cached schedule."
        else:
            message = "Google Calendar request failed; check authorization and try again."
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    """Do not forward bearer credentials to a redirected endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def _private_json(path: Path, value: dict[str, Any]) -> None:
    """Write atomically, with a private directory and mode 0600 file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _event_time(value: Any) -> datetime | None:
    """Parse a timed event; all-day and ambiguous local-only dates are skipped."""
    if not isinstance(value, dict) or not isinstance(value.get("dateTime"), str):
        return None
    try:
        result = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        if result.tzinfo is None:
            zone = ZoneInfo(value.get("timeZone", ""))
            candidates = []
            for fold in (0, 1):
                candidate = result.replace(tzinfo=zone, fold=fold)
                if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == result:
                    candidates.append(candidate)
            if not candidates or len({item.utcoffset() for item in candidates}) != 1:
                return None
            result = candidates[0]
        if result.utcoffset() is None:
            return None
        return result
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
        return None


_NUMBER = r"(?:(?:[A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z])\s*)?\d{1,5}[A-Z]?"
_FLIGHT_TITLE = re.compile(
    rf"^(?:FLIGHT\s+)?(?P<number>{_NUMBER})\s*"
    r"(?P<dh>\(\s*(?:DH|DEADHEAD)\s*\)|\[\s*DH\s*\])?"
    r"(?:\s*:\s*|\s+)(?P<origin>[A-Z]{3})\s*[-–—→]\s*(?P<destination>[A-Z]{3})"
    r"(?:\s*\(\s*\d{3,4}\s*[-–—]\s*\d{3,4}\s*\))?$"
)
_REPORT_TITLE = re.compile(r"^(?:REPORT|RPT)(?:\s*:|\s+\d)")
_DEADHEAD_PREFIX = re.compile(r"^(?:DH|DEADHEAD)\s+")
_DEADHEAD_SUFFIX = re.compile(r"\s+(?:\((?:DH|DEADHEAD)\)|\[(?:DH|DEADHEAD)\]|DH|DEADHEAD)$")


def parse_events(events: list[dict[str, Any]], source: str = "FC View") -> list[dict[str, Any]]:
    """Convert FC View/Buzz-style individual flight titles to the shared schema.

    One summary must describe one leg. A report event's description may repeat
    every leg in a duty; it is deliberately ignored. The latest report whose
    event interval contains a leg's departure supplies that leg's report time.
    """
    reports: list[tuple[datetime, datetime]] = []
    candidates: dict[tuple[str, str, str, str], tuple[str, dict[str, Any]]] = {}
    # Resolve repeated event IDs before parsing so a cancellation or later edit
    # supersedes its older copy (pagination normally supplies each ID once).
    unique: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id:
            previous = unique.get(event_id)
            if previous is None or str(event.get("updated", "")) >= str(previous.get("updated", "")):
                unique[event_id] = event
        else:
            anonymous.append(event)
    for event in [*unique.values(), *anonymous]:
        if event.get("status") == "cancelled":
            continue
        title = event.get("summary", "")
        if not isinstance(title, str) or len(title) > 1000:
            continue
        title = " ".join(title.upper().split())
        start, end = _event_time(event.get("start")), _event_time(event.get("end"))
        if start is None or end is None or end.astimezone(UTC) <= start.astimezone(UTC):
            continue
        if _REPORT_TITLE.match(title):
            reports.append((start, end))
            continue
        deadhead = bool(_DEADHEAD_PREFIX.match(title) or _DEADHEAD_SUFFIX.search(title))
        title = _DEADHEAD_PREFIX.sub("", title)
        title = _DEADHEAD_SUFFIX.sub("", title)
        match = _FLIGHT_TITLE.fullmatch(title)
        if match is None:
            continue
        number = match["number"].replace(" ", "")
        origin, destination = match["origin"], match["destination"]
        if origin == destination:
            continue
        key = (number, origin, destination, _iso(start.astimezone(UTC)))
        public_id = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:24]
        flight = {
            "id": public_id,
            "number": number,
            "origin": origin,
            "destination": destination,
            "start": _iso(start),
            "end": _iso(end),
            "deadhead": bool(deadhead or match["dh"]),
            "source": str(source)[:256],
        }
        updated = str(event.get("updated", ""))
        if key not in candidates or updated >= candidates[key][0]:
            candidates[key] = (updated, flight)
    flights = [item[1] for item in candidates.values()]
    for flight in flights:
        departure = datetime.fromisoformat(flight["start"]).astimezone(UTC)
        matching = [start for start, end in reports if start.astimezone(UTC) <= departure < end.astimezone(UTC)]
        if matching:
            flight["report"] = _iso(max(matching, key=lambda value: value.astimezone(UTC)))
    flights.sort(key=lambda flight: (datetime.fromisoformat(flight["start"]).astimezone(UTC), flight["number"]))
    return flights


class CalendarClient:
    """Desktop OAuth client. Config is caller-owned; this class never edits it."""

    def __init__(self, data_dir: str | Path, config: dict[str, Any]) -> None:
        self.data_dir = Path(data_dir)
        self.config = config
        self.token_path = self.data_dir / "google-token.json"
        self.client_path = self.data_dir / "google-client.json"
        self.cache_path = self.data_dir / "calendar-cache.json"
        self._lock = threading.RLock()
        self._opener = build_opener(_NoRedirect())
        self._cache_key = ""
        self._snapshot: dict[str, Any] = {}
        self._load_cache()

    def _selection_key(self) -> str:
        selection = str(self.config.get("calendar_id") or self.config.get("calendar_name") or "FC View")
        return hashlib.sha256(selection.encode("utf-8")).hexdigest()

    def _empty(self) -> dict[str, Any]:
        return {"flights": [], "last_sync": None, "error": None,
                "source": str(self.config.get("calendar_name") or "FC View"), "demo": False}

    def _load_cache(self) -> None:
        self._cache_key = self._selection_key()
        self._snapshot = self._empty()
        try:
            cached = _read_json(self.cache_path)
            if cached.get("selection") != self._cache_key:
                return
            snapshot = cached.get("snapshot")
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("flights"), list):
                return
            # Re-parse only the allowed display fields; never relay arbitrary
            # extra keys from a damaged/hand-edited cache to renderer or control.
            flights = []
            for item in snapshot["flights"]:
                if not isinstance(item, dict):
                    continue
                title = f"{item.get('number', '')}: {item.get('origin', '')}-{item.get('destination', '')}"
                parsed = parse_events([{"summary": title,
                                        "start": {"dateTime": item.get("start")},
                                        "end": {"dateTime": item.get("end")}}], str(snapshot.get("source", "FC View")))
                if parsed:
                    flight = parsed[0]
                    flight["deadhead"] = item.get("deadhead") is True
                    report = _event_time({"dateTime": item.get("report")})
                    if report is not None and report.astimezone(UTC) <= datetime.fromisoformat(flight["start"]).astimezone(UTC):
                        flight["report"] = _iso(report)
                    flights.append(flight)
            last_sync = _event_time({"dateTime": snapshot.get("last_sync")})
            self._snapshot = {"flights": flights, "last_sync": _iso(last_sync) if last_sync else None,
                              "error": None, "source": str(snapshot.get("source", "FC View"))[:256], "demo": False}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self._snapshot["error"] = "Cached schedule could not be read; synchronize again."

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._cache_key != self._selection_key():
                self._load_cache()
            return copy.deepcopy(self._snapshot)

    def _request_json(self, method: str, url: str, *, form: dict[str, Any] | None = None,
                      access_token: str | None = None) -> dict[str, Any]:
        # Every endpoint is pinned by the application, never loaded from OAuth
        # client JSON, preventing accidental credential delivery elsewhere.
        if not (url == TOKEN_URL or url.startswith(CALENDAR_URL + "/")):
            raise CalendarError("Unsupported Google API endpoint.")
        headers = {"Accept": "application/json", "User-Agent": "FlightWall/0.1"}
        data = None
        if form is not None:
            data = urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if access_token is not None:
            headers["Authorization"] = "Bearer " + access_token
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=20) as response:
                payload = response.read(_MAX_RESPONSE + 1)
            if len(payload) > _MAX_RESPONSE:
                raise CalendarError("Google Calendar response was too large; retry later.")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise ValueError("Invalid API response")
            return result
        except HTTPError as error:
            reason = ""
            try:
                body = json.loads(error.read(16384))
                if body.get("error") in ("invalid_grant", "invalid_client", "access_denied"):
                    reason = body["error"]
            except (ValueError, TypeError, AttributeError):
                pass
            raise _HTTPFailure(error.code, reason) from None
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            raise CalendarError("Google Calendar network unavailable; using the cached schedule.") from None
        except (ValueError, UnicodeError):
            raise CalendarError("Google Calendar returned an invalid response; retry later.") from None

    def _client(self) -> dict[str, Any]:
        try:
            stored = _read_json(self.client_path)
            client = stored.get("installed", stored)
            if not isinstance(client, dict) or not isinstance(client.get("client_id"), str) or not client["client_id"]:
                raise ValueError("Missing client ID")
            return client
        except (OSError, ValueError, TypeError):
            raise CalendarError("Google Calendar is not authorized; run authorize first.") from None

    def _token(self) -> dict[str, Any]:
        try:
            token = _read_json(self.token_path)
            if not isinstance(token.get("access_token"), str):
                raise ValueError("Missing access token")
            granted = token.get("scopes", [])
            if not isinstance(granted, list) or not set(SCOPES).issubset(granted):
                raise CalendarError("Google Calendar read permissions are missing; run authorize again.")
            return token
        except CalendarError:
            raise
        except (OSError, ValueError, TypeError):
            raise CalendarError("Google Calendar is not authorized; run authorize first.") from None

    def _store_token(self, response: dict[str, Any], client: dict[str, Any],
                     previous: dict[str, Any] | None = None) -> dict[str, Any]:
        previous = previous or {}
        access_token = response.get("access_token")
        if not isinstance(access_token, str) or not access_token or len(access_token) > 16384:
            raise CalendarError("Google did not return a valid access token; authorize again.")
        token_type = response.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            raise CalendarError("Google returned an unsupported token type.")
        scope_text = response.get("scope", " ".join(previous.get("scopes", SCOPES)))
        granted = scope_text.split() if isinstance(scope_text, str) else []
        if not set(SCOPES).issubset(granted):
            raise CalendarError("Both Google Calendar read permissions must be approved; authorize again.")
        refresh_token = response.get("refresh_token") or previous.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise CalendarError("Google did not grant offline access; run authorize again and approve access.")
        try:
            lifetime = int(response.get("expires_in", 3600))
            if lifetime <= 0:
                raise ValueError("Expired token")
        except (ValueError, TypeError, OverflowError):
            raise CalendarError("Google returned an invalid token lifetime; authorize again.") from None
        token = {"client_id": client["client_id"], "access_token": access_token,
                 "refresh_token": refresh_token, "expires_at": time.time() + lifetime,
                 "scopes": granted}
        _private_json(self.token_path, token)
        return token

    def _access_token(self, force_refresh: bool = False) -> str:
        client, token = self._client(), self._token()
        if token.get("client_id") != client["client_id"]:
            raise CalendarError("Google authorization does not match this client; run authorize again.")
        try:
            valid = float(token.get("expires_at", 0)) > time.time() + 60
        except (ValueError, TypeError):
            valid = False
        if valid and not force_refresh:
            return token["access_token"]
        if not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
            raise CalendarError("Google offline authorization is missing; run authorize again.")
        form = {"client_id": client["client_id"], "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token"}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        result = self._request_json("POST", TOKEN_URL, form=form)
        return self._store_token(result, client, token)["access_token"]

    def _api(self, path: str, parameters: dict[str, Any]) -> dict[str, Any]:
        url = CALENDAR_URL + path + "?" + urlencode(parameters)
        token = self._access_token()
        try:
            return self._request_json("GET", url, access_token=token)
        except _HTTPFailure as error:
            if error.status != 401:
                raise
            token = self._access_token(force_refresh=True)
            return self._request_json("GET", url, access_token=token)

    def _pages(self, path: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        parameters = dict(parameters)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(100):
            result = self._api(path, parameters)
            page_items = result.get("items", [])
            if not isinstance(page_items, list):
                raise CalendarError("Google Calendar returned an invalid event list.")
            items.extend(item for item in page_items if isinstance(item, dict))
            next_page = result.get("nextPageToken")
            if not next_page:
                return items
            if not isinstance(next_page, str) or next_page in seen:
                raise CalendarError("Google Calendar pagination failed; retry later.")
            seen.add(next_page)
            parameters["pageToken"] = next_page
        raise CalendarError("Google Calendar returned too many pages; retry later.")

    def list_calendars(self) -> list[dict[str, Any]]:
        """Return selectable readable calendars; errors are safe CalendarError messages."""
        with self._lock:
            calendars = self._pages("/users/me/calendarList", {
                "maxResults": 250, "minAccessRole": "reader", "showHidden": "true",
                "fields": "nextPageToken,items(id,summary,summaryOverride,timeZone,primary,accessRole)",
            })
            result = {}
            for item in calendars:
                if isinstance(item.get("id"), str) and item["id"] and not item.get("deleted"):
                    result[item["id"]] = {"id": item["id"],
                                          "name": str(item.get("summaryOverride") or item.get("summary") or "Unnamed calendar")[:256],
                                          "timezone": str(item.get("timeZone") or "UTC"),
                                          "primary": item.get("primary") is True}
            return sorted(result.values(), key=lambda item: (item["name"].casefold(), item["id"]))

    def get_events(self, calendar_id: str, time_min=None, time_max=None, *, include_descriptions=False) -> dict[str, Any]:
        """Read one bounded calendar; descriptions require explicit opt-in.

        Calendar-level timezone is retained so all-day household events use the
        source calendar's civil dates rather than the wall's display timezone.
        """
        if not isinstance(calendar_id, str) or not calendar_id or len(calendar_id) > 2048:
            raise CalendarError("Select a valid Google calendar first.")
        if not isinstance(include_descriptions, bool):
            raise CalendarError("Description access must be explicitly enabled or disabled.")
        now = _utc_now()
        start, end = time_min or now - timedelta(days=2), time_max or now + timedelta(days=45)
        if not isinstance(start, datetime) or not isinstance(end, datetime) or start.tzinfo is None or end.tzinfo is None or end <= start:
            raise CalendarError("Calendar event windows require ordered timezone-aware timestamps.")
        parameters = {"singleEvents": "true", "showDeleted": "false", "orderBy": "startTime", "maxResults": 2500,
                      "timeMin": _iso(start), "timeMax": _iso(end),
                      "fields": "nextPageToken,timeZone,items(id,summary,status,start,end,updated)"}
        if include_descriptions:
            parameters["fields"] = "nextPageToken,timeZone,items(id,summary,status,start,end,updated,description)"
        with self._lock:
            items, seen, zone = [], set(), None
            for _ in range(100):
                result = self._api("/calendars/" + quote(calendar_id, safe="") + "/events", parameters)
                if not isinstance(result.get("items", []), list):
                    raise CalendarError("Google Calendar returned an invalid event list.")
                items.extend(item for item in result.get("items", []) if isinstance(item, dict))
                if isinstance(result.get("timeZone"), str):
                    zone = result["timeZone"]
                next_page = result.get("nextPageToken")
                if not next_page:
                    return {"items": items, "timezone": zone}
                if not isinstance(next_page, str) or next_page in seen:
                    raise CalendarError("Google Calendar pagination failed; retry later.")
                seen.add(next_page)
                parameters["pageToken"] = next_page
        raise CalendarError("Google Calendar returned too many pages; retry later.")

    def sync(self) -> dict[str, Any]:
        """Fetch a full bounded schedule, replacing the cache only after success."""
        with self._lock:
            if self._cache_key != self._selection_key():
                self._load_cache()
            try:
                calendar_id = str(self.config.get("calendar_id") or "").strip()
                source = str(self.config.get("calendar_name") or "FC View").strip()
                if not calendar_id:
                    matches = [item for item in self.list_calendars() if item["name"].strip().casefold() == source.casefold()]
                    if not matches:
                        raise CalendarError("The selected calendar was not found; list calendars and select FC View or Buzz Flight Brief.")
                    if len(matches) > 1:
                        raise CalendarError("Several calendars have this name; select one by its calendar ID.")
                    calendar_id, source = matches[0]["id"], matches[0]["name"]
                now = _utc_now()
                events = self._pages("/calendars/" + quote(calendar_id, safe="") + "/events", {
                    "singleEvents": "true", "showDeleted": "false", "orderBy": "startTime", "maxResults": 2500,
                    "timeMin": _iso(now - timedelta(days=2)), "timeMax": _iso(now + timedelta(days=45)),
                    "fields": "nextPageToken,items(id,summary,status,start,end,updated)",
                })
                snapshot = {"flights": parse_events(events, source), "last_sync": _iso(now),
                            "error": None, "source": source, "demo": False}
                _private_json(self.cache_path, {"selection": self._cache_key, "snapshot": snapshot})
                self._snapshot = snapshot
            except CalendarError as error:
                self._snapshot["error"] = str(error)
            except (OSError, ValueError, TypeError, OverflowError):
                self._snapshot["error"] = "Calendar sync or local storage failed; using the cached schedule."
            return copy.deepcopy(self._snapshot)

    def authorize(self, credentials_path: str | Path, open_browser: bool = True, *,
                  callback_port: int = 8766, timeout: float = 300,
                  on_url: Callable[[str], Any] = print) -> dict[str, Any]:
        """Run Desktop OAuth with S256 PKCE and a loopback-only callback.

        For a headless Batocera host, forward the callback port over SSH from a
        laptop, pass open_browser=False, and open the printed URL on the laptop.
        This is a supported loopback redirect, never Google's retired OOB flow.
        """
        try:
            raw_client = _read_json(Path(credentials_path)).get("installed")
            if not isinstance(raw_client, dict) or not isinstance(raw_client.get("client_id"), str) or not raw_client["client_id"]:
                raise ValueError("Not a Desktop app credential")
            if raw_client.get("client_secret") is not None and not isinstance(raw_client["client_secret"], str):
                raise ValueError("Invalid client secret")
            client = {key: raw_client[key] for key in ("client_id", "client_secret") if key in raw_client}
        except (OSError, ValueError, TypeError):
            raise CalendarError("Choose the downloaded Google OAuth Desktop app client JSON file.") from None
        if not isinstance(callback_port, int) or not 0 <= callback_port <= 65535 or timeout <= 0:
            raise CalendarError("Invalid OAuth callback port or timeout.")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        received: dict[str, str] = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # The callback URL contains a one-use authorization code.

            def do_GET(self):
                parsed = urlsplit(self.path)
                expected_host = f"127.0.0.1:{self.server.server_port}"
                if len(self.path) > 16384 or parsed.path != "/oauth2/callback" or self.headers.get("Host") != expected_host:
                    self._reply(404, "Unknown callback.")
                    return
                query = parse_qs(parsed.query)
                values = query.get("state", [])
                if len(values) != 1 or not hmac.compare_digest(values[0].encode("utf-8"), state.encode("ascii")):
                    self._reply(403, "Invalid authorization state. Return to the original sign-in link.")
                    return
                if received:
                    self._reply(409, "Authorization was already received.")
                elif query.get("error"):
                    received["error"] = "denied"
                    self._reply(400, "Google authorization was declined. You may close this tab.")
                elif len(query.get("code", [])) == 1 and query["code"][0]:
                    received["code"] = query["code"][0]
                    self._reply(200, "Authorization received. Return to Flight Wall to finish setup. You may close this tab.")
                else:
                    self._reply(400, "No authorization code was received.")

            def _reply(self, status, message):
                body = message.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

        class LoopbackServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(5)
                return connection, address

        try:
            server = LoopbackServer(("127.0.0.1", callback_port), Callback)
        except OSError:
            raise CalendarError("OAuth callback port is unavailable; close the other setup or choose another port.") from None
        redirect = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
        url = AUTH_URL + "?" + urlencode({"client_id": client["client_id"], "redirect_uri": redirect,
                                         "response_type": "code", "scope": " ".join(SCOPES),
                                         "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                                         "access_type": "offline", "prompt": "consent"})
        try:
            on_url(url)
            if open_browser:
                try:
                    webbrowser.open(url, new=1, autoraise=True)
                except (OSError, webbrowser.Error):
                    pass  # The printed URL is still available.
            deadline = time.monotonic() + timeout
            while not received and time.monotonic() < deadline:
                server.timeout = min(0.5, max(0.01, deadline - time.monotonic()))
                server.handle_request()
        finally:
            server.server_close()
        if not received:
            raise CalendarError("Google authorization timed out; start authorize again.")
        if "error" in received:
            raise CalendarError("Google authorization was declined; start authorize again when ready.")
        form = {"client_id": client["client_id"], "code": received["code"], "code_verifier": verifier,
                "redirect_uri": redirect, "grant_type": "authorization_code"}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        with self._lock:
            result = self._request_json("POST", TOKEN_URL, form=form)
            # Keep the Desktop credential wrapper so the same documented
            # authorize --credentials google-client.json command can be used
            # again when its refresh token expires or is revoked.
            _private_json(self.client_path, {"installed": client})
            self._store_token(result, client)
        return {"authorized": True, "scopes": list(SCOPES),
                "next_step": "List calendars, select FC View or Buzz Flight Brief, then disable demo mode and sync."}
