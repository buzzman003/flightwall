"""Scoped invitation tests with fake phone storage; no phone or service contacted."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import Message
import http.client
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flightwall.onboarding import OnboardingRouter, SetupError
from flightwall.phone_invites import COOKIE, PhoneInviteRouter
from flightwall.config import ConfigStore
from flightwall.remote import RemoteServer


class Handler:
    def __init__(self, body=None, cookie=None, csrf=None, origin="https://example.test", host="example.test"):
        self.headers = Message()
        self.headers["Host"] = host
        self.headers["Origin"] = origin
        if cookie:
            self.headers["Cookie"] = cookie
        if csrf:
            self.headers["X-Lisa-CSRF"] = csrf
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(raw))
        self.rfile, self.wfile = io.BytesIO(raw), io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.output_headers = {}
        self.code = None
        self.close_connection = False

    def send_response(self, code):
        self.code = code

    def send_header(self, key, value):
        self.output_headers[key] = value

    def end_headers(self):
        pass

    def result(self):
        return json.loads(self.wfile.getvalue())


class FakePresence:
    def __init__(self, root):
        self.root = root
        self.people = {}
        self.rotations = []

    def token_path(self, person):
        return self.root / (person + ".synthetic-token")

    def rotate(self, person):
        self.rotations.append(person)
        self.people.pop(person, None)
        token = "synthetic-phone-" + "x" * 48
        self.token_path(person).write_text(token)
        return token

    def _locked(self):
        return nullcontext()

    def _read(self):
        return {"people": self.people}


class PhoneInviteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"public_url": "https://example.test", "presence_enabled": False,
                       "shopping_enabled": False, "shopping_lisa_opt_in": False, "lisa_private_enabled": False,
                       "home_latitude": "synthetic-private-pin"}
        (self.root / "control-token.txt").write_text("synthetic-admin-" + "a" * 40)
        self.saved, self.consents = [], []

        def save(values):
            self.saved.append(values.copy())
            self.config.update(values)

        self.parent = OnboardingRouter(self.root, lambda: self.config, {"setup_save": save})
        self.parent._record_consent = lambda person, scope: self.consents.append((person, scope))
        self.presence = FakePresence(self.root)
        self.router = PhoneInviteRouter(self.parent, presence=self.presence)
        self.cookie, self.csrf = None, None

    def request(self, route, body=None, *, method=None, **kwargs):
        handler = Handler(body, kwargs.pop("cookie", self.cookie), kwargs.pop("csrf", self.csrf), **kwargs)
        handled = self.router.handle(handler, method or ("GET" if body is None else "POST"), route)
        self.assertTrue(handled)
        return handler

    def create(self):
        invite = self.router.create({"person": "lisa", "phone_type": "iphone"})
        token = parse_qs(urlsplit(invite["url"]).fragment)["invite"][0]
        return invite, token

    def exchange(self):
        invite, token = self.create()
        response = self.request("/setup/lisa/exchange", {"token": token})
        self.assertEqual(response.code, 200, response.result())
        self.cookie = response.output_headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = response.result()["csrf"]
        return invite, token, response

    def pair(self):
        return self.request("/setup/lisa/pair", {"owner_consent": True, "consenting_person": "lisa", "phone_type": "iphone"})

    def test_invite_has_fragment_only_and_only_hash_is_persisted(self):
        invite, token = self.create()
        self.assertFalse(urlsplit(invite["url"]).query)
        self.assertEqual(urlsplit(invite["url"]).path, "/setup/lisa")
        stored = self.router.path.read_text()
        self.assertNotIn(token, stored)
        self.assertNotIn("synthetic-admin-", stored)
        self.assertNotIn("synthetic-private-pin", stored)

    def test_create_restricted_to_lisa_iphone_with_bounded_expiry(self):
        for body in ({"person": "chris"}, {"phone_type": "android"}, {"expires_minutes": 1},
                     {"expires_minutes": True}, {"recipient": "not-allowed"}):
            with self.assertRaises(SetupError):
                self.router.create(body)

    def test_public_assets_include_no_credentials_or_household_data(self):
        for route in ("/setup/lisa", "/setup/lisa/app.js", "/setup/lisa/style.css"):
            response = self.request(route)
            self.assertEqual(response.code, 200)
            self.assertEqual(response.output_headers["Cache-Control"], "no-store")
            self.assertEqual(response.output_headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn(b"synthetic-private-pin", response.wfile.getvalue())

    def test_exchange_is_one_use_with_separate_secure_scoped_cookie(self):
        _, token, response = self.exchange()
        cookie = response.output_headers["Set-Cookie"]
        for value in (COOKIE, "Path=/setup/lisa", "HttpOnly", "Secure", "SameSite=Strict"):
            self.assertIn(value, cookie)
        self.assertNotIn("FlightWallSetup=", cookie)
        retry = self.request("/setup/lisa/exchange", {"token": token})
        self.assertEqual(retry.code, 400)

    def test_invitation_get_does_not_consume_link_and_query_token_is_rejected(self):
        _, token = self.create()
        self.assertEqual(self.request("/setup/lisa").code, 200)
        self.assertEqual(self.request("/setup/lisa?invite=redacted").code, 400)
        self.assertEqual(self.request("/setup/lisa/exchange", {"token": token}).code, 200)

    def test_lisa_cookie_does_not_authenticate_admin_routes(self):
        self.exchange()
        handler = Handler(cookie=self.cookie)
        self.assertTrue(self.parent.handle(handler, "GET", "/setup/status"))
        self.assertEqual(handler.code, 401)
        forbidden = self.request("/setup/lisa/settings", {"home_latitude": 0})
        self.assertEqual(forbidden.code, 404)
        self.assertFalse(self.saved)

    def test_origin_csrf_and_consent_all_required(self):
        self.exchange()
        bad_origin = self.request("/setup/lisa/pair", {}, origin="https://other.test")
        self.assertEqual(bad_origin.code, 403)
        missing_csrf = self.request("/setup/lisa/pair", {}, csrf="")
        self.assertEqual(missing_csrf.code, 403)
        for body in ({}, {"owner_consent": False, "consenting_person": "lisa", "phone_type": "iphone"},
                     {"owner_consent": True, "consenting_person": "chris", "phone_type": "iphone"}):
            self.assertEqual(self.request("/setup/lisa/pair", body).code, 400)
        self.assertFalse(self.presence.rotations)

    def test_pair_is_once_scoped_and_does_not_enable_other_features(self):
        self.exchange()
        response = self.pair()
        self.assertEqual(response.code, 200, response.result())
        result = response.result()
        self.assertIs(result["verified"], False)
        self.assertEqual(result["endpoint"], "https://example.test/api/v1/presence/lisa")
        self.assertEqual(self.saved, [{"presence_enabled": True, "lisa_phone_type": "iphone"}])
        self.assertEqual(self.consents, [("lisa", "phone-presence")])
        self.assertFalse(self.config["shopping_enabled"])
        self.assertFalse(self.config["shopping_lisa_opt_in"])
        self.assertFalse(self.config["lisa_private_enabled"])
        self.assertEqual(self.presence.rotations, ["lisa"])
        self.assertEqual(self.pair().code, 400)
        self.assertEqual(self.presence.rotations, ["lisa"])
        config = result["configuration"]
        self.assertEqual(config["mode"], 3)
        self.assertEqual(config["days"], 0)
        self.assertFalse(config["allowinvalidcerts"])
        self.assertFalse(config["remoteConfiguration"])
        self.assertFalse(config["allowRemoteLocation"])
        self.assertNotIn("waypoints", config)
        status = self.request("/setup/lisa/status").result()
        self.assertNotIn(result["password"], json.dumps(status))
        self.assertNotIn(result["password"], self.router.path.read_text())

    def test_retry_after_uncertain_pair_failure_cannot_issue_second_credential(self):
        self.exchange()
        save = self.parent.callbacks["setup_save"]

        def fail_after_rotation(_values):
            raise OSError("synthetic storage failure")

        self.parent.callbacks["setup_save"] = fail_after_rotation
        self.assertEqual(self.pair().code, 400)
        self.assertEqual(self.presence.rotations, ["lisa"])
        self.parent.callbacks["setup_save"] = save
        retry = self.pair()
        self.assertEqual(retry.code, 400)
        self.assertIn("new link", retry.result()["error"])
        self.assertEqual(self.presence.rotations, ["lisa"])
        self.assertFalse(self.request("/setup/lisa/status").result()["verified"])

    def test_never_verified_from_manual_old_or_pre_pair_phone_state(self):
        self.exchange()
        self.pair()
        now = datetime.now(timezone.utc)
        for source, at in (("manual", now), ("owntracks-transition", now),
                           ("owntracks-location", now - timedelta(minutes=10)),
                           ("owntracks-location", now + timedelta(minutes=5))):
            self.presence.people["lisa"] = {"state": "home", "source": source, "observed_at": at.isoformat()}
            self.assertFalse(self.request("/setup/lisa/status").result()["verified"])

    def test_verification_requires_new_real_report_and_current_credential(self):
        self.exchange()
        self.pair()
        observed = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.presence.people["lisa"] = {"state": "home", "source": "owntracks-location", "observed_at": observed.isoformat()}
        status = self.request("/setup/lisa/status").result()
        self.assertTrue(status["verified"])
        self.assertEqual(status["state"], "home")
        self.assertEqual(set(status), {"person", "csrf", "paired", "verified", "state", "observed_at"})
        self.presence.token_path("lisa").write_text("synthetic-replacement")
        self.assertFalse(self.request("/setup/lisa/status").result()["verified"])

    def test_expiry_revocation_admin_rotation_and_origin_change_reject_sessions(self):
        invite, _, _ = self.exchange()
        with patch("flightwall.phone_invites.time.time", return_value=time.time() + 4000):
            self.assertEqual(self.request("/setup/lisa/status").code, 401)
        invite, _, _ = self.exchange()
        self.router.revoke({"id": invite["id"]})
        self.assertEqual(self.request("/setup/lisa/status").code, 401)
        self.exchange()
        (self.root / "control-token.txt").write_text("synthetic-new-admin")
        self.assertEqual(self.request("/setup/lisa/status").code, 401)
        self.exchange()
        self.config["public_url"] = "https://new.test"
        self.assertEqual(self.request("/setup/lisa/status", host="new.test", origin="https://new.test").code, 401)

    def test_new_invite_revokes_old_unused_link_and_active_session(self):
        _, old_token, _ = self.exchange()
        self.create()
        self.assertEqual(self.request("/setup/lisa/status").code, 401)
        self.assertEqual(self.request("/setup/lisa/exchange", {"token": old_token}).code, 400)

    def test_concurrent_invite_creation_has_one_current_link_without_lock_initialization_race(self):
        routers = [PhoneInviteRouter(self.parent, presence=self.presence) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            invitations = list(executor.map(lambda router: router.create({}), routers))
        successes = 0
        for invite in invitations:
            token = parse_qs(urlsplit(invite["url"]).fragment)["invite"][0]
            response = self.request("/setup/lisa/exchange", {"token": token})
            successes += response.code == 200
        self.assertEqual(successes, 1)

    def test_restart_keeps_one_use_consumed_without_restoring_browser_session(self):
        _, token, _ = self.exchange()
        self.router = PhoneInviteRouter(self.parent, presence=self.presence)
        self.assertEqual(self.request("/setup/lisa/status").code, 401)
        self.assertEqual(self.request("/setup/lisa/exchange", {"token": token}).code, 400)

    def test_revoking_invite_does_not_unpair_completed_phone(self):
        invite, _, _ = self.exchange()
        self.pair()
        before = self.presence.token_path("lisa").read_text()
        self.router.revoke({"id": invite["id"]})
        self.assertEqual(self.presence.token_path("lisa").read_text(), before)

    def test_browser_uses_no_persistent_storage_or_query_credentials(self):
        script = (self.parent.asset_dir / "lisa.js").read_text()
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        self.assertIn("history.replaceState", script)
        self.assertIn("60000", script)
        self.assertNotIn("?token", script)


class PhoneInviteHttpTests(unittest.TestCase):
    """Real loopback HTTP; headers model an HTTPS reverse proxy, not real TLS."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ConfigStore(self.root)
        self.config.update({"public_url": "https://example.test"})
        self.presence = FakePresence(self.root)
        self.server = RemoteServer(self.root, self.config.get, {"setup_save": self.config.update}, port=0).start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.server.stop)
        self.admin = self.server.api_key_path.read_text().strip()
        self.request("/setup")
        self.server._onboarding._invites().presence = self.presence

    def request(self, path, body=None, cookie=None, csrf=None, *, lisa=False, origin="https://example.test"):
        connection = http.client.HTTPConnection(*self.server.address, timeout=5)
        headers = {"Host": "example.test", "Origin": origin}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-Lisa-CSRF" if lisa else "X-Setup-CSRF"] = csrf
        raw = None
        if body is not None:
            raw = json.dumps(body)
            headers["Content-Type"] = "application/json"
        try:
            connection.request("GET" if body is None else "POST", path, raw, headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, dict(response.getheaders()), value
        finally:
            connection.close()

    def login(self):
        code, headers, _ = self.request("/setup/login", {"token": self.admin})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        code, _, result = self.request("/setup/status", cookie=cookie)
        self.assertEqual(code, 200)
        return cookie, result["csrf"]

    def test_owner_creation_lisa_pairing_scope_and_revoke_over_http(self):
        self.assertEqual(self.request("/setup/phone/invite", {})[0], 401)
        admin_cookie, admin_csrf = self.login()
        self.assertEqual(self.request("/setup/phone/invite", {}, admin_cookie, "wrong")[0], 403)
        code, _, invite = self.request("/setup/phone/invite", {"person": "lisa", "phone_type": "iphone"},
                                       admin_cookie, admin_csrf)
        self.assertEqual(code, 200, invite)
        token = parse_qs(urlsplit(invite["url"]).fragment)["invite"][0]
        code, headers, exchange = self.request("/setup/lisa/exchange", {"token": token})
        self.assertEqual(code, 200, exchange)
        lisa_cookie, lisa_csrf = headers["Set-Cookie"].split(";", 1)[0], exchange["csrf"]
        self.assertIn("Path=/setup/lisa; HttpOnly; Secure; SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(self.request("/setup/status", cookie=lisa_cookie)[0], 401)
        self.assertEqual(self.request("/api/v1/display", cookie=lisa_cookie)[0], 401)
        self.assertEqual(self.request("/api/v1/status", cookie=lisa_cookie)[0], 401)
        self.assertEqual(self.request("/setup/phone/invite", {}, lisa_cookie, lisa_csrf, lisa=True)[0], 401)
        self.assertEqual(self.request("/setup/lisa/status", cookie=admin_cookie)[0], 401)
        consent = {"owner_consent": True, "consenting_person": "lisa", "phone_type": "iphone"}
        self.assertEqual(self.request("/setup/lisa/pair", consent, lisa_cookie, "wrong", lisa=True)[0], 403)
        code, _, paired = self.request("/setup/lisa/pair", consent, lisa_cookie, lisa_csrf, lisa=True)
        self.assertEqual(code, 200, paired)
        self.assertFalse(paired["verified"])
        self.assertEqual(self.presence.rotations, ["lisa"])
        self.assertFalse(self.config.get()["shopping_lisa_opt_in"])
        self.assertFalse(self.config.get()["shopping_enabled"])
        self.assertFalse(self.config.get()["lisa_private_enabled"])
        self.assertEqual(self.request("/setup/lisa/pair", consent, lisa_cookie, lisa_csrf, lisa=True)[0], 400)
        self.assertEqual(self.request("/setup/phone/invite/revoke", {"id": invite["id"]}, admin_cookie, admin_csrf)[0], 200)
        self.assertEqual(self.request("/setup/lisa/status", cookie=lisa_cookie)[0], 401)

    def test_real_transport_preserves_query_for_invitation_rejection(self):
        # A raw query credential is invalid even though the static path exists.
        # This catches setup dispatch accidentally dropping the query string.
        code, _, result = self.request("/setup/lisa?invite=synthetic-never-a-real-secret")
        self.assertEqual(code, 400, result)
        self.assertIn("query string", result["error"])


if __name__ == "__main__":
    unittest.main()
