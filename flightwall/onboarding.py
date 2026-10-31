"""Authenticated browser setup, isolated from MCP and phone credentials."""
from __future__ import annotations

import base64
from collections import defaultdict, deque
import hashlib
import hmac
from http.cookies import SimpleCookie
import ipaddress
import html
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import private_json

WIZARD_SETTINGS = frozenset({
    "timezone", "rotation_seconds", "flight_focus_seconds", "flight_window_before_minutes",
    "flight_window_after_minutes", "calendar_id", "calendar_name", "secondary_calendar_id",
    "secondary_calendar_name", "family_calendar_profile", "family_calendar_id", "family_calendar_name",
    "kids_filter_enabled", "kids_stale_minutes", "presence_enabled", "presence_ttl_minutes",
    "home_radius_m", "home_latitude", "home_longitude", "chris_phone_type", "lisa_phone_type",
    "messages_enabled", "chris_gmail_enabled", "lisa_gmail_enabled", "chris_lawyer_senders",
    "chris_lawyer_domains", "lisa_mail_senders", "lisa_mail_domains", "chris_notifications_enabled",
    "lisa_notifications_enabled", "chris_notification_apps", "lisa_notification_apps",
    "lisa_private_enabled", "health_enabled", "health_query", "inspiration_enabled",
    "inspiration_topics", "inspiration_sources", "guest_mode", "openai_enabled", "openai_model",
    "hotel_details_enabled",
    "display_scale_mode", "timers_enabled", "custody_outlook_enabled", "custody_outlook_days", "flight_preview_days",
    "local_visits_enabled", "shopping_ticker_enabled", "shopping_ticker_visible",
})
SCOPES = {
    "calendar": ("https://www.googleapis.com/auth/calendar.events.readonly",
                 "https://www.googleapis.com/auth/calendar.calendarlist.readonly"),
    "gmail": ("https://www.googleapis.com/auth/gmail.readonly",),
}
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
OPENAI_RESPONSES = "https://api.openai.com/v1/responses"
COOKIE_NAME = "FlightWallSetup"
SESSION_SECONDS = 3600
MAX_BODY = 65536
PEOPLE = ("chris", "lisa")


class SetupError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _json_request(url, form=None, *, payload=None, bearer=None):
    if url not in (GOOGLE_TOKEN, OPENAI_RESPONSES):
        raise SetupError("This setup service is unavailable.")
    headers = {"Accept": "application/json", "User-Agent": "FlightWall-Setup/0.3"}
    if form is not None:
        data = urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    try:
        with build_opener(_NoRedirect()).open(Request(url, data=data, headers=headers), timeout=15) as response:
            raw = response.read(131073)
            if len(raw) > 131072 or response.geturl() != url:
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError
            return result
    except Exception:
        raise SetupError("The provider could not complete the request. Check its API setup and try again.") from None


def _clean_status(value):
    if isinstance(value, dict):
        return {str(k): _clean_status(v) for k, v in value.items()
                if str(k) not in {"health_query", "inspiration_topics", "body", "preview", "image_data"}
                and not re.search(r"(^|_)(token|secret|key|authorization|credential)(s)?$", str(k), re.I)}
    if isinstance(value, list):
        return [_clean_status(v) for v in value[:100]]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return None


class OnboardingRouter:
    def __init__(self, data_dir, config_getter, callbacks=None):
        self.data_dir = Path(data_dir)
        self.config_getter = config_getter
        self.callbacks = callbacks or {}
        self.sessions = {}
        self.flows = {}
        self.failures = defaultdict(deque)
        self.lock = threading.RLock()
        self.asset_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "assets" / "onboarding"
        self.phone_invites = None

    def _invites(self):
        with self.lock:
            if self.phone_invites is None:
                from .phone_invites import PhoneInviteRouter
                self.phone_invites = PhoneInviteRouter(self)
            return self.phone_invites

    def _origin(self, handler):
        host = handler.headers.get("Host", "")
        if any(c in host for c in "/\\@\r\n"):
            raise SetupError("Open setup through the configured HTTPS address or localhost.")
        public = self.config_getter().get("public_url", "").rstrip("/")
        if public and host.casefold() == urlsplit(public).netloc.casefold():
            return public, True
        try:
            parsed = urlsplit("http://" + host)
            peer = ipaddress.ip_address(handler.client_address[0])
            local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
            if not peer.is_loopback or not local or not parsed.port or parsed.path:
                raise ValueError
        except (ValueError, TypeError):
            raise SetupError("Open setup through the configured HTTPS address or a localhost SSH tunnel.") from None
        return "http://" + host, False

    def _reply(self, handler, code, value, *, content_type="application/json; charset=utf-8", cookie=None):
        raw = json.dumps(value, allow_nan=False).encode("utf-8") if content_type.startswith("application/json") else value
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(raw)))
        # Early auth/Origin failures intentionally do not consume an untrusted
        # request body. Never let its unread bytes become another HTTP request.
        handler.close_connection = True
        handler.send_header("Connection", "close")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            handler.send_header("Set-Cookie", cookie)
        handler.end_headers()
        handler.wfile.write(raw)

    def _body(self, handler, limit=MAX_BODY):
        try:
            if handler.headers.get("Transfer-Encoding") is not None or len(handler.headers.get_all("Content-Length", [])) != 1:
                raise ValueError
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= limit or handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError
            value = json.loads(handler.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError, UnicodeError):
            handler.close_connection = True
            raise SetupError("Send a small JSON object for this setup action.") from None

    def _purge(self):
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
        self.flows = {k: v for k, v in self.flows.items() if v["expires"] > now}

    def _session(self, handler):
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get("Cookie", ""))
            value = cookie[COOKIE_NAME].value
            key = hashlib.sha256(value.encode("ascii")).hexdigest()
        except Exception:
            return None, None
        with self.lock:
            self._purge()
            session = self.sessions.get(key)
            if session and not self._valid_session(session):
                self.sessions.pop(key, None)
                session = None
            return key, session

    def _valid_session(self, session):
        try:
            current = (self.data_dir / "control-token.txt").read_text().strip()
        except OSError:
            return False
        return bool(current) and hmac.compare_digest(session.get("admin_hash", ""), hashlib.sha256(current.encode()).hexdigest())

    def _cookie(self, value, secure, *, clear=False):
        return (f"{COOKIE_NAME}={value}; Path=/setup; HttpOnly; SameSite=Strict; Max-Age="
                + ("0" if clear else str(SESSION_SECONDS)) + ("; Secure" if secure else ""))

    def _consent(self, body, person):
        if body.get("owner_consent") is not True or body.get("consenting_person") != person:
            raise SetupError(f"{person.title()} must review and explicitly approve this connection on their own setup panel.")

    def _record_consent(self, person, scope):
        path = self.data_dir / "setup-consents.json"
        try:
            records = json.loads(path.read_text())
            if not isinstance(records, list):
                records = []
        except (OSError, ValueError):
            records = []
        records.append({"person": person, "scope": scope, "at": int(time.time())})
        private_json(path, records[-100:])

    def _account(self, person, service):
        if person not in PEOPLE or service not in SCOPES:
            raise SetupError("Choose Chris or Lisa and a supported Google connection.")
        directory = self.data_dir / "accounts" / person
        prefix = "google" if service == "calendar" else "gmail"
        return directory / (prefix + "-client.json"), directory / (prefix + "-token.json")

    def _saved_authorization(self, person, service):
        client_path, token_path = self._account(person, service)
        try:
            client, token = json.loads(client_path.read_text()), json.loads(token_path.read_text())
            if not isinstance(client, dict) or not isinstance(token, dict):
                return False
            client = client.get("installed", client.get("web", client))
            if not isinstance(client, dict) or not isinstance(client.get("client_id"), str) or not client["client_id"]:
                return False
            return (token.get("client_id") == client["client_id"]
                    and set(SCOPES[service]) <= set(token.get("scopes", []))
                    and isinstance(token.get("refresh_token"), str) and bool(token["refresh_token"]))
        except (OSError, ValueError, TypeError):
            return False

    def _status(self, session):
        config = self.config_getter()
        extra = self.callbacks.get("setup_get", lambda: {})()
        if not isinstance(extra, dict):
            extra = {}
        return {"csrf": session["csrf"], "settings": {k: config.get(k) for k in sorted(WIZARD_SETTINGS - {"health_query", "inspiration_topics"})},
                "profiles": {person: {"calendar_authorized": self._saved_authorization(person, "calendar"),
                                      "gmail_authorized": self._saved_authorization(person, "gmail")} for person in PEOPLE},
                "openai_key_saved": (self.data_dir / "openai.key").is_file(),
                "health_query_saved": bool(config.get("health_query")),
                "inspiration_topics_saved": bool(config.get("inspiration_topics")),
                "public_url": config.get("public_url", ""),
                "google_redirect_uri": config.get("public_url", "").rstrip("/") + "/setup/oauth/callback" if config.get("public_url") else "",
                "mcp_url": config.get("public_url", "").rstrip("/") + "/mcp" if config.get("public_url") else "",
                "status": _clean_status(extra)}

    def _save_settings(self, body):
        patch = body.get("settings")
        if not isinstance(patch, dict) or not patch or set(patch) - WIZARD_SETTINGS:
            raise SetupError("Choose supported setup settings to save.")
        patch = dict(patch)
        for person in PEOPLE:
            private_fields = {person + "_gmail_enabled", person + "_notifications_enabled"}
            selection_fields = ({"chris_lawyer_senders", "chris_lawyer_domains", "chris_notification_apps"}
                                if person == "chris" else {"lisa_mail_senders", "lisa_mail_domains", "lisa_notification_apps"})
            if person == "lisa":
                private_fields |= {"lisa_private_enabled", "health_enabled", "inspiration_enabled", "openai_enabled"}
                selection_fields |= {"health_query", "inspiration_topics", "inspiration_sources"}
            if any(patch.get(field) is True for field in private_fields) or set(patch) & selection_fields:
                self._consent(body, person)
                self._record_consent(person, "settings:" + ",".join(sorted(private_fields & set(patch))))
            if patch.get(person + "_gmail_enabled") is True and not self._saved_authorization(person, "gmail"):
                raise SetupError(f"Connect {person.title()}'s Gmail with their own permission before enabling its message cards.")
        for id_key, name_key, person in (("calendar_id", "calendar_name", "chris"),
                                         ("secondary_calendar_id", "secondary_calendar_name", "chris"),
                                         ("family_calendar_id", "family_calendar_name", patch.get("family_calendar_profile", self.config_getter().get("family_calendar_profile", "chris")))):
            if id_key not in patch:
                continue
            if not patch[id_key]:
                if id_key == "calendar_id":
                    raise SetupError("Choose a primary flight calendar. Disconnect it deliberately using local settings instead of this chooser.")
                patch[name_key] = ""
                if id_key == "family_calendar_id":
                    patch["kids_filter_enabled"] = False
                continue
            choices = self.callbacks.get("setup_calendars", lambda person: [])(person)
            matches = [item for item in choices if item.get("id") == patch[id_key]]
            if not matches:
                raise SetupError("Select a calendar from the verified list for that Google account.")
            patch[name_key] = matches[0]["name"]
            if id_key == "calendar_id":
                # A verified primary-calendar choice is the explicit transition
                # from sample flight data to this household's real roster.
                patch["demo"] = False
        if (patch.get("family_calendar_profile", self.config_getter().get("family_calendar_profile"))
                != self.config_getter().get("family_calendar_profile") and "family_calendar_id" not in patch):
            raise SetupError("Choose a verified custody calendar for the newly selected account.")
        if patch.get("kids_filter_enabled") is True and not (patch.get("family_calendar_id") or self.config_getter().get("family_calendar_id")):
            raise SetupError("Select and verify the custody calendar before enabling the kids rule.")
        callback = self.callbacks.get("setup_save")
        if callback is None:
            raise SetupError("Setup saving is not available in this build.")
        callback(patch)
        self.callbacks.get("setup_refresh", lambda: None)()
        return {"saved": True}

    def _save_google_client(self, body):
        person, service = body.get("person"), body.get("service")
        self._consent(body, person)
        client_path, _ = self._account(person, service)
        raw = body.get("credentials", {}).get("web") if isinstance(body.get("credentials"), dict) else None
        callback = self.config_getter().get("public_url", "").rstrip("/") + "/setup/oauth/callback"
        if not callback.startswith("https://"):
            raise SetupError("Configure your public HTTPS address first, or use the documented Desktop OAuth CLI method.")
        if (not isinstance(raw, dict) or not isinstance(raw.get("client_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{8,230}\.apps\.googleusercontent\.com", raw["client_id"])
                or not isinstance(raw.get("client_secret"), str) or not 8 <= len(raw["client_secret"]) <= 512
                or any(ord(c) < 33 for c in raw["client_secret"])
                or callback not in raw.get("redirect_uris", [])
                or raw.get("token_uri", GOOGLE_TOKEN) != GOOGLE_TOKEN
                or raw.get("auth_uri", "https://accounts.google.com/o/oauth2/auth") not in
                (GOOGLE_AUTH, "https://accounts.google.com/o/oauth2/auth")):
            raise SetupError("Upload the Google Web application client JSON with this exact authorized redirect URI. Desktop JSON cannot use this browser callback.")
        # Keep working Desktop/previous Web authorization intact until the new
        # consent flow succeeds. Cancelled Google sign-in must not break it.
        staging_path = client_path.with_name(client_path.stem.replace("-client", "-web-client") + ".json")
        private_json(staging_path, {key: raw[key] for key in ("client_id", "client_secret")})
        return {"saved": True}

    def _oauth_start(self, body, session_key):
        person, service = body.get("person"), body.get("service")
        self._consent(body, person)
        client_path, _ = self._account(person, service)
        public = self.config_getter().get("public_url", "").rstrip("/")
        if not public.startswith("https://"):
            raise SetupError("A configured public HTTPS address is required for this browser connection.")
        try:
            staging_path = client_path.with_name(client_path.stem.replace("-client", "-web-client") + ".json")
            client = json.loads(staging_path.read_text())
        except (OSError, ValueError):
            raise SetupError("Upload the Google Web application client JSON for this account first.") from None
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        redirect = public + "/setup/oauth/callback"
        with self.lock:
            self._purge()
            if len(self.flows) >= 20:
                raise SetupError("Finish an existing Google connection before starting another.")
            self.flows[hashlib.sha256(state.encode()).hexdigest()] = {
                "person": person, "service": service, "client": client, "verifier": verifier,
                "redirect": redirect, "session": session_key, "expires": time.time() + 600,
            }
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        return {"authorization_url": GOOGLE_AUTH + "?" + urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(SCOPES[service]), "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline",
            "prompt": "consent select_account", "include_granted_scopes": "false",
        })}

    def _oauth_complete(self, query):
        values = parse_qs(query)
        if any(len(v) != 1 for v in values.values()):
            raise SetupError("This Google response is not valid. Start the connection again.")
        state = values.get("state", [""])[0]
        with self.lock:
            self._purge()
            flow = self.flows.pop(hashlib.sha256(state.encode()).hexdigest(), None)
            if not flow or flow["session"] not in self.sessions or not self._valid_session(self.sessions[flow["session"]]):
                raise SetupError("This Google connection expired or was already used. Return to setup and start again.")
        if "error" in values or not values.get("code") or len(values["code"][0]) > 4096:
            raise SetupError("Google access was not approved. Existing saved connections were not changed.")
        if flow["redirect"] != self.config_getter().get("public_url", "").rstrip("/") + "/setup/oauth/callback":
            raise SetupError("The setup address changed. Start the Google connection again.")
        client = flow["client"]
        response = _json_request(GOOGLE_TOKEN, form={"grant_type": "authorization_code", "code": values["code"][0],
                "redirect_uri": flow["redirect"], "client_id": client["client_id"], "client_secret": client["client_secret"],
                "code_verifier": flow["verifier"]})
        granted = response.get("scope", "").split() if isinstance(response.get("scope"), str) else []
        access, refresh = response.get("access_token"), response.get("refresh_token")
        try:
            lifetime = int(response.get("expires_in", 0))
        except (ValueError, TypeError):
            lifetime = 0
        if (set(granted) != set(SCOPES[flow["service"]]) or response.get("token_type", "").casefold() != "bearer"
                or not isinstance(access, str) or not 1 <= len(access) <= 16384
                or not isinstance(refresh, str) or not 1 <= len(refresh) <= 16384 or not 0 < lifetime <= 86400):
            raise SetupError("Google did not grant the exact requested read permissions and offline access. Start again and approve only this connection.")
        client_path, token_path = self._account(flow["person"], flow["service"])
        private_json(client_path, client)
        private_json(token_path, {"client_id": client["client_id"], "access_token": access, "refresh_token": refresh,
                                  "expires_at": time.time() + lifetime, "scopes": granted})
        self._record_consent(flow["person"], flow["service"])
        self.callbacks.get("setup_refresh", lambda: None)()

    def handle(self, handler, method, path):
        parsed = urlsplit(path)
        if parsed.path.casefold() != "/setup" and not parsed.path.startswith("/setup/"):
            return False
        try:
            if parsed.path == "/setup/lisa" or parsed.path.startswith("/setup/lisa/"):
                return self._invites().handle(handler, method, path)
            origin, secure = self._origin(handler)
            route = "/setup" if parsed.path.casefold() == "/setup" else parsed.path.rstrip("/")
            if method == "GET" and route in ("/setup", "/setup/app.js", "/setup/style.css"):
                filename, mime = {"/setup": ("index.html", "text/html; charset=utf-8"),
                                  "/setup/app.js": ("app.js", "text/javascript; charset=utf-8"),
                                  "/setup/style.css": ("style.css", "text/css; charset=utf-8")}[route]
                self._reply(handler, 200, (self.asset_dir / filename).read_bytes(), content_type=mime)
                return True
            if method == "GET" and route == "/setup/oauth/callback":
                self._oauth_complete(parsed.query)
                self._reply(handler, 200, '<!doctype html><meta charset="utf-8"><title>Account connected</title><p>Your Google read authorization is saved. Return to setup to list and choose the actual calendars.</p><a href="/setup">Return to Flight Wall setup</a>', content_type="text/html; charset=utf-8")
                return True
            if method == "POST" and handler.headers.get("Origin") != origin:
                self._reply(handler, 403, {"error": "Setup requests must come from this same browser address."})
                return True
            session_key, session = self._session(handler)
            if method == "POST" and route == "/setup/login":
                body = self._body(handler)
                peer = handler.client_address[0]
                with self.lock:
                    failures = self.failures[peer]
                    while failures and failures[0] < time.time() - 900:
                        failures.popleft()
                    if len(failures) >= 8:
                        self._reply(handler, 429, {"error": "Too many sign-in attempts. Wait 15 minutes and try again."})
                        return True
                    try:
                        expected = (self.data_dir / "control-token.txt").read_text().strip()
                    except OSError:
                        expected = ""
                    token = body.get("token")
                    if not expected or not isinstance(token, str) or not hmac.compare_digest(token, expected):
                        failures.append(time.time())
                        self._reply(handler, 401, {"error": "That setup token was not accepted."})
                        return True
                    self._purge()
                    if len(self.sessions) >= 20:
                        self.sessions.pop(next(iter(self.sessions)))
                    value = secrets.token_urlsafe(32)
                    key = hashlib.sha256(value.encode()).hexdigest()
                    self.sessions[key] = {"csrf": secrets.token_urlsafe(32), "expires": time.time() + SESSION_SECONDS,
                                          "admin_hash": hashlib.sha256(expected.encode()).hexdigest()}
                self._reply(handler, 200, {"signed_in": True}, cookie=self._cookie(value, secure))
                return True
            if not session:
                self._reply(handler, 401, {"error": "Sign in with your local setup token to continue."})
                return True
            if method == "POST" and not hmac.compare_digest(handler.headers.get("X-Setup-CSRF", ""), session["csrf"]):
                self._reply(handler, 403, {"error": "This setup session needs to be refreshed. Reload the page."})
                return True
            if method == "GET" and route == "/setup/status":
                self._reply(handler, 200, self._status(session))
                return True
            if method == "POST":
                body = self._body(handler, 8 * 1024 * 1024 if route == "/setup/content/add" else MAX_BODY)
                if route == "/setup/logout":
                    with self.lock:
                        self.sessions.pop(session_key, None)
                    result = {"signed_out": True}
                    self._reply(handler, 200, result, cookie=self._cookie("", secure, clear=True))
                    return True
                if route == "/setup/settings":
                    result = self._save_settings(body)
                elif route == "/setup/phone/invite":
                    result = self._invites().create(body)
                elif route == "/setup/phone/invite/revoke":
                    result = self._invites().revoke(body)
                elif route == "/setup/google/client":
                    result = self._save_google_client(body)
                elif route == "/setup/google/start":
                    result = self._oauth_start(body, session_key)
                elif route == "/setup/calendars":
                    person = body.get("person")
                    if person not in PEOPLE:
                        raise SetupError("Choose Chris or Lisa's connected account.")
                    result = {"calendars": self.callbacks.get("setup_calendars", lambda p: [])(person), "verified": True}
                elif route == "/setup/kids-preview":
                    person, calendar_id = body.get("person"), body.get("calendar_id")
                    if person not in PEOPLE or not isinstance(calendar_id, str):
                        raise SetupError("Choose a connected account and its custody calendar.")
                    result = self.callbacks.get("setup_kids_preview", lambda p, c: {"available": False})(person, calendar_id)
                elif route == "/setup/openai/key":
                    value = body.get("value", "")
                    if not isinstance(value, str) or not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,250}", value):
                        raise SetupError("Enter your own OpenAI API key. It is optional and stored only on this board.")
                    path = self.data_dir / "openai.key"
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    temporary = path.with_name(".openai-" + secrets.token_hex(8))
                    try:
                        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(descriptor, "w", encoding="ascii") as file:
                            file.write(value + "\n")
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
                    result = {"saved": True, "verified": False}
                elif route == "/setup/openai/test":
                    try:
                        value = (self.data_dir / "openai.key").read_text().strip()
                    except OSError:
                        raise SetupError("Save an API key before testing this optional connection.") from None
                    response = _json_request(OPENAI_RESPONSES, payload={"model": self.config_getter().get("openai_model", "gpt-5-mini"), "input": "Reply with OK.", "max_output_tokens": 128, "store": False}, bearer=value)
                    if not isinstance(response.get("id"), str):
                        raise SetupError("OpenAI did not return a completed API response.")
                    result = {"verified": True, "tested_at": int(time.time())}
                elif route == "/setup/phone/pair":
                    person = body.get("person")
                    self._consent(body, person)
                    if person not in PEOPLE:
                        raise SetupError("Choose whose phone you are pairing.")
                    public = self.config_getter().get("public_url", "").rstrip("/")
                    if not public.startswith("https://"):
                        raise SetupError("Configure your public HTTPS address before pairing a background phone connection.")
                    from .presence import PresenceStore
                    token = PresenceStore(self.data_dir, self.config_getter).rotate(person)
                    result = {"person": person, "token": token, "endpoint": public + "/api/v1/presence/" + person,
                              "one_time_reveal": True}
                    self._record_consent(person, "phone-presence")
                elif route == "/setup/notifications/pair":
                    person = body.get("person")
                    self._consent(body, person)
                    if person not in PEOPLE:
                        raise SetupError("Choose whose notifications you are pairing.")
                    public = self.config_getter().get("public_url", "").rstrip("/")
                    if not public.startswith("https://"):
                        raise SetupError("Configure public HTTPS before pairing phone notifications.")
                    from .messages import MessageFeeds
                    token = MessageFeeds(self.data_dir, self.config_getter).rotate_notification_token(person)
                    result = {"person": person, "token": token, "endpoint": public + "/api/v1/notifications/" + person,
                              "one_time_reveal": True}
                    self._record_consent(person, "phone-notifications")
                elif route == "/setup/content/add":
                    self._consent(body, "lisa")
                    callback = self.callbacks.get("content_add")
                    if not callback:
                        raise SetupError("Curated source cards are not available in this build.")
                    result = callback(body.get("item", {}))
                elif route == "/setup/content/generate":
                    self._consent(body, "lisa")
                    callback = self.callbacks.get("content_generate")
                    if not callback:
                        raise SetupError("Optional inspiration generation is not available in this build.")
                    result = callback()
                elif route == "/setup/layover":
                    callback = self.callbacks.get("setup_layover")
                    if not callback:
                        raise SetupError("Manual layover details are not available in this build.")
                    result = callback(body.get("item", {}))
                elif route == "/setup/finish":
                    private_json(self.data_dir / "setup-complete.json", {"completed_at": int(time.time())})
                    result = {"completed": True}
                else:
                    self._reply(handler, 404, {"error": "That setup action is not available."})
                    return True
                self._reply(handler, 200, result)
                return True
            self._reply(handler, 405, {"error": "Use the setup page for this action."})
        except SetupError as exc:
            if parsed.path == "/setup/oauth/callback":
                self._reply(handler, 400, '<!doctype html><meta charset="utf-8"><title>Connection needs attention</title><p>' + html.escape(str(exc)) + '</p><a href="/setup">Return to setup</a>', content_type="text/html; charset=utf-8")
            else:
                self._reply(handler, 400, {"error": str(exc)})
        except Exception:
            self._reply(handler, 400, {"error": "Setup could not complete that step. Check the connection and try again. Saved secrets are not shown."})
        return True
