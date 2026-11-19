"""Separate read-only Gmail Desktop consent through a private loopback callback."""
from __future__ import annotations

import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from .agenda_google_tasks import TasksClient
from .config import private_json
from .messages import GmailClient, GMAIL_SCOPE, GMAIL_URL, MessageError, TOKEN_URL, _profile
from .personal import PersonalStore


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
MAX_CREDENTIAL_BYTES = 65536


def _desktop_client(path):
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError()
        with path.open(encoding="utf-8") as stream:
            raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise ValueError()
        value = json.loads(raw)
        client = value.get("installed") if isinstance(value, dict) else None
        if (not isinstance(client, dict)
                or not isinstance(client.get("client_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{8,230}\.apps\.googleusercontent\.com", client["client_id"])):
            raise ValueError()
        secret = client.get("client_secret")
        if secret is not None and (not isinstance(secret, str) or not 1 <= len(secret) <= 512
                                   or any(ord(char) < 33 for char in secret)):
            raise ValueError()
        # Endpoints in a downloaded document cannot redirect this flow.
        return {key: client[key] for key in ("client_id", "client_secret") if key in client}
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MessageError("Choose the downloaded Google OAuth Desktop app client JSON for Google consent.") from None


def _checked_token(response, client, service=GmailClient):
    if not isinstance(response, dict):
        raise MessageError("Google did not return valid Google read-only authorization. Try sign-in again.")
    access, refresh = response.get("access_token"), response.get("refresh_token")
    scope = response.get("scope")
    lifetime, kind = response.get("expires_in"), response.get("token_type")
    if (not isinstance(access, str) or not 1 <= len(access) <= 16384
            or not isinstance(refresh, str) or not 1 <= len(refresh) <= 16384
            or any(not 33 <= ord(char) <= 126 for char in access + refresh)
            or not isinstance(scope, str) or not service.granted_scopes(scope.split())
            or not isinstance(kind, str) or kind.casefold() != "bearer"
            or type(lifetime) is not int or not 1 <= lifetime <= 86400):
        raise MessageError("Approve only the requested read-only permission and offline access. Try sign-in again.")
    return {"client_id": client["client_id"], "access_token": access, "refresh_token": refresh,
            "expires_at": time.time() + lifetime, "scopes": list(getattr(service, "scopes", (service.scope,)))}


def _store_pair(account, client, token, prefix="gmail"):
    """Prepare both files before replacement and restore predecessors on failure."""
    if account.parent.is_symlink() or account.is_symlink():
        raise MessageError("The private Google account directory needs local repair.")
    account.mkdir(mode=0o700, parents=True, exist_ok=True)
    if account.is_symlink():
        raise MessageError("The private Google account directory needs local repair.")
    account.chmod(0o700)
    lock = PersonalStore(account, lambda: {})
    lock.lock_path = account / f"{prefix}-auth.lock"
    try:
        with lock._locked():
            targets = [account / f"{prefix}-client.json", account / f"{prefix}-token.json"]
            previous = []
            for path in targets:
                if path.is_symlink() or path.exists() and not path.is_file():
                    raise OSError()
                if path.exists() and path.stat().st_size > MAX_CREDENTIAL_BYTES:
                    raise OSError()
                previous.append(path.read_bytes() if path.exists() else None)
            with tempfile.TemporaryDirectory(prefix=f".{prefix}-oauth-", dir=account) as staging:
                staged = [Path(staging) / path.name for path in targets]
                for path, value in zip(staged, ({"installed": client}, token)):
                    private_json(path, value)
                replaced = []
                try:
                    for staged_path, target in zip(staged, targets):
                        os.replace(staged_path, target)
                        replaced.append(target)
                except OSError:
                    for target, original in zip(targets, previous):
                        if target not in replaced:
                            continue
                        if original is None:
                            target.unlink(missing_ok=True)
                        else:
                            restore = Path(staging) / (target.name + ".restore")
                            descriptor = os.open(restore, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                            with os.fdopen(descriptor, "wb") as stream:
                                stream.write(original)
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.replace(restore, target)
                    raise
    except (OSError, ValueError):
        raise MessageError("Google authorization could not be saved privately. Existing Calendar authorization is unchanged.") from None


def check_gmail_profile(data_dir, expected_email, *, person="chris"):
    """Compare the selected mailbox privately; never return its email address."""
    person = _profile(person)
    if (not isinstance(expected_email, str) or len(expected_email) > 254
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,32}", expected_email)):
        raise MessageError("Use the expected complete mailbox address for the profile check.")
    client = GmailClient(Path(data_dir) / "accounts" / person)
    try:
        if set(client._token().get("scopes", [])) != {GMAIL_SCOPE}:
            raise ValueError()
        value = client._request("GET", GMAIL_URL + "/profile?fields=emailAddress", token=client._access_token())
        actual = value.get("emailAddress") if isinstance(value, dict) else None
        if (not isinstance(actual, str) or not actual or len(actual) > 254
                or not actual.isascii()):
            raise ValueError()
        matched = hmac.compare_digest(actual.casefold(), expected_email.casefold())
    except Exception:
        raise MessageError("The Gmail profile could not be verified. Reconnect the selected person's Gmail and try again.") from None
    return {"person": person, "matches": matched, "read_only": True}


def authorize_gmail(data_dir, credentials_path, open_browser=True, *, callback_port=8766,
                    timeout=300, on_url=print, person="chris"):
    return _authorize(GmailClient, data_dir, credentials_path, open_browser, callback_port, timeout, on_url, person)

def authorize_tasks(data_dir, credentials_path, open_browser=True, *, callback_port=8766, timeout=300, on_url=print):
    return _authorize(TasksClient, data_dir, credentials_path, open_browser, callback_port, timeout, on_url, "chris")


def _authorize(service, data_dir, credentials_path, open_browser, callback_port, timeout, on_url, person):
    person = _profile(person)
    client = _desktop_client(credentials_path)
    if (type(callback_port) is not int or not 0 <= callback_port <= 65535
            or type(timeout) not in (int, float) or not 0 < timeout <= 3600 or not math.isfinite(timeout)):
        raise MessageError("Use a valid Google callback port and positive sign-in timeout.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # The callback includes a one-use authorization code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if (len(self.path) > 16384 or parsed.path != "/oauth2/callback"
                    or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"):
                self._reply(404, "Unknown Google callback.")
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            states = query.get("state", [])
            if (len(states) != 1 or not hmac.compare_digest(states[0].encode("utf-8"), state.encode("ascii"))):
                self._reply(403, "Invalid authorization state. Return to the original Google sign-in link.")
                return
            if received:
                self._reply(409, "Authorization was already received.")
            elif "error" in query:
                received["error"] = "denied"
                self._reply(400, "Google authorization was declined. You may close this tab.")
            elif (len(query.get("code", [])) == 1 and 1 <= len(query["code"][0]) <= 4096
                  and not any(ord(char) < 33 for char in query["code"][0])):
                received["code"] = query["code"][0]
                self._reply(200, "Authorization received. Return to Flight Wall to finish Google setup.")
            else:
                self._reply(400, "No valid Google authorization code was received.")

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
        raise MessageError("Google callback port is unavailable. Close the other sign-in or choose another port.") from None
    redirect = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
    url = AUTH_URL + "?" + urlencode({"client_id": client["client_id"], "redirect_uri": redirect,
            "response_type": "code", "scope": " ".join(getattr(service, "scopes", (service.scope,))), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "false"})
    try:
        on_url(url)
        if open_browser:
            try:
                webbrowser.open(url, new=1, autoraise=True)
            except (OSError, webbrowser.Error):
                pass
        deadline = time.monotonic() + timeout
        while not received and time.monotonic() < deadline:
            server.timeout = min(.5, max(.01, deadline - time.monotonic()))
            server.handle_request()
    finally:
        server.server_close()
    if not received:
        raise MessageError("Google Google authorization timed out. Start Google sign-in again.")
    if "error" in received:
        raise MessageError("Google Google authorization was declined. Existing connections are unchanged.")
    form = {"client_id": client["client_id"], "code": received["code"], "code_verifier": verifier,
            "redirect_uri": redirect, "grant_type": "authorization_code"}
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    account = Path(data_dir) / "accounts" / person
    try:
        response = service(account)._request("POST", TOKEN_URL, form=form)
    except MessageError:
        raise
    except Exception:
        raise MessageError("Google could not complete Google authorization. Existing connections are unchanged.") from None
    token = _checked_token(response, client, service)
    if service is TasksClient:
        client = TasksClient(account).verified_client(client, token["access_token"])
    _store_pair(account, client, token, service.prefix)
    return {"authorized": True, "person": person, "scopes": token["scopes"],
            "next_step": f"{service.label} consent is saved separately. Review your preferences in household setup."}
