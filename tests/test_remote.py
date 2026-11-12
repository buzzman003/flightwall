"""Exercise the real HTTP transport and OAuth grant flow, without network accounts."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from flightwall import __version__
from flightwall.remote import RemoteServer


class RemoteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        self.config = {"public_url": "https://wall.example.com", "control_port": 0}
        self.callbacks = {
            "get_status": lambda: {"version": "0.1.0", "api_key": "should disappear", "nested": {"refresh_token": "never returned", "ok": True}},
            "update_settings": lambda settings: self._record("settings", settings),
            "refresh_calendar": lambda: self._record("refresh", {}),
            "check_updates": lambda: self._record("updates", {}),
        }
        self.server = RemoteServer(self.temp.name, lambda: self.config, self.callbacks).start()
        self.key = self.server.api_key_path.read_text().strip()

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def _record(self, name, value):
        self.calls.append((name, value))
        return {"ok": True, "changed": value}

    def request(self, path, method="GET", body=None, token=None, headers=None, form=False):
        hdr = dict(headers or {})
        if token:
            hdr["Authorization"] = "Bearer " + token
        if body is not None:
            body = urlencode(body) if form else json.dumps(body)
            hdr.setdefault("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
        connection = http.client.HTTPConnection(*self.server.address, timeout=5)
        try:
            connection.request(method, path, body, hdr)
            response = connection.getresponse()
            raw = response.read().decode()
            value = json.loads(raw) if raw and response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, dict(response.getheaders()), value
        finally:
            connection.close()

    def rpc(self, method, params=None, token=None):
        return self.request("/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token or self.key)

    def register(self, redirect="https://client.example/callback"):
        status, _, body = self.request("/oauth/register", "POST", {"client_name": "My MCP client", "redirect_uris": [redirect]})
        self.assertEqual(status, 201, body)
        return body["client_id"]

    def begin(self, client=None, verifier=None, **changes):
        client = client or self.register()
        verifier = verifier or "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": client, "redirect_uri": "https://client.example/callback",
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": "test-state",
                  "resource": self.server.resource, "scope": "flightwall.control offline_access"}
        params.update(changes)
        status, headers, page = self.request("/oauth/authorize?" + urlencode(params))
        self.assertEqual(status, 200, page)
        self.assertIn("Secure; HttpOnly; SameSite=Lax", headers["Set-Cookie"])
        pending = re.search('name="pending" value="([^"]+)"', page).group(1)
        csrf = re.search('name="csrf" value="([^"]+)"', page).group(1)
        return client, verifier, {"pending": pending, "csrf": csrf, "admin_secret": self.key, "approve": "yes"}

    def authorization_code(self):
        client, verifier, form = self.begin()
        status, headers, _ = self.request("/oauth/authorize", "POST", form, headers={"Cookie": "fw_approval=" + form["csrf"]}, form=True)
        self.assertEqual(status, 302)
        params = parse_qs(urlsplit(headers["Location"]).query)
        self.assertEqual(params["state"], ["test-state"])
        self.assertEqual(params["iss"], ["https://wall.example.com"])
        return client, verifier, params["code"][0]

    def token_form(self, client, verifier, code):
        return {"grant_type": "authorization_code", "client_id": client, "code": code,
                "code_verifier": verifier, "redirect_uri": "https://client.example/callback", "resource": self.server.resource}

    def grant(self):
        status, _, tokens = self.request("/oauth/token", "POST", self.token_form(*self.authorization_code()), form=True)
        self.assertEqual(status, 200, tokens)
        return tokens

    def test_unauthorized_and_health(self):
        status, headers, body = self.request("/mcp", "POST", {})
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata="https://wall.example.com/.well-known/oauth-protected-resource/mcp"', headers["WWW-Authenticate"])
        self.assertEqual(self.request("/api/v1/status")[0], 401)
        self.assertEqual(self.request("/health")[2], {"ok": True, "service": "flightwall", "version": __version__})
        self.assertNotIn(self.key, json.dumps(body))

    def test_rejected_post_closes_before_a_pooled_client_sends_its_next_request(self):
        phone = self.server.presence.rotate("chris")
        self.config["presence_enabled"] = True
        public_url = self.server.public_url
        cases = (("unknown route", "/unknown", "application/json", 404),
                 ("disabled OAuth", "/oauth/token", "application/json", 503),
                 ("unavailable device", "/api/v1/presence/chris", "application/json", 503),
                 ("invalid device body", "/api/v1/presence/chris", "text/plain", 400))
        for name, path, content_type, expected_status in cases:
            with self.subTest(name=name):
                self.server.public_url = "" if name == "disabled OAuth" else public_url
                if name == "invalid device body":
                    self.callbacks["update_presence"] = lambda _person, _payload: {"updated": True}
                connection = http.client.HTTPConnection(*self.server.address, timeout=5)
                try:
                    connection.request("POST", path, '{"state":"home"}',
                                       {"Authorization": "Bearer " + phone, "Content-Type": content_type})
                    rejected = connection.getresponse()
                    self.assertEqual(rejected.status, expected_status)
                    self.assertEqual(rejected.getheader("Connection"), "close")
                    rejected.read()
                    connection.request("GET", "/health")
                    healthy = connection.getresponse()
                    self.assertEqual(healthy.status, 200)
                    self.assertTrue(json.loads(healthy.read())["ok"])
                finally:
                    connection.close()

    def test_unrelated_voice_validation_errors_remain_generic(self):
        self.config["alexa_enabled"] = True
        voice_token = self.server.alexa_token_path.read_text().strip()

        def reject(_payload):
            raise ValueError("Private callback details")

        self.callbacks["start_timer"] = reject
        status, _, body = self.request("/api/v1/timers", "POST", {"label": "Tea"}, voice_token)
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Personal-item data rejected; check the title, dates and identifier"})

    def test_initialize_tools_notifications_and_no_shell(self):
        status, headers, body = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")
        self.assertNotIn("Mcp-Session-Id", headers)
        tools = self.rpc("tools/list")[2]["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"get_status", "update_settings", "refresh_calendar", "check_updates"})
        result = self.rpc("tools/call", {"name": "update_settings", "arguments": {"settings": {"brightness": 0.3}}})[2]["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(self.calls, [("settings", {"brightness": 0.3})])
        result = self.rpc("tools/call", {"name": "shell", "arguments": {"command": "touch /tmp/unwanted"}})[2]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.request("/mcp", "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.key)[0], 202)
        self.assertEqual(self.request("/mcp", "POST", {"jsonrpc": "2.0", "id": 3, "result": {}}, self.key)[0], 202)
        self.assertEqual(self.request("/mcp", token=self.key)[0], 405)

    def test_api_redaction_and_local_security_settings(self):
        status, _, body = self.request("/api/v1/status", token=self.key)
        self.assertEqual(status, 200)
        self.assertNotIn("api_key", body)
        self.assertEqual(body["nested"], {"ok": True})
        self.assertEqual(self.request("/api/v1/settings", "POST", {"public_url": "https://attacker.example"}, self.key)[0], 400)
        self.assertEqual(self.request("/api/v1/settings", "POST", {"update_public_key": "attacker-key"}, self.key)[0], 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request("/api/v1/calendar/refresh", "POST", {}, self.key)[0], 200)
        self.assertEqual(self.request("/api/v1/updates/check", "POST", {}, self.key)[0], 200)

    def test_guest_mode_and_flight_focus_can_be_changed_through_settings(self):
        patch = {"guest_mode": True, "flight_focus_seconds": 30,
                 "flight_window_before_minutes": 45, "flight_window_after_minutes": 90}
        self.assertEqual(self.request("/api/v1/settings", "POST", patch, self.key)[0], 200)
        self.assertEqual(self.calls, [("settings", patch)])

    def test_origin_and_limits(self):
        self.assertEqual(self.request("/mcp", "POST", {}, self.key, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/oauth/register", "POST", {}, headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.request("/api/v1/settings", "POST", {"calendar_name": "x" * 70000}, self.key)[0], 400)
        self.assertEqual(self.request("/api/v1/settings", "POST", {"brightness": True}, self.key)[0], 400)
        self.assertEqual(self.request("/api/v1/status", token=self.key, headers={"Origin": "https://wall.example.com"})[0], 200)

    def test_metadata_uses_configured_url_not_host(self):
        status, _, body = self.request("/.well-known/oauth-authorization-server", headers={"Host": "attacker.example"})
        self.assertEqual(status, 200)
        self.assertEqual(body["issuer"], "https://wall.example.com")
        self.assertEqual(body["code_challenge_methods_supported"], ["S256"])
        status, _, body = self.request("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(body["resource"], "https://wall.example.com/mcp")

    def test_uppercase_setup_link_uses_same_generic_owner_login_page(self):
        status, headers, body = self.request("/SETUP")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertNotIn(self.key, body)

    def test_registration_redirect_validation_and_bound(self):
        for uri in ("http://evil.example/callback", "https://user:secret@example.com/cb", "https://example.com/cb#fragment", "javascript:alert(1)"):
            self.assertEqual(self.request("/oauth/register", "POST", {"redirect_uris": [uri]})[0], 400)
        self.register("http://127.0.0.1:12345/callback")
        with self.server._lock:
            for index in range(64):
                self.server._state["clients"]["test-" + str(index)] = {"created": 99999999999, "approved": False}
        self.assertEqual(self.request("/oauth/register", "POST", {"redirect_uris": ["https://example.com/cb"]})[0], 429)

    def test_authorization_requires_pkce_state_resource_and_exact_redirect(self):
        client = self.register()
        base = {"response_type": "code", "client_id": client, "redirect_uri": "https://client.example/callback",
                "code_challenge": "A" * 43, "code_challenge_method": "S256", "state": "test", "resource": self.server.resource}
        for changes in ({"code_challenge_method": "plain"}, {"state": ""}, {"resource": "https://other.example/mcp"}, {"redirect_uri": "https://client.example/callback/extra"}, {"scope": "all-powerful"}):
            params = dict(base, **changes)
            self.assertEqual(self.request("/oauth/authorize?" + urlencode(params))[0], 400)

    def test_owner_approval_and_csrf_required(self):
        _, _, form = self.begin()
        self.assertEqual(self.request("/oauth/authorize", "POST", form, form=True)[0], 403)
        wrong = dict(form, admin_secret="wrong")
        headers = {"Cookie": "fw_approval=" + form["csrf"]}
        self.assertEqual(self.request("/oauth/authorize", "POST", wrong, headers=headers, form=True)[0], 403)
        self.assertEqual(self.request("/oauth/authorize", "POST", form, headers=headers, form=True)[0], 302)
        self.assertEqual(self.request("/oauth/authorize", "POST", form, headers=headers, form=True)[0], 403)

    def test_pkce_exchange_resource_binding_single_use_and_expiry(self):
        args = self.authorization_code()
        form = self.token_form(*args)
        self.assertEqual(self.request("/oauth/token", "POST", dict(form, resource="https://other.example/mcp"), form=True)[0], 400)
        self.assertEqual(self.request("/oauth/token", "POST", dict(form, code_verifier="w" * 64), form=True)[0], 400)
        status, _, tokens = self.request("/oauth/token", "POST", form, form=True)
        self.assertEqual(status, 200, tokens)
        self.assertEqual(tokens["expires_in"], 3600)
        self.assertEqual(self.request("/oauth/token", "POST", form, form=True)[0], 400)
        self.assertEqual(self.request("/api/v1/status", token=tokens["access_token"])[0], 200)
        with self.server._lock:
            self.server._state["access"][hashlib.sha256(tokens["access_token"].encode()).hexdigest()]["expires"] = 1
        self.assertEqual(self.request("/api/v1/status", token=tokens["access_token"])[0], 401)

    def test_refresh_rotates_and_persists_only_hashes(self):
        client, verifier, code = self.authorization_code()
        tokens = self.request("/oauth/token", "POST", self.token_form(client, verifier, code), form=True)[2]
        form = {"grant_type": "refresh_token", "client_id": client, "refresh_token": tokens["refresh_token"], "resource": self.server.resource}
        status, _, rotated = self.request("/oauth/token", "POST", form, form=True)
        self.assertEqual(status, 200, rotated)
        self.assertNotEqual(tokens["refresh_token"], rotated["refresh_token"])
        self.assertEqual(self.request("/oauth/token", "POST", form, form=True)[0], 400)
        saved = (Path(self.temp.name) / "remote-oauth.json").read_text()
        for secret in (self.key, tokens["access_token"], tokens["refresh_token"], rotated["refresh_token"]):
            self.assertNotIn(secret, saved)
        for path in (self.server.api_key_path, Path(self.temp.name) / "remote-oauth.json"):
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.server.stop()
        self.server = RemoteServer(self.temp.name, lambda: self.config, self.callbacks).start()
        self.assertEqual(self.request("/api/v1/status", token=rotated["access_token"])[0], 200)
        self.server.revoke_oauth()
        self.assertEqual(self.request("/api/v1/status", token=rotated["access_token"])[0], 401)

    def test_tokens_cannot_cross_changed_public_audience(self):
        tokens = self.grant()
        self.server.stop()
        self.config["public_url"] = "https://new-wall.example.com"
        self.server = RemoteServer(self.temp.name, lambda: self.config, self.callbacks).start()
        self.assertEqual(self.request("/api/v1/status", token=tokens["access_token"])[0], 401)

    def test_invalid_configuration_refuses_public_or_plain_http(self):
        for public_url in ("http://wall.example.com", "https://wall.example.com/path", "https://name:password@wall.example.com"):
            with self.assertRaises(ValueError):
                RemoteServer(self.temp.name, lambda: {"public_url": public_url}, {})
        with self.assertRaises(ValueError):
            RemoteServer(self.temp.name, lambda: {}, {}, host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
