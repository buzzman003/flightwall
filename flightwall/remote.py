"""Small authenticated maintenance server; no shell or filesystem MCP tools.

The HTTP server is loopback-only. A user-configured HTTPS reverse proxy is
required for access from another machine. OAuth is a separate authorization
server hosted alongside the MCP resource server, using public clients + PKCE.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from . import __version__
from .personal import PersonalError
from .card_preferences import CARD_DEFAULTS
from .presence import PresenceStore, PresenceError, PresenceDisabled, OWNTRACKS_REPORTS
from .config import private_json

MAX_BODY = 65536
SCOPE = "flightwall.control"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SETTING_TYPES = {
    "card_seconds": "string",
    **{key: "boolean" for key in CARD_DEFAULTS},
    "national_radar_scope": "string", "national_radar_flight_only": "boolean",
    "airline": "string", "timezone": "string", "rotation_seconds": "integer",
    "brightness": "number", "glow": "number", "dot_gap": "number",
    "led_effect": "boolean", "show_deadheads": "boolean", "takeover_hours": "integer",
    "sleep_start": "string", "sleep_end": "string", "sleep_enabled": "boolean",
    "nearby_enabled": "boolean", "home_latitude": "nullable_number",
    "home_longitude": "nullable_number", "nearby_radius_nm": "integer", "nearby_radius_miles": "nullable_number",
    "weather_enabled": "boolean",
    "personal_enabled": "boolean", "appointment_duration_minutes": "integer",
    "dinner_start": "string", "dinner_end": "string",
    "guest_mode": "boolean", "flight_focus_seconds": "integer",
    "flight_window_before_minutes": "integer", "flight_window_after_minutes": "integer",
    "display_scale_mode": "string", "timers_enabled": "boolean",
    "custody_outlook_enabled": "boolean", "custody_outlook_days": "integer",
    "flight_preview_days": "integer",
    "local_visits_enabled": "boolean", "shopping_ticker_enabled": "boolean",
    "shopping_ticker_visible": "boolean",
}
_SECRET_KEYS = re.compile(r"(token|secret|password|credential|api.?key|private.?key)", re.I)
_PKCE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _private_json(path, value):
    """Atomic writes with private permissions, including the temporary file."""
    private_json(path, value)


def _public_base(value):
    if not value:
        return ""
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError("public_url must be an HTTPS origin, for example https://wall.example.com")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("Invalid public_url port") from exc
    return urlunsplit(("https", parts.netloc.lower(), "", "", ""))


def _redirect_ok(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parts = urlsplit(value)
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        return False
    try:
        parts.port
        loopback = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = parts.hostname == "localhost"
    return parts.scheme == "https" or (parts.scheme == "http" and loopback)


class RemoteServer:
    """Callbacks: get_status(), update_settings(dict), refresh_calendar(), check_updates().

    Optional voice bridge callbacks: create_personal_item(dict),
    cancel_personal_item(dict), get_voice_context(). The bridge has a separate
    local bearer token and does not grant access to MCP or flight status.

    Start is nonblocking; callbacks run on HTTP worker threads. The application
    must validate settings, synchronize its own state, and return public data.
    public_url changes require a service restart to invalidate old audiences.
    """

    def __init__(self, data_dir, config_getter, callbacks, host="127.0.0.1", port=None):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("RemoteServer must bind to IPv4 loopback; use an HTTPS reverse proxy")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_getter = config_getter
        self.callbacks = callbacks
        self.presence = PresenceStore(data_dir, config_getter)
        self._onboarding = None
        self._notification_feeds = None
        config = config_getter()
        self.public_url = _public_base(config.get("public_url", ""))
        self.resource = self.public_url + "/mcp" if self.public_url else ""
        self.api_key_path = self.data_dir / "control-token.txt"
        if not self.api_key_path.exists():
            fd = os.open(self.api_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
        os.chmod(self.api_key_path, 0o600)
        self._api_key = self.api_key_path.read_text(encoding="ascii").strip()
        if len(self._api_key) < 32:
            raise ValueError("control-token.txt must contain a secret of at least 32 characters")
        self.alexa_token_path = self.data_dir / "alexa-token.txt"
        if not self.alexa_token_path.exists():
            fd = os.open(self.alexa_token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
        os.chmod(self.alexa_token_path, 0o600)
        self._alexa_key = self.alexa_token_path.read_text(encoding="ascii").strip()
        if len(self._alexa_key) < 32 or hmac.compare_digest(self._alexa_key, self._api_key):
            raise ValueError("alexa-token.txt must contain a separate secret of at least 32 characters")
        self._lock = threading.RLock()
        self._state_path = self.data_dir / "remote-oauth.json"
        if self._state_path.exists():
            self._state = json.loads(self._state_path.read_text(encoding="utf-8"))
            os.chmod(self._state_path, 0o600)
        else:
            self._state = {"clients": {}, "access": {}, "refresh": {}, "issuer": self.public_url}
        # Tokens are bound to this configured issuer, never the HTTP Host header.
        if self._state.get("issuer") != self.public_url:
            self._state = {"clients": {}, "access": {}, "refresh": {}, "issuer": self.public_url}
        self._pending, self._codes = {}, {}
        self._failed_approvals = []
        self._server = _HTTPServer((host, int(config.get("control_port", 8765) if port is None else port)), self._handler_class())
        self._server.daemon_threads = True
        self._thread = None
        self._save()

    @property
    def address(self):
        return self._server.server_address

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, name="flightwall-control", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        if self._thread:
            self._server.shutdown()
            self._thread.join(timeout=5)
        self._server.server_close()

    def revoke_oauth(self):
        """Local CLI operation: disconnect every OAuth client immediately."""
        with self._lock:
            self._state["access"].clear()
            self._state["refresh"].clear()
            self._codes.clear()
            self._pending.clear()
            self._save()

    def _save(self):
        _private_json(self._state_path, self._state)

    def _onboarding_dispatch(self, handler, method, path):
        if path.casefold() != "/setup" and not path.startswith("/setup/"):
            return False
        with self._lock:
            if self._onboarding is None:
                from .onboarding import OnboardingRouter
                self._onboarding = OnboardingRouter(self.data_dir, self.config_getter, self.callbacks)
        # Preserve OAuth state/code and let scoped pages reject secret-bearing
        # query strings. Route selection above intentionally uses only the path.
        return self._onboarding.handle(handler, method, handler.path)

    def _authenticate_notification(self, person, token):
        with self._lock:
            if self._notification_feeds is None:
                from .messages import MessageFeeds
                self._notification_feeds = MessageFeeds(self.data_dir, self.config_getter)
        return self._notification_feeds.authenticate_notification(person, token)

    def _cleanup(self):
        now = time.time()
        for table in (self._pending, self._codes, self._state["access"], self._state["refresh"]):
            for key in [key for key, value in table.items() if value["expires"] <= now]:
                del table[key]
        for key, client in list(self._state["clients"].items()):
            if not client.get("approved") and client["created"] + 86400 < now:
                del self._state["clients"][key]
        self._failed_approvals = [stamp for stamp in self._failed_approvals if stamp > now - 600]

    def _redact(self, value):
        if isinstance(value, dict):
            return {str(key): self._redact(item) for key, item in value.items() if not _SECRET_KEYS.search(str(key))}
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            return value.replace(self._api_key, "[redacted]").replace(self._alexa_key, "[redacted]")
        return value

    def _invoke_personal(self, name, payload=None):
        """HTTP-only bridge; the application validates dates and stores dedup keys."""
        operations = {"start_timer", "cancel_timer", "get_timers", "create_flight_watch",
                      "get_shopping_list", "add_shopping_item", "remove_shopping_item",
                      "clear_shopping_list", "show_shopping_list", "select_dinner_option"}
        if name not in ("create_personal_item", "cancel_personal_item", "get_voice_context") and name not in operations:
            raise ValueError("Unknown voice operation")
        callback = self.callbacks.get(name)
        if callback is None:
            raise LookupError("Personal-item bridge is not available")
        if name in operations:
            # Explicit HTTP-only capabilities. OAuth/MCP and phone tokens cannot
            # use these callbacks; none returns a calendar or private room card.
            if name in {"get_timers", "get_shopping_list"}:
                return self._redact(callback())
            allowed = {
                "start_timer": {"label", "duration_seconds", "request_id", "source"},
                "cancel_timer": {"id", "request_id"},
                "create_flight_watch": {"airline", "flight_number", "date", "request_id", "source"},
                "add_shopping_item": {"item", "request_id", "source"},
                "remove_shopping_item": {"item", "id", "request_id", "source"},
                "clear_shopping_list": {"confirmed", "request_id"},
                "show_shopping_list": {"visible", "request_id"},
                "select_dinner_option": {"option", "title", "date", "generation", "request_id"},
            }[name]
            if not isinstance(payload, dict) or not payload or payload.keys() - allowed:
                raise ValueError("Invalid household operation")
            if name == "select_dinner_option" and set(payload) not in (
                    {"option", "date", "generation", "request_id"},
                    {"title", "date", "generation", "request_id"}):
                raise ValueError("Choose one dinner number or displayed title with its current date and generation")
            return self._redact(callback(payload))
        if name == "get_voice_context":
            value = callback()
            if not isinstance(value, dict):
                raise ValueError("Invalid voice context")
            # This credential must never provide a flight roster or full config.
            types = {"timezone": str, "appointment_duration_minutes": int,
                     "dinner_start": str, "dinner_end": str,
                     "dinner_tomorrow_preview": bool, "dinner_requires_kids_home": bool,
                     "dinner_options_date": str, "dinner_options_generation": str}
            value = {key: value[key] for key, expected in types.items()
                     if key in value and type(value[key]) is expected}
        else:
            if not isinstance(payload, dict) or not payload or len(payload) > 20:
                raise ValueError("Personal-item payload must be a nonempty object")
            allowed = ({"kind", "title", "date", "time", "duration_minutes", "source", "request_id",
                        "sides", "ready_time", "ready_window_start", "ready_window_end"}
                       if name == "create_personal_item" else {"id", "request_id"})
            if payload.keys() - allowed:
                raise ValueError("Unsupported personal-item fields")
            for key, value in payload.items():
                if key == "duration_minutes":
                    if type(value) is not int:
                        raise ValueError("Invalid duration")
                elif not isinstance(value, str) or len(value) > (160 if key == "title" else 512):
                    raise ValueError("Invalid personal-item text")
            value = callback(payload)
        return self._redact(value if value is not None else {"ok": True})

    def _invoke(self, name, arguments):
        if name not in ("get_status", "update_settings", "refresh_calendar", "check_updates"):
            raise ValueError("Unknown tool")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        if name == "update_settings":
            if set(arguments) != {"settings"} or not isinstance(arguments["settings"], dict):
                raise ValueError("Provide a settings object")
            settings = arguments["settings"]
            if not settings or set(settings) - SETTING_TYPES.keys():
                raise ValueError("Unsupported or empty settings; security and update trust settings are local-only")
            for key, value in settings.items():
                expected = SETTING_TYPES[key]
                valid = ((expected == "boolean" and type(value) is bool)
                         or (expected == "integer" and type(value) is int)
                         or (expected == "number" and type(value) in (int, float) and math.isfinite(value))
                         or (expected == "nullable_number" and (value is None or type(value) in (int, float) and math.isfinite(value)))
                         or (expected == "string" and isinstance(value, str) and len(value) <= 512))
                if not valid:
                    raise ValueError("Invalid type for setting " + key)
            result = self.callbacks[name](settings)
        else:
            if arguments:
                raise ValueError("This tool takes no arguments")
            result = self.callbacks[name]()
        return self._redact(result if result is not None else {"ok": True})

    def _authorized(self, header):
        if not header.startswith("Bearer "):
            return False
        token = header[7:]
        if len(token) > 2048 or not token.isascii():
            return False
        if hmac.compare_digest(token, self._api_key):
            return True
        try:
            hashed = _digest(token)
        except UnicodeEncodeError:
            return False
        with self._lock:
            grant = self._state["access"].get(hashed)
            return bool(grant and grant["expires"] > time.time() and grant["resource"] == self.resource
                        and SCOPE in grant["scope"].split())

    def _static_authorized(self, header):
        if not header.startswith("Bearer "):
            return False
        token = header[7:]
        return token.isascii() and len(token) <= 2048 and hmac.compare_digest(token, self._api_key)

    def _personal_authorized(self, header):
        if not header.startswith("Bearer "):
            return False
        token = header[7:]
        if len(token) > 2048 or not token.isascii():
            return False
        return hmac.compare_digest(token, self._alexa_key) or hmac.compare_digest(token, self._api_key)

    def _mint(self, client, scope):
        self._cleanup()
        if len(self._state["access"]) >= 256 or len(self._state["refresh"]) >= 128:
            raise ValueError("Token limit reached; revoke unused connections locally")
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        common = {"client_id": client, "resource": self.resource, "scope": scope}
        self._state["access"][_digest(access)] = dict(common, expires=time.time() + 3600)
        # Refresh tokens expire after 30 days; each refresh rotates the secret.
        self._state["refresh"][_digest(refresh)] = dict(common, expires=time.time() + 30 * 86400)
        self._save()
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600, "scope": scope}

    def _handler_class(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "FlightWall"
            sys_version = ""

            def log_message(self, *_args):
                pass  # Authorization URLs, calendar details and tokens must not enter logs.

            def _reply(self, code, value=None, headers=None, content_type="application/json"):
                payload = (json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
                           if content_type == "application/json" and value is not None
                           else (value.encode("utf-8") if isinstance(value, str) else b""))
                self.send_response(code)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                if self.close_connection:
                    # Tell HTTP/1.1 clients before they consider this socket
                    # reusable, including early authentication/body rejection.
                    self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
                for key, item in (headers or {}).items():
                    self.send_header(key, str(item))
                self.end_headers()
                self.wfile.write(payload)

            def _guard(self):
                origin = self.headers.get("Origin")
                allowed = {f"http://127.0.0.1:{owner.address[1]}", f"http://localhost:{owner.address[1]}"}
                if owner.public_url:
                    allowed.add(owner.public_url)
                if origin is not None and origin not in allowed:
                    self.close_connection = True
                    self._reply(403, {"error": "Origin not allowed"})
                    return False
                if len(self.path) > 8192:
                    self.close_connection = True
                    self._reply(414, {"error": "Request URI too long"})
                    return False
                return True

            def _auth(self, personal=False, static=False):
                authorization = self.headers.get("Authorization", "")
                accepted = (owner._static_authorized(authorization) if static else
                            owner._personal_authorized(authorization) if personal else owner._authorized(authorization))
                if accepted:
                    if (personal and not hmac.compare_digest(authorization[7:], owner._api_key)
                            and not owner.config_getter().get("alexa_enabled", False)):
                        self.close_connection = True
                        self._reply(503, {"error": "Alexa bridge is disabled; enable it locally on the display computer"})
                        return False
                    return True
                challenge = ('Bearer realm="flightwall-display"' if static else
                             'Bearer realm="flightwall-personal"' if personal else 'Bearer realm="flightwall"')
                if owner.public_url and not personal and not static:
                    challenge += ', resource_metadata="' + owner.public_url + '/.well-known/oauth-protected-resource/mcp", scope="' + SCOPE + '"'
                self.close_connection = True
                self._reply(401, {"error": "Authentication required"}, {"WWW-Authenticate": challenge})
                return False

            def _body(self, form=False):
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Chunked requests are not supported")
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError as exc:
                    raise ValueError("Content-Length required") from exc
                if not 0 <= length <= MAX_BODY:
                    raise ValueError("Body must be at most 65536 bytes")
                expected = "application/x-www-form-urlencoded" if form else "application/json"
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != expected:
                    raise ValueError("Expected " + expected)
                body = self.rfile.read(length).decode("utf-8")
                if form:
                    values = parse_qs(body, keep_blank_values=True, max_num_fields=32)
                    if any(len(items) != 1 for items in values.values()):
                        raise ValueError("Duplicate form fields")
                    return {key: items[0] for key, items in values.items()}
                return json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))

            def do_GET(self):
                if not self._guard():
                    return
                path = urlsplit(self.path).path
                if self._setup_dispatch("GET", path):
                    return
                if path == "/health":
                    return self._reply(200, {"ok": True, "service": "flightwall", "version": __version__})
                if path.startswith("/.well-known/"):
                    if not owner.public_url:
                        return self._reply(503, {"error": "Configure public_url locally before using OAuth"})
                    if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
                        return self._reply(200, {"resource": owner.resource, "authorization_servers": [owner.public_url],
                            "resource_name": "Flight Wall maintenance", "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"]})
                    if path == "/.well-known/oauth-authorization-server":
                        return self._reply(200, {"issuer": owner.public_url,
                            "authorization_endpoint": owner.public_url + "/oauth/authorize",
                            "token_endpoint": owner.public_url + "/oauth/token",
                            "registration_endpoint": owner.public_url + "/oauth/register",
                            "revocation_endpoint": owner.public_url + "/oauth/revoke",
                            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                            "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
                            "scopes_supported": [SCOPE, "offline_access"], "authorization_response_iss_parameter_supported": True})
                if path == "/oauth/authorize":
                    return self._authorize_get()
                if path == "/mcp":
                    if self._auth():
                        self._reply(405, {"error": "This stateless MCP endpoint accepts POST only"}, {"Allow": "POST"})
                    return
                if path == "/api/v1/status":
                    if self._auth():
                        self._action("get_status", {})
                    return
                if path == "/api/v1/display":
                    if self._auth(static=True):
                        callback = owner.callbacks.get("get_display_status")
                        if callback is None:
                            return self._reply(503, {"error": "Display status is not available"})
                        try:
                            self._reply(200, owner._redact(callback()))
                        except Exception:
                            self._reply(500, {"error": "Display status failed; inspect the local application"})
                    return
                if path == "/api/v1/voice-context":
                    if self._auth(personal=True):
                        self._personal_action("get_voice_context")
                    return
                household_reads = {"/api/v1/timers": "get_timers", "/api/v1/shopping-list": "get_shopping_list"}
                if path in household_reads:
                    if self._auth(personal=True):
                        self._personal_action(household_reads[path])
                    return
                self._reply(404, {"error": "Not found"})

            def do_POST(self):
                if not self._guard():
                    return
                path = urlsplit(self.path).path
                try:
                    if self._setup_dispatch("POST", path):
                        return
                    device = re.fullmatch(r"/api/v1/(presence|notifications)/(chris|lisa)", path)
                    if device:
                        return self._device_action(device[1], device[2])
                    if path.startswith("/oauth/"):
                        if not owner.public_url:
                            return self._reply(503, {"error": "Configure public_url locally before using OAuth"})
                        if path == "/oauth/register":
                            return self._register(self._body())
                        if path == "/oauth/authorize":
                            return self._authorize_post(self._body(form=True))
                        if path == "/oauth/token":
                            return self._token(self._body(form=True))
                        if path == "/oauth/revoke":
                            return self._revoke(self._body(form=True))
                    personal_routes = {"/api/v1/personal-items": "create_personal_item",
                                       "/api/v1/dinner-options/select": "select_dinner_option",
                                       "/api/v1/personal-items/cancel": "cancel_personal_item",
                                       "/api/v1/timers": "start_timer", "/api/v1/timers/cancel": "cancel_timer",
                                       "/api/v1/flight-watches": "create_flight_watch",
                                       "/api/v1/shopping-list/add": "add_shopping_item",
                                       "/api/v1/shopping-list/remove": "remove_shopping_item",
                                       "/api/v1/shopping-list/clear": "clear_shopping_list",
                                       "/api/v1/shopping-list/show": "show_shopping_list"}
                    if path in personal_routes:
                        if not self._auth(personal=True):
                            return
                        return self._personal_action(personal_routes[path], self._body())
                    routes = {"/api/v1/settings": "update_settings", "/api/v1/calendar/refresh": "refresh_calendar", "/api/v1/updates/check": "check_updates"}
                    if path == "/mcp" or path in routes:
                        if not self._auth():
                            return
                        body = self._body()
                        if path == "/mcp":
                            return self._mcp(body)
                        return self._action(routes[path], {"settings": body} if routes[path] == "update_settings" else body)
                    self._reply(404, {"error": "Not found"})
                except (ValueError, UnicodeError, TypeError):
                    self.close_connection = True
                    self._reply(400, {"error": "Invalid request"})
                except Exception:
                    self._reply(500, {"error": "Operation failed; inspect the local application status"})

            def _setup_dispatch(self, method, path):
                try:
                    return owner._onboarding_dispatch(self, method, path)
                except Exception:
                    self._reply(503, {"error": "Setup is unavailable; inspect local application status"})
                    return True

            def _device_action(self, kind, person):
                authorization = self.headers.get("Authorization", "")
                token = authorization[7:] if authorization.startswith("Bearer ") else ""
                if kind == "presence" and authorization.startswith("Basic ") and len(authorization) <= 4096:
                    try:
                        credentials = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                        username, password = credentials.split(":", 1)
                        token = password if username == person else ""
                    except (ValueError, UnicodeError):
                        token = ""
                authenticated = (owner.presence.authenticate(person, token) if kind == "presence"
                                 else owner._authenticate_notification(person, token))
                if not authenticated:
                    self.close_connection = True
                    challenge = ('Basic realm="flightwall-presence", charset="UTF-8", Bearer realm="flightwall-presence"'
                                 if kind == "presence" else 'Bearer realm="flightwall-notifications"')
                    return self._reply(401, {"error": "Device authentication required"},
                                       {"WWW-Authenticate": challenge})
                if kind == "presence" and not owner.config_getter().get("presence_enabled", False):
                    self.close_connection = True
                    return self._reply(503, {"error": "Presence is disabled; enable it locally after opting in"})
                callback = owner.callbacks.get("update_presence" if kind == "presence" else "receive_notification")
                if callback is None:
                    return self._reply(503, {"error": "This device feature is not available"})
                try:
                    payload = self._body()
                    result = callback(person, payload)
                    # A paired phone receives an acknowledgement, never a roster,
                    # another person's presence, message summaries or coordinates.
                    if kind == "presence" and isinstance(payload, dict) and payload.get("_type") in OWNTRACKS_REPORTS:
                        return self._reply(200, [])  # OwnTracks HTTP response format.
                    ack = {"accepted": True}
                    if isinstance(result, dict):
                        for key in ("updated", "duplicate", "received"):
                            if type(result.get(key)) is bool:
                                ack[key] = result[key]
                    return self._reply(200, ack)
                except PresenceDisabled as exc:
                    return self._reply(503, {"error": owner._redact(str(exc))[:500]})
                except PresenceError as exc:
                    return self._reply(400, {"error": owner._redact(str(exc))[:500]})
                except (ValueError, TypeError):
                    return self._reply(400, {"error": "Device report rejected; check its fields and observation time"})

            def _personal_action(self, name, payload=None):
                try:
                    self._reply(200, owner._invoke_personal(name, payload))
                except PersonalError as exc:
                    self._reply(400, {"error": owner._redact(str(exc))[:1000]})
                except (ValueError, TypeError):
                    self._reply(400, {"error": "Personal-item data rejected; check the title, dates and identifier"})
                except LookupError:
                    self._reply(503, {"error": "Personal-item bridge is not available"})
                except Exception:
                    self._reply(500, {"error": "Personal-item operation failed; inspect local status"})

            def _action(self, name, arguments):
                try:
                    self._reply(200, owner._invoke(name, arguments))
                except ValueError as exc:
                    # Validation messages must never include caller-supplied secrets.
                    self._reply(400, {"error": str(exc) if str(exc).startswith(("Invalid type for setting ", "Unsupported or empty settings", "Provide a settings", "This tool takes")) else "Settings or operation rejected"})
                except Exception:
                    self._reply(500, {"error": "Operation failed; inspect local status"})

            def _mcp(self, body):
                if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
                    return self._rpc_error(None, -32600, "Invalid Request")
                request_id = body.get("id")
                if request_id is not None and (type(request_id) not in (str, int) or isinstance(request_id, str) and len(request_id) > 256):
                    return self._rpc_error(None, -32600, "Invalid request ID")
                if "method" not in body and "id" in body and ("result" in body or "error" in body):
                    return self._reply(202)
                if "id" not in body and isinstance(body.get("method"), str):
                    return self._reply(202)  # notifications and client responses are accepted, without a session.
                method, params = body.get("method"), body.get("params", {})
                if not isinstance(method, str) or not isinstance(params, dict):
                    return self._rpc_error(request_id, -32600, "Invalid Request")
                version = self.headers.get("MCP-Protocol-Version")
                if version and version not in VERSIONS:
                    return self._reply(400, {"error": "Unsupported MCP protocol version"})
                if method == "initialize":
                    offered = params.get("protocolVersion")
                    result = {"protocolVersion": offered if offered in VERSIONS else VERSIONS[0],
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "flightwall", "version": __version__},
                              "instructions": "Maintain Flight Wall settings and request calendar or signed update checks. No shell access."}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": _tools()}
                elif method == "tools/call":
                    try:
                        value = owner._invoke(params.get("name"), params.get("arguments", {}))
                        result = {"content": [{"type": "text", "text": json.dumps(value, allow_nan=False)}], "isError": False}
                        if isinstance(value, dict):
                            result["structuredContent"] = value
                    except ValueError:
                        result = {"content": [{"type": "text", "text": "Settings or tool arguments rejected; use the advertised tool schema and valid setting ranges."}], "isError": True}
                    except Exception:
                        result = {"content": [{"type": "text", "text": "Operation failed; inspect local application status."}], "isError": True}
                else:
                    return self._rpc_error(request_id, -32601, "Method not found")
                self._reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

            def _rpc_error(self, request_id, code, message):
                self._reply(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

            def _register(self, body):
                if not isinstance(body, dict):
                    raise ValueError("Invalid registration")
                redirects = body.get("redirect_uris")
                if not isinstance(redirects, list) or not 1 <= len(redirects) <= 5 or not all(_redirect_ok(uri) for uri in redirects):
                    return self._reply(400, {"error": "invalid_redirect_uri"})
                if body.get("token_endpoint_auth_method", "none") != "none":
                    return self._reply(400, {"error": "invalid_client_metadata", "error_description": "Only public clients with PKCE are supported"})
                with owner._lock:
                    owner._cleanup()
                    if len(owner._state["clients"]) >= 64:
                        return self._reply(429, {"error": "Registration limit reached; remove unused connections locally"})
                    client_id = secrets.token_urlsafe(24)
                    name = body.get("client_name", "MCP client")
                    if not isinstance(name, str) or len(name) > 100:
                        raise ValueError("Invalid client name")
                    owner._state["clients"][client_id] = {"redirect_uris": redirects, "name": name, "created": time.time(), "approved": False}
                    owner._save()
                self._reply(201, {"client_id": client_id, "client_id_issued_at": int(time.time()), "client_name": name,
                    "redirect_uris": redirects, "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})

            def _authorize_get(self):
                if not owner.public_url:
                    return self._reply(503, {"error": "Configure public_url locally"})
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=20)
                    if any(len(items) != 1 for items in query.values()):
                        raise ValueError("Duplicate parameters")
                    params = {key: items[0] for key, items in query.items()}
                    with owner._lock:
                        owner._cleanup()
                        client = owner._state["clients"].get(params.get("client_id"))
                        if not client or params.get("redirect_uri") not in client["redirect_uris"]:
                            raise ValueError("Invalid client or redirect")
                        if (params.get("response_type") != "code" or params.get("code_challenge_method") != "S256"
                                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", ""))
                                or params.get("resource") != owner.resource
                                or not 1 <= len(params.get("state", "")) <= 1024):
                            raise ValueError("PKCE, resource and state required")
                        scope = params.get("scope", SCOPE)
                        scopes = scope.split()
                        if SCOPE not in scopes or set(scopes) - {SCOPE, "offline_access"}:
                            return self._reply(400, {"error": "invalid_scope"})
                        if len(owner._pending) >= 128:
                            return self._reply(429, {"error": "Too many pending authorizations"})
                        pending, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                        owner._pending[pending] = dict(params, csrf=_digest(csrf), expires=time.time() + 600, scope=" ".join(sorted(set(scopes))))
                    page = ('<!doctype html><meta charset="utf-8"><title>Approve Flight Wall access</title>'
                        '<h1>Connect Flight Wall</h1><p>Client: ' + html.escape(client["name"]) + '</p>'
                        '<p>This connection can read flight display status, change display settings, refresh your calendar, '
                        'and check for signed software updates. It cannot run commands or read authentication files.</p>'
                        '<p>Approve only a connection you started. Retrieve your control token locally on your Batocera machine. '
                        'Never paste that token into a chat message.</p>'
                        '<form method="post" action="/oauth/authorize">'
                        '<input type="hidden" name="pending" value="' + pending + '">'
                        '<input type="hidden" name="csrf" value="' + csrf + '">'
                        '<label>Local control token <input type="password" name="admin_secret" autocomplete="off" required></label>'
                        '<button type="submit" name="approve" value="yes">Approve connection</button></form>')
                    self._reply(200, page, {"Set-Cookie": "fw_approval=" + csrf + "; Path=/oauth/authorize; Secure; HttpOnly; SameSite=Lax; Max-Age=600"}, "text/html")
                except (ValueError, TypeError):
                    self._reply(400, {"error": "invalid_request", "error_description": "Valid registered client, exact redirect URI, S256 PKCE, resource and state required"})

            def _authorize_post(self, body):
                cookies = SimpleCookie()
                try:
                    cookies.load(self.headers.get("Cookie", ""))
                    cookie = cookies["fw_approval"].value
                except (KeyError, ValueError):
                    cookie = ""
                with owner._lock:
                    owner._cleanup()
                    pending = owner._pending.get(body.get("pending"))
                    csrf = body.get("csrf", "")
                    if (not pending or not cookie or not hmac.compare_digest(cookie, csrf)
                            or not hmac.compare_digest(pending["csrf"], _digest(csrf))):
                        return self._reply(403, {"error": "Approval expired or CSRF check failed; restart connection"})
                    secret = body.get("admin_secret", "")
                    if not hmac.compare_digest(secret, owner._api_key):
                        owner._failed_approvals.append(time.time())
                        return self._reply(429 if len(owner._failed_approvals) >= 10 else 403, {"error": "Approval denied"})
                    if len(owner._failed_approvals) >= 10:
                        return self._reply(429, {"error": "Too many attempts; try again in 10 minutes"})
                    if body.get("approve") != "yes":
                        return self._reply(403, {"error": "Approval required"})
                    del owner._pending[body["pending"]]
                    code = secrets.token_urlsafe(48)
                    owner._codes[_digest(code)] = dict(pending, expires=time.time() + 120)
                    owner._state["clients"][pending["client_id"]]["approved"] = True
                    owner._save()
                separator = "&" if urlsplit(pending["redirect_uri"]).query else "?"
                redirect = pending["redirect_uri"] + separator + urlencode({"code": code, "state": pending["state"], "iss": owner.public_url})
                self._reply(302, None, {"Location": redirect, "Set-Cookie": "fw_approval=; Path=/oauth/authorize; Secure; HttpOnly; SameSite=Lax; Max-Age=0"})

            def _token(self, body):
                with owner._lock:
                    owner._cleanup()
                    if body.get("resource") != owner.resource:
                        return self._reply(400, {"error": "invalid_target"})
                    client = body.get("client_id")
                    if client not in owner._state["clients"]:
                        return self._reply(400, {"error": "invalid_client"})
                    if body.get("grant_type") == "authorization_code":
                        code = owner._codes.get(_digest(body.get("code", "")))
                        verifier = body.get("code_verifier", "")
                        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
                        if (not code or code["client_id"] != client or code["redirect_uri"] != body.get("redirect_uri")
                                or not _PKCE.fullmatch(verifier) or not hmac.compare_digest(code["code_challenge"], challenge)):
                            return self._reply(400, {"error": "invalid_grant"})
                        del owner._codes[_digest(body["code"])]
                        return self._reply(200, owner._mint(client, code["scope"]))
                    if body.get("grant_type") == "refresh_token":
                        hashed = _digest(body.get("refresh_token", ""))
                        grant = owner._state["refresh"].get(hashed)
                        if not grant or grant["client_id"] != client or grant["resource"] != owner.resource:
                            return self._reply(400, {"error": "invalid_grant"})
                        del owner._state["refresh"][hashed]
                        return self._reply(200, owner._mint(client, grant["scope"]))
                    self._reply(400, {"error": "unsupported_grant_type"})

            def _revoke(self, body):
                with owner._lock:
                    hashed = _digest(body.get("token", ""))
                    for table in (owner._state["access"], owner._state["refresh"]):
                        grant = table.get(hashed)
                        if grant and grant["client_id"] == body.get("client_id"):
                            del table[hashed]
                    owner._save()
                self._reply(200, {})

        return Handler


class _HTTPServer(ThreadingHTTPServer):
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self._workers = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


def _tools():
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    descriptions = {
        "get_status": "Read Flight Wall display, calendar synchronization and update status. Authentication secrets are excluded.",
        "update_settings": "Change validated display and home-location settings permanently. Calendar authorization, security, credentials and update trust remain local-only.",
        "refresh_calendar": "Request a read-only refresh from the Google Calendar already authorized on this device.",
        "check_updates": "Check the locally configured signed release feed; install according to local update policy at next safe launch.",
    }
    result = []
    for name, description in descriptions.items():
        schema = empty
        if name == "update_settings":
            properties = {key: {"type": ["number", "null"] if value == "nullable_number" else value} for key, value in SETTING_TYPES.items()}
            limits = {"rotation_seconds": (3, 300), "brightness": (0.05, 1), "glow": (0, 1),
                "dot_gap": (0, 0.65), "takeover_hours": (1, 96), "nearby_radius_nm": (1, 250), "nearby_radius_miles": (0.1, 250),
                "home_latitude": (-90, 90), "home_longitude": (-180, 180),
                "appointment_duration_minutes": (1, 1440), "flight_focus_seconds": (15, 300),
                "flight_window_before_minutes": (0, 240), "flight_window_after_minutes": (0, 240)}
            for key, (minimum, maximum) in limits.items():
                properties[key].update(minimum=minimum, maximum=maximum)
            properties["airline"].update(pattern=r"^([A-Za-z0-9]{2,3})?$", description="Airline IATA/ICAO code; empty for no override.")
            properties["timezone"]["description"] = "IANA timezone, for example America/New_York or UTC."
            properties["nearby_radius_miles"]["description"] = "Nearby radius in statute miles; null preserves the legacy nautical-mile setting."
            properties["national_radar_scope"]["enum"] = ["national", "southeast", "local", "hyperlocal"]
            properties["weather_enabled"]["description"] = "Show the local forecast; provision forecast coordinates through the local CLI."
            for key in ("sleep_start", "sleep_end", "dinner_start", "dinner_end"):
                properties[key].update(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$", description="Local 24-hour time HH:MM.")
            schema = {"type": "object", "properties": {"settings": {"type": "object", "minProperties": 1,
                "properties": properties, "additionalProperties": False}},
                "required": ["settings"], "additionalProperties": False}
        result.append({"name": name, "description": description, "inputSchema": schema,
            "annotations": {"readOnlyHint": name == "get_status", "destructiveHint": False,
                "idempotentHint": name != "check_updates", "openWorldHint": name in ("refresh_calendar", "check_updates")}})
    return result
