"""One-use Lisa phone invitation, isolated from the administrator setup session."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.cookies import SimpleCookie
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from .onboarding import SetupError
from .presence import PresenceStore
from .shopping import _file_lock, _read_json, _write_json, _stamp, ShoppingError

COOKIE = "FlightWallLisaSetup"
SESSION_SECONDS = 1800
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{40,100}")
UTC = timezone.utc


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PhoneInviteRouter:
    """Compose with OnboardingRouter; create/revoke must remain admin-only calls."""
    def __init__(self, parent, *, presence=None):
        self.parent = parent
        self.directory = parent.data_dir / "phone-invites"
        self.path = self.directory / "lisa.json"
        self.presence = presence or PresenceStore(parent.data_dir, parent.config_getter)
        self.lock = threading.RLock()
        self.sessions = {}
        self.failures = deque(maxlen=20)

    def _locked(self):
        return _file_lock(self.directory, ".lock", self.lock)

    def _origin(self):
        origin = self.parent.config_getter().get("public_url", "").rstrip("/")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or "\\" in origin):
            raise SetupError("Configure the board's public HTTPS address before making a phone invitation.")
        return origin

    def _admin_hash(self):
        try:
            token = (self.parent.data_dir / "control-token.txt").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            token = ""
        if not token:
            raise SetupError("The board's setup credential is unavailable.")
        return _digest(token)

    def _read(self):
        state = _read_json(self.path, None)
        keys = {"version", "id", "token_hash", "origin", "admin_hash", "created_at", "expires_at",
                "used_at", "revoked_at", "pairing_started_at", "paired_at", "phone_hash"}
        if state is None:
            return None
        if (not isinstance(state, dict) or set(state) != keys or state["version"] != 1
                or not isinstance(state["id"], str) or not re.fullmatch(r"[a-f0-9]{24}", state["id"])
                or any(not isinstance(state[key], str) or not re.fullmatch(r"[a-f0-9]{64}", state[key])
                       for key in ("token_hash", "admin_hash"))
                or not isinstance(state["origin"], str)
                or state["phone_hash"] is not None and (not isinstance(state["phone_hash"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", state["phone_hash"]))):
            raise SetupError("The phone invitation needs to be recreated by the owner.")
        for key in ("created_at", "expires_at", "used_at", "revoked_at", "pairing_started_at", "paired_at"):
            if (state[key] is None and key in ("created_at", "expires_at")
                    or state[key] is not None and (type(state[key]) not in (int, float)
                        or not 0 < state[key] < 253402300799)):
                raise SetupError("The phone invitation needs to be recreated by the owner.")
        return state

    def _valid(self, state):
        return bool(state and state["revoked_at"] is None and state["expires_at"] > time.time()
                    and state["origin"] == self._origin()
                    and hmac.compare_digest(state["admin_hash"], self._admin_hash()))

    def create(self, body):
        """Called only after the parent's administrator session and CSRF checks."""
        if (not isinstance(body, dict) or set(body) - {"person", "phone_type", "expires_minutes"}
                or body.get("person", "lisa") != "lisa" or body.get("phone_type", "iphone") != "iphone"):
            raise SetupError("This invitation is only for Lisa's iPhone.")
        minutes = body.get("expires_minutes", 60)
        if type(minutes) is not int or not 15 <= minutes <= 1440:
            raise SetupError("Choose an invitation lifetime of 15 to 1440 minutes.")
        origin, admin_hash, now = self._origin(), self._admin_hash(), time.time()
        token = secrets.token_urlsafe(32)
        state = {"version": 1, "id": secrets.token_hex(12), "token_hash": _digest(token),
                 "origin": origin, "admin_hash": admin_hash, "created_at": now,
                 "expires_at": now + minutes * 60, "used_at": None, "revoked_at": None,
                 "pairing_started_at": None, "paired_at": None, "phone_hash": None}
        with self._locked():
            _write_json(self.path, state)
            self.sessions.clear()
        return {"id": state["id"], "url": origin + "/setup/lisa#invite=" + token,
                "expires_at": state["expires_at"], "person": "lisa", "one_use": True}

    def revoke(self, body):
        """Invalidate invitation/session; this does not remove an already paired phone."""
        if not isinstance(body, dict) or set(body) != {"id"} or not isinstance(body["id"], str):
            raise SetupError("Choose the invitation ID to revoke.")
        with self._locked():
            state = self._read()
            if not state or not hmac.compare_digest(state["id"], body["id"]):
                raise SetupError("That phone invitation is no longer current.")
            state["revoked_at"] = time.time()
            _write_json(self.path, state)
            self.sessions.clear()
        return {"revoked": True, "phone_credential_changed": False}

    @staticmethod
    def _cookie(value, clear=False):
        return (COOKIE + "=" + value + "; Path=/setup/lisa; HttpOnly; Secure; SameSite=Strict; Max-Age="
                + ("0" if clear else str(SESSION_SECONDS)))

    def _session(self, handler):
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get("Cookie", ""))
            token = cookie[COOKIE].value
            if not TOKEN_PATTERN.fullmatch(token):
                return None
            key = _digest(token)
        except Exception:
            return None
        with self._locked():
            self.sessions = {key: value for key, value in self.sessions.items() if value["expires"] > time.time()}
            session = self.sessions.get(key)
            state = self._read()
            if session and self._valid(state) and session["invite_id"] == state["id"] and state["used_at"] is not None:
                return session
            return None

    def _exchange(self, body, origin):
        token = body.get("token") if isinstance(body, dict) else None
        if set(body) != {"token"} or not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise SetupError("Ask Chris for a new phone invitation link.")
        with self._locked():
            now = time.time()
            while self.failures and self.failures[0] < now - 900:
                self.failures.popleft()
            if len(self.failures) >= 20:
                raise SetupError("Too many invitation attempts. Wait 15 minutes before trying again.")
            state = self._read()
            if (not self._valid(state) or state["origin"] != origin or state["used_at"] is not None
                    or not hmac.compare_digest(_digest(token), state["token_hash"])):
                self.failures.append(now)
                raise SetupError("This invitation was used, expired or revoked. Ask Chris for a new link.")
            state["used_at"] = now
            _write_json(self.path, state)
            cookie = secrets.token_urlsafe(32)
            session = {"invite_id": state["id"], "csrf": secrets.token_urlsafe(32),
                       "expires": min(state["expires_at"], now + SESSION_SECONDS)}
            self.sessions[_digest(cookie)] = session
            return {"csrf": session["csrf"], "person": "lisa", "expires_at": session["expires"]}, cookie

    def _pair(self, body, session):
        if (set(body) != {"owner_consent", "consenting_person", "phone_type"}
                or body.get("owner_consent") is not True or body.get("consenting_person") != "lisa"
                or body.get("phone_type") != "iphone"):
            raise SetupError("Lisa must approve pairing her own iPhone before continuing.")
        with self._locked():
            state = self._read()
            if not self._valid(state) or state["id"] != session["invite_id"]:
                raise SetupError("This phone invitation is no longer valid.")
            if state["pairing_started_at"] is not None:
                raise SetupError("This invitation already created its phone credential. Ask Chris for a new link if needed.")
            save = self.parent.callbacks.get("setup_save")
            if not save:
                raise SetupError("Phone setup is unavailable on this board.")
            # Reserve the one credential operation before mutating presence.
            # A failed/crashed response requires a new invitation, not a retry.
            state["pairing_started_at"] = time.time()
            _write_json(self.path, state)
            token = self.presence.rotate("lisa")
            save({"presence_enabled": True, "lisa_phone_type": "iphone"})
            self.parent._record_consent("lisa", "phone-presence")
            state["paired_at"] = time.time()
            state["phone_hash"] = _digest(token)
            _write_json(self.path, state)
            endpoint = state["origin"] + "/api/v1/presence/lisa"
            config = {"_type": "configuration", "mode": 3, "auth": True, "url": endpoint,
                      "username": "lisa", "password": token, "tid": "LI", "deviceId": "iphone",
                      "monitoring": 1, "days": 0, "allowinvalidcerts": False,
                      "remoteConfiguration": False, "allowRemoteLocation": False, "cmd": False}
            return {"person": "lisa", "username": "lisa", "password": token, "endpoint": endpoint,
                    "configuration": config, "one_time_reveal": True, "verified": False}

    def _status(self, session):
        result = {"person": "lisa", "csrf": session["csrf"], "paired": False,
                  "verified": False, "state": "unknown", "observed_at": None}
        with self._locked():
            state = self._read()
            if not self._valid(state) or state["id"] != session["invite_id"]:
                raise SetupError("This phone invitation is no longer valid.")
            if state["paired_at"] is None:
                return result
            result["paired"] = True
            if self.parent.config_getter().get("presence_enabled") is not True:
                return result
            try:
                token = self.presence.token_path("lisa").read_text(encoding="ascii").strip()
                if not hmac.compare_digest(state["phone_hash"], _digest(token)):
                    return result
                with self.presence._locked():
                    record = self.presence._read()["people"].get("lisa")
                now = datetime.now(UTC)
                if (not record or record.get("source") != "owntracks-location"
                        or record.get("state") not in ("home", "away", "unknown")):
                    return result
                observed = _stamp(record["observed_at"])
                if (observed.timestamp() <= state["paired_at"] or observed <= now - timedelta(minutes=5)
                        or observed > now + timedelta(seconds=120)):
                    return result
                result.update(verified=True, state=record["state"], observed_at=record["observed_at"])
            except (OSError, ValueError, TypeError, KeyError):
                pass
        return result

    def handle(self, handler, method, path):
        parsed = urlsplit(path)
        route = parsed.path.rstrip("/")
        if route != "/setup/lisa" and not route.startswith("/setup/lisa/"):
            return False
        reply = self.parent._reply
        try:
            origin, secure = self.parent._origin(handler)
            if parsed.query:
                raise SetupError("Use the original phone link; invitation credentials do not belong in a query string.")
            assets = {"/setup/lisa": ("lisa.html", "text/html; charset=utf-8"),
                      "/setup/lisa/app.js": ("lisa.js", "text/javascript; charset=utf-8"),
                      "/setup/lisa/style.css": ("lisa.css", "text/css; charset=utf-8")}
            if method == "GET" and route in assets:
                filename, mime = assets[route]
                reply(handler, 200, (self.parent.asset_dir / filename).read_bytes(), content_type=mime)
                return True
            if not secure or origin != self._origin():
                raise SetupError("Open Lisa's original HTTPS invitation link on her iPhone.")
            if method == "POST" and handler.headers.get("Origin") != origin:
                reply(handler, 403, {"error": "Use this same phone setup page for the request."})
                return True
            if method == "POST" and route == "/setup/lisa/exchange":
                result, cookie = self._exchange(self.parent._body(handler, 2048), origin)
                reply(handler, 200, result, cookie=self._cookie(cookie))
                return True
            session = self._session(handler)
            if not session:
                reply(handler, 401, {"error": "Open an unused phone invitation link to continue."})
                return True
            if method == "POST" and not hmac.compare_digest(handler.headers.get("X-Lisa-CSRF", ""), session["csrf"]):
                reply(handler, 403, {"error": "Reload Lisa's phone setup page before continuing."})
                return True
            if method == "GET" and route == "/setup/lisa/status":
                reply(handler, 200, self._status(session))
            elif method == "POST" and route == "/setup/lisa/pair":
                reply(handler, 200, self._pair(self.parent._body(handler, 2048), session))
            elif method == "POST" and route == "/setup/lisa/leave":
                with self.lock:
                    self.sessions = {key: value for key, value in self.sessions.items() if value is not session}
                reply(handler, 200, {"closed": True}, cookie=self._cookie("", clear=True))
            else:
                reply(handler, 404, {"error": "This page only pairs Lisa's iPhone."})
        except (SetupError, ShoppingError) as exc:
            reply(handler, 400, {"error": str(exc)})
        except Exception:
            reply(handler, 400, {"error": "Phone setup could not finish. Ask Chris for a new invitation; no secrets are shown."})
        return True
