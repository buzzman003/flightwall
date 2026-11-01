"""The Alexa credential may manage personal cards, without reading flight data."""
import http.client
import json
import os
from datetime import datetime, timedelta, timezone
import socket
import tempfile
import unittest

from flightwall.remote import RemoteServer
from flightwall.personal import PersonalError


class PersonalRemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"control_port": 0, "alexa_enabled": True}
        self.calls = []
        self.callbacks = {
            "get_status": lambda: {"flights": ["private roster"]},
            "update_settings": lambda value: self._record("settings", value),
            "refresh_calendar": lambda: self._record("refresh", None),
            "check_updates": lambda: self._record("updates", None),
            "create_personal_item": lambda value: self._record("create", value),
            "cancel_personal_item": lambda value: self._record("cancel", value),
            "get_voice_context": lambda: {"timezone": "America/New_York", "appointment_duration_minutes": 60,
                "dinner_start": "18:00", "dinner_end": "20:00", "flights": ["private roster"],
                "config": {"calendar_id": "private calendar"}, "token": "private credential"},
        }
        self.server = RemoteServer(self.temp.name, lambda: self.config, self.callbacks).start()
        self.admin = self.server.api_key_path.read_text().strip()
        self.alexa = self.server.alexa_token_path.read_text().strip()

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def _record(self, operation, payload):
        self.calls.append((operation, payload))
        return {"saved": True, "operation": operation, "item": {"id": "personal-123"}}

    def request(self, path, method="GET", body=None, token=None, headers=None):
        hdr = dict(headers or {})
        if token:
            hdr["Authorization"] = "Bearer " + token
        if body is not None:
            body = json.dumps(body)
            hdr.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(*self.server.address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=hdr)
            response = connection.getresponse()
            value = response.read()
            return response.status, json.loads(value) if value else None
        finally:
            connection.close()

    def test_create_cancel_preserve_alexa_payload_and_request_id(self):
        payload = {"kind": "appointment", "title": "Dentist", "date": "2026-11-05", "time": "14:30",
                   "duration_minutes": 45, "source": "alexa", "request_id": "request-" + "x" * 504}
        status, result = self.request("/api/v1/personal-items", "POST", payload, self.alexa)
        self.assertEqual(status, 200)
        self.assertTrue(result["saved"])
        self.assertEqual(self.calls, [("create", payload)])
        # The store, rather than the HTTP layer, owns idempotent replay handling.
        self.request("/api/v1/personal-items", "POST", payload, self.alexa)
        self.assertEqual(self.calls[-1], ("create", payload))
        cancellation = {"id": result["item"]["id"], "request_id": "cancel-request"}
        self.assertEqual(self.request("/api/v1/personal-items/cancel", "POST", cancellation, self.alexa)[0], 200)
        self.assertEqual(self.calls[-1], ("cancel", cancellation))

    def test_voice_context_exposes_only_timezone_and_personal_defaults(self):
        status, result = self.request("/api/v1/voice-context", token=self.alexa)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"timezone": "America/New_York", "appointment_duration_minutes": 60,
                                 "dinner_start": "18:00", "dinner_end": "20:00"})
        self.assertEqual(self.calls, [])

    def test_alexa_token_cannot_read_roster_or_use_admin_tools(self):
        paths = [("/api/v1/status", "GET", None), ("/mcp", "GET", None),
                 ("/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                 ("/api/v1/settings", "POST", {"brightness": 0.5}),
                 ("/api/v1/calendar/refresh", "POST", {}), ("/api/v1/updates/check", "POST", {})]
        for path, method, body in paths:
            with self.subTest(path=path, method=method):
                status, result = self.request(path, method, body, self.alexa)
                self.assertEqual(status, 401)
                self.assertNotIn("private roster", json.dumps(result))
        self.assertEqual(self.calls, [])

    def test_admin_control_token_can_use_personal_routes_without_enabling_alexa(self):
        self.config["alexa_enabled"] = False
        self.assertEqual(self.request("/api/v1/voice-context", token=self.alexa)[0], 503)
        self.assertEqual(self.request("/api/v1/personal-items", "POST", {"kind": "dinner", "title": "Pizza"}, self.alexa)[0], 503)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request("/api/v1/voice-context", token=self.admin)[0], 200)
        self.assertEqual(self.request("/api/v1/personal-items", "POST", {"kind": "dinner", "title": "Pizza"}, self.admin)[0], 200)
        self.assertEqual(self.calls[-1][0], "create")

    def test_default_disabled_and_missing_or_wrong_tokens_rejected(self):
        self.config.pop("alexa_enabled")
        self.assertEqual(self.request("/api/v1/voice-context", token=self.alexa)[0], 503)
        for token in (None, "unrecognized-token", "bearer-token" * 300):
            self.assertEqual(self.request("/api/v1/voice-context", token=token)[0], 401)
            self.assertEqual(self.request("/api/v1/personal-items", "POST", {"kind": "dinner"}, token)[0], 401)
        self.assertEqual(self.calls, [])

    def test_personal_token_persists_privately_and_rotates_independently(self):
        self.assertNotEqual(self.admin, self.alexa)
        if os.name == "posix":
            self.assertEqual(os.stat(self.server.alexa_token_path).st_mode & 0o777, 0o600)
        self.server.stop()
        self.server = RemoteServer(self.temp.name, lambda: self.config, self.callbacks).start()
        self.assertEqual(self.server.alexa_token_path.read_text().strip(), self.alexa)
        self.server.stop()
        self.server.alexa_token_path.unlink()
        self.server = RemoteServer(self.temp.name, lambda: self.config, self.callbacks).start()
        self.assertNotEqual(self.server.alexa_token_path.read_text().strip(), self.alexa)
        self.assertEqual(self.server.api_key_path.read_text().strip(), self.admin)
        self.assertEqual(self.request("/api/v1/voice-context", token=self.alexa)[0], 401)
        self.assertEqual(self.request("/api/v1/status", token=self.admin)[0], 200)

    def test_bad_payload_origin_and_credential_return_are_blocked(self):
        for payload in ([], {}, {"kind": "dinner", "title": "x" * 161},
                        {"kind": "dinner", "request_id": "x" * 513},
                        {"kind": "appointment", "duration_minutes": True},
                        {"kind": "dinner", "command": "read the flight roster"}):
            self.assertEqual(self.request("/api/v1/personal-items", "POST", payload, self.alexa)[0], 400)
        self.assertEqual(self.request("/api/v1/personal-items", "POST", {"kind": "dinner"}, self.alexa,
                                      headers={"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.calls, [])
        self.callbacks["create_personal_item"] = lambda _: {"saved": True, "api_key": self.admin,
                                                          "message": self.alexa + " " + self.admin}
        result = self.request("/api/v1/personal-items", "POST", {"kind": "dinner"}, self.alexa)[1]
        self.assertNotIn(self.alexa, json.dumps(result))
        self.assertNotIn(self.admin, json.dumps(result))
        self.assertNotIn("api_key", result)

    def test_callback_validation_is_400_and_unavailable_feature_is_503(self):
        def reject(_payload):
            raise ValueError("sensitive validation detail " + self.alexa)
        self.callbacks["create_personal_item"] = reject
        status, result = self.request("/api/v1/personal-items", "POST", {"kind": "dinner"}, self.alexa)
        self.assertEqual(status, 400)
        self.assertNotIn(self.alexa, json.dumps(result))
        del self.callbacks["get_voice_context"]
        self.assertEqual(self.request("/api/v1/voice-context", token=self.alexa)[0], 503)

    def test_explicit_personal_validation_message_is_safe_and_actionable(self):
        def reject(_payload):
            raise PersonalError("That local time does not exist because the clocks change. " + self.alexa)
        self.callbacks["create_personal_item"] = reject
        status, result = self.request("/api/v1/personal-items", "POST", {"kind": "dinner"}, self.alexa)
        self.assertEqual(status, 400)
        self.assertIn("does not exist because the clocks change", result["error"])
        self.assertNotIn(self.alexa, result["error"])

    def test_mcp_remains_limited_to_existing_four_tools(self):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        status, result = self.request("/mcp", "POST", rpc, self.admin)
        self.assertEqual(status, 200)
        self.assertEqual({tool["name"] for tool in result["result"]["tools"]},
                         {"get_status", "update_settings", "refresh_calendar", "check_updates"})
        rpc.update(method="tools/call", params={"name": "create_personal_item", "arguments": {"kind": "dinner"}})
        self.assertTrue(self.request("/mcp", "POST", rpc, self.admin)[1]["result"]["isError"])
        self.assertEqual(self.calls, [])


class RuntimePersonalBridgeTests(unittest.TestCase):
    request = PersonalRemoteTests.request

    def setUp(self):
        from flightwall.backend import Runtime
        from flightwall.config import ConfigStore
        self.temp = tempfile.TemporaryDirectory()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        store = ConfigStore(self.temp.name)
        store.update({"control_port": port, "timezone": "UTC", "alexa_enabled": True})
        self.runtime = Runtime(self.temp.name, self.temp.name + "/install").start()
        self.server = self.runtime.remote
        self.alexa = self.server.alexa_token_path.read_text().strip()
        self.admin = self.server.api_key_path.read_text().strip()

    def tearDown(self):
        self.runtime.stop()
        self.temp.cleanup()

    def test_real_runtime_context_create_replay_cancel_and_roster_privacy(self):
        status, context = self.request("/api/v1/voice-context", token=self.alexa)
        self.assertEqual(status, 200)
        self.assertEqual(set(context), {"timezone", "appointment_duration_minutes", "dinner_start", "dinner_end",
                                        "dinner_tomorrow_preview", "dinner_requires_kids_home"})
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        payload = {"kind": "appointment", "title": "Dentist", "date": tomorrow, "time": "14:30",
                   "source": "alexa", "request_id": "real-runtime-request"}
        status, created = self.request("/api/v1/personal-items", "POST", payload, self.alexa)
        self.assertEqual(status, 200)
        self.assertTrue(created["created"])
        self.assertEqual(created["item"]["source"], "alexa")
        self.assertEqual(created["item"]["title"], "Dentist")
        repeated = self.request("/api/v1/personal-items", "POST", payload, self.alexa)[1]
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["item"]["id"], created["item"]["id"])
        self.assertEqual(self.request("/api/v1/status", token=self.alexa)[0], 401)
        self.assertIn("snapshot", self.request("/api/v1/status", token=self.admin)[1])
        self.assertEqual(self.request("/api/v1/personal-items", "POST", dict(payload, date="2026-02-30", request_id="invalid-date"), self.alexa)[0], 400)
        status, result = self.request("/api/v1/personal-items/cancel", "POST", {"id": created["item"]["id"]}, self.alexa)
        self.assertEqual(status, 200)
        self.assertTrue(result["cancelled"])


if __name__ == "__main__":
    unittest.main()
