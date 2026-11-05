"""Presence freshness, privacy, replay checks and real HTTP device isolation."""
from datetime import datetime, timedelta, timezone
import base64
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from flightwall.presence import PresenceStore, PresenceError, PresenceDisabled
from flightwall.remote import RemoteServer


class PresenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)
        self.config = {"presence_enabled": True, "presence_ttl_minutes": 90,
                       "home_latitude": 33.6407, "home_longitude": -84.4277, "home_radius_m": 200}
        self.store = PresenceStore(self.temp.name, lambda: self.config)

    def tearDown(self):
        self.temp.cleanup()

    def report(self, state="home", seconds=0, request_id=None):
        result = {"state": state, "observed_at": (self.now + timedelta(seconds=seconds)).isoformat()}
        if request_id is not None:
            result["request_id"] = request_id
        return result

    def test_pairing_is_explicit_private_and_person_specific(self):
        self.assertFalse(self.store.token_path("chris").exists())
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "unknown")
        self.assertFalse(self.store.token_path("chris").exists())
        first, lisa = self.store.rotate("chris"), self.store.rotate("lisa")
        self.assertNotEqual(first, lisa)
        self.assertTrue(self.store.authenticate("chris", first))
        self.assertFalse(self.store.authenticate("lisa", first))
        self.assertFalse(self.store.authenticate("chris", "wrong"))
        if os.name == "posix":
            self.assertEqual(os.stat(self.store.token_path("chris")).st_mode & 0o777, 0o600)
        self.store.update("chris", self.report(), self.now)
        self.store.update("lisa", self.report("away"), self.now)
        replacement = self.store.rotate("chris")
        self.assertFalse(self.store.authenticate("chris", first))
        self.assertTrue(self.store.authenticate("chris", replacement))
        state = self.store.snapshot(self.now)
        self.assertEqual(state["chris"]["state"], "unknown")
        self.assertEqual(state["lisa"]["state"], "away")
        for person in ("../chris", "unknown", "Chris", ""):
            with self.assertRaises(PresenceError):
                self.store.token_path(person)

    def test_state_expires_to_unknown_without_new_heartbeat(self):
        self.store.update("chris", self.report(), self.now)
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "home")
        self.assertEqual(self.store.snapshot(self.now + timedelta(minutes=89))["chris"]["state"], "home")
        expired = self.store.snapshot(self.now + timedelta(minutes=90))["chris"]
        self.assertEqual(expired["state"], "unknown")
        self.assertTrue(expired["stale"])
        self.assertIsNotNone(expired["last_seen"])
        fresh = self.now + timedelta(minutes=91)
        self.store.update("chris", {"state": "away", "observed_at": fresh.isoformat()}, fresh)
        self.assertEqual(self.store.snapshot(fresh)["chris"]["state"], "away")
        self.config["presence_enabled"] = False
        self.assertEqual(self.store.snapshot(fresh)["chris"], {"state": "unknown", "last_seen": None, "stale": True})
        with self.assertRaises(PresenceDisabled):
            self.store.update("chris", self.report(), self.now)

    def test_failed_token_replacement_does_not_leave_fresh_presence(self):
        self.store.rotate("chris")
        self.store.update("chris", self.report(), self.now)
        replace = os.replace
        def fail_token(source, destination):
            if destination == self.store.token_path("chris"):
                raise OSError("Simulated token replacement failure")
            return replace(source, destination)
        with patch("flightwall.presence.os.replace", side_effect=fail_token), self.assertRaises(PresenceError):
            self.store.rotate("chris")
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "unknown")

    def test_timestamps_reject_stale_future_naive_and_conflicting_reports(self):
        for value in (self.report(seconds=-5400), self.report(seconds=121),
                      {"state": "home", "observed_at": "2026-11-01T14:00:00"},
                      {"state": "home", "observed_at": "2026-02-30T14:00:00Z"},
                      {"state": "maybe", "observed_at": self.now.isoformat()}):
            with self.assertRaises(PresenceError):
                self.store.update("chris", value, self.now)
        self.store.update("chris", self.report(seconds=2), self.now)
        with self.assertRaises(PresenceError):
            self.store.update("chris", self.report("away", seconds=1), self.now)
        with self.assertRaises(PresenceError):
            self.store.update("chris", self.report("away", seconds=2), self.now)
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "home")

    def test_retry_ledger_survives_restart_without_overwriting_newer_state(self):
        report = self.report(request_id="phone-secret-request-id")
        self.assertTrue(self.store.update("chris", report, self.now)["updated"])
        self.store = PresenceStore(self.temp.name, lambda: self.config)
        self.assertTrue(self.store.update("chris", report, self.now)["duplicate"])
        self.store.update("chris", self.report("away", seconds=1), self.now)
        self.assertTrue(self.store.update("chris", report, self.now)["duplicate"])
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "away")
        with self.assertRaises(PresenceError):
            self.store.update("chris", self.report("home", seconds=2, request_id="phone-secret-request-id"), self.now)
        self.assertNotIn("phone-secret-request-id", self.store.path.read_text())

    def test_owntracks_home_transition_filters_other_geofences(self):
        entry = {"_type": "transition", "event": "enter", "desc": "Home", "tst": self.now.timestamp()}
        self.store.update("lisa", entry, self.now)
        self.assertEqual(self.store.snapshot(self.now)["lisa"]["state"], "home")
        self.store.update("lisa", dict(entry, event="leave", tst=self.now.timestamp() + 1), self.now)
        self.assertEqual(self.store.snapshot(self.now)["lisa"]["state"], "away")
        ignored = self.store.update("lisa", dict(entry, desc="Airport", tst=self.now.timestamp() + 2), self.now)
        self.assertTrue(ignored["ignored"])
        self.assertFalse(ignored["updated"])
        self.assertEqual(self.store.snapshot(self.now)["lisa"]["state"], "away")

    def test_owntracks_housekeeping_does_not_create_or_refresh_presence(self):
        self.assertFalse(self.store.path.exists())
        for report in ({"_type": "status", "android": {"loc": 1}},
                       {"_type": "waypoint", "desc": "Private region", "tst": self.now.timestamp(), "lat": 33.64, "lon": -84.42},
                       {"_type": "waypoints", "waypoints": []},
                       {"_type": "lwt", "tst": self.now.timestamp() - 20000}):
            with self.subTest(kind=report["_type"]):
                result = self.store.update("chris", report, self.now)
                self.assertTrue(result["ignored"])
                self.assertFalse(result["updated"])
                self.assertFalse(self.store.path.exists())
        self.store.update("chris", self.report(), self.now)
        before = self.store.path.read_bytes()
        later = self.now + timedelta(minutes=90)
        self.store.update("chris", {"_type": "status", "android": {"loc": 1}}, later)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot(later)["chris"]["state"], "unknown")

    def test_owntracks_expired_and_older_are_discarded_but_same_clock_conflict_is_rejected(self):
        report = {"_type": "location", "lat": 33.6407, "lon": -84.4277, "acc": 10, "tst": self.now.timestamp()}
        self.store.update("chris", report, self.now)
        before = self.store.path.read_bytes()
        for payload, now in ((dict(report, tst=self.now.timestamp() - 1), self.now),
                             (dict(report, tst=self.now.timestamp() - 5400), self.now),
                             (report, self.now + timedelta(minutes=90))):
            self.assertTrue(self.store.update("chris", payload, now)["ignored"])
            self.assertEqual(self.store.path.read_bytes(), before)
        for now in (self.now, self.now + timedelta(minutes=90)):
            with self.assertRaises(PresenceError):
                self.store.update("chris", dict(report, lat=34), now)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_expired_owntracks_does_not_create_a_saved_observation(self):
        payload = {"_type": "location", "lat": 33.6407, "lon": -84.4277, "tst": self.now.timestamp() - 5400}
        self.assertTrue(self.store.update("chris", payload, self.now)["ignored"])
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "unknown")

    def test_owntracks_location_keeps_only_state_and_handles_accuracy_overlap(self):
        report = {"_type": "location", "lat": 33.6407, "lon": -84.4277, "acc": 10,
                  "tst": self.now.timestamp(), "tid": "phone", "batt": 87}
        self.store.update("chris", report, self.now)
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "home")
        self.store.update("chris", dict(report, acc=500, tst=self.now.timestamp() + 1), self.now)
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "unknown")
        self.store.update("chris", dict(report, lat=33.65, tst=self.now.timestamp() + 2), self.now)
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "away")
        saved = self.store.path.read_text()
        for private in ("33.6407", "-84.4277", '"lat"', '"lon"', '"acc"', '"tid"', '"batt"'):
            self.assertNotIn(private, saved)
        state = json.loads(saved)
        self.assertEqual(set(state["people"]["chris"]), {"state", "observed_at", "source"})
        if os.name == "posix":
            self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.assertEqual(set(self.store.snapshot(self.now)["chris"]), {"state", "last_seen", "stale"})

    def test_invalid_geofence_and_corrupt_store_fail_closed(self):
        payload = {"_type": "location", "lat": 33.6, "lon": -84.4, "tst": self.now.timestamp()}
        self.config["home_latitude"] = None
        with self.assertRaises(PresenceError):
            self.store.update("chris", payload, self.now)
        self.config["home_latitude"] = 33.6407
        for bad in (dict(payload, lat=True), dict(payload, lon=181), dict(payload, acc=-1)):
            with self.assertRaises(PresenceError):
                self.store.update("chris", bad, self.now)
        self.store.update("chris", self.report(), self.now)
        self.store.path.write_text('{"invalid":"store"}')
        self.assertEqual(self.store.snapshot(self.now)["chris"]["state"], "unknown")
        with self.assertRaises(PresenceError):
            self.store.update("lisa", self.report(), self.now)
        self.assertEqual(self.store.path.read_text(), '{"invalid":"store"}')

    def test_independent_store_instances_keep_both_people_under_concurrent_updates(self):
        errors = []
        def update_many(person, state):
            other = PresenceStore(self.temp.name, lambda: self.config)
            try:
                for index in range(20):
                    other.update(person, self.report(state, index), self.now)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=update_many, args=("chris", "home")),
                   threading.Thread(target=update_many, args=("lisa", "away"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        result = self.store.snapshot(self.now + timedelta(seconds=20))
        self.assertEqual(result["chris"]["state"], "home")
        self.assertEqual(result["lisa"]["state"], "away")


class PresenceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"control_port": 0, "presence_enabled": True,
                       "home_latitude": 33.6407, "home_longitude": -84.4277}
        self.store = PresenceStore(self.temp.name, lambda: self.config)
        self.chris, self.lisa = self.store.rotate("chris"), self.store.rotate("lisa")
        self.callbacks = {"update_presence": lambda person, payload: self.store.update(person, payload),
                          "get_status": lambda: {"flights": ["private roster"]}}
        self.server = RemoteServer(self.temp.name, lambda: self.config, self.callbacks).start()

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def request(self, path, token, payload=None, method="POST", headers=None):
        connection = http.client.HTTPConnection(*self.server.address, timeout=5)
        try:
            hdr = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            hdr.update(headers or {})
            body = None if payload is None else json.dumps(payload)
            connection.request(method, path, body=body, headers=hdr)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            connection.close()

    def report(self):
        return {"state": "home", "observed_at": datetime.now(timezone.utc).isoformat()}

    def test_phone_only_updates_its_own_presence_and_receives_no_roster(self):
        status, result = self.request("/api/v1/presence/chris", self.chris, self.report())
        self.assertEqual(status, 200)
        self.assertEqual(result, {"accepted": True, "updated": True, "duplicate": False})
        self.assertEqual(self.request("/api/v1/presence/lisa", self.chris, self.report())[0], 401)
        for path, method, payload in (("/api/v1/status", "GET", None), ("/mcp", "POST", {}),
                                      ("/api/v1/settings", "POST", {"brightness": 0.5})):
            self.assertEqual(self.request(path, self.chris, payload, method)[0], 401)
        self.assertEqual(self.request("/api/v1/presence/chris", self.chris, method="GET")[0], 404)
        for credential in (self.server.api_key_path.read_text().strip(), self.server.alexa_token_path.read_text().strip()):
            self.assertEqual(self.request("/api/v1/presence/chris", credential, self.report())[0], 401)
        self.assertEqual(self.store.snapshot()["lisa"]["state"], "unknown")

    def test_disabled_presence_invalid_observation_and_live_token_rotation(self):
        self.config["presence_enabled"] = False
        self.assertEqual(self.request("/api/v1/presence/chris", self.chris, self.report())[0], 503)
        self.config["presence_enabled"] = True
        self.assertEqual(self.request("/api/v1/presence/chris", self.chris, {"state": "home"})[0], 400)
        replacement = self.store.rotate("chris")
        self.assertEqual(self.request("/api/v1/presence/chris", self.chris, self.report())[0], 401)
        self.assertEqual(self.request("/api/v1/presence/chris", replacement, self.report())[0], 200)

    def test_owntracks_response_is_an_array_and_ack_filters_callback_data(self):
        payload = {"_type": "location", "lat": 33.6407, "lon": -84.4277, "acc": 5,
                   "tst": datetime.now(timezone.utc).timestamp()}
        self.assertEqual(self.request("/api/v1/presence/chris", self.chris, payload), (200, []))
        self.callbacks["update_presence"] = lambda _person, _payload: {"updated": True, "flights": ["private"], "lat": 33.64}
        status, result = self.request("/api/v1/presence/lisa", self.lisa, self.report())
        self.assertEqual(status, 200)
        self.assertEqual(result, {"accepted": True, "updated": True})

    def test_owntracks_basic_auth_requires_the_matching_profile_and_presence_route(self):
        from urllib.request import HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm, ProxyHandler, Request, build_opener
        def basic(person, token):
            return "Basic " + base64.b64encode((person + ":" + token).encode()).decode()
        payload = {"_type": "transition", "event": "enter", "desc": "Home",
                   "tst": datetime.now(timezone.utc).timestamp()}
        status, result = self.request("/api/v1/presence/chris", "", payload,
                                      headers={"Authorization": basic("chris", self.chris)})
        self.assertEqual((status, result), (200, []))
        for authorization in (basic("lisa", self.chris), "Basic ???", "Basic " + base64.b64encode(b"missing-colon").decode()):
            self.assertEqual(self.request("/api/v1/presence/chris", "", payload,
                                          headers={"Authorization": authorization})[0], 401)
        self.assertEqual(self.request("/api/v1/status", "", method="GET",
                                      headers={"Authorization": basic("chris", self.chris)})[0], 401)
        # A standards-based HTTP client can follow the Basic challenge too.
        url = f"http://127.0.0.1:{self.server.address[1]}/api/v1/presence/chris"
        passwords = HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, url, "chris", self.chris)
        opener = build_opener(ProxyHandler({}), HTTPBasicAuthHandler(passwords))
        with opener.open(Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}), timeout=5) as response:
            self.assertEqual(json.load(response), [])

    def test_owntracks_backlog_drains_over_http_without_renewing_or_saving_discarded_reports(self):
        now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
        self.callbacks["update_presence"] = lambda person, payload: self.store.update(person, payload, now)
        basic = "Basic " + base64.b64encode(("chris:" + self.chris).encode()).decode()
        headers = {"Authorization": basic}
        current = {"_type": "location", "lat": 33.6407, "lon": -84.4277, "acc": 5, "tst": now.timestamp()}
        self.assertEqual(self.request("/api/v1/presence/chris", "", current, headers=headers), (200, []))
        before = self.store.path.read_bytes()
        modified = self.store.path.stat().st_mtime_ns
        waypoint = {"_type": "waypoint", "desc": "Private activity", "lat": 33.64, "lon": -84.42, "tst": now.timestamp()}
        backlog = [{"_type": "status", "android": {"loc": 1, "bo": 0}},
                   dict(current, tst=now.timestamp() - 5400), waypoint,
                   {"_type": "waypoints", "waypoints": [waypoint]},
                   {"_type": "transition", "event": "leave", "desc": "Private activity", "tst": now.timestamp()},
                   {"_type": "lwt", "tst": now.timestamp() - 20000},
                   dict(current, lat=34, tst=now.timestamp() - 1)]
        for payload in backlog:
            with self.subTest(kind=payload["_type"]):
                self.assertEqual(self.request("/api/v1/presence/chris", "", payload, headers=headers), (200, []))
                self.assertEqual(self.store.path.read_bytes(), before)
                self.assertEqual(self.store.path.stat().st_mtime_ns, modified)
        self.assertEqual(self.store.snapshot(now)["chris"]["last_seen"], "2026-11-05T18:00:00.000000Z")
        self.assertNotIn("Private activity", self.store.path.read_text())
        self.assertEqual(self.store.snapshot(now + timedelta(minutes=90))["chris"]["state"], "unknown")
        fresh = dict(current, lat=34, tst=now.timestamp() + 1)
        self.assertEqual(self.request("/api/v1/presence/chris", "", fresh, headers=headers), (200, []))
        self.assertEqual(self.store.snapshot(now)["chris"]["state"], "away")

    def test_owntracks_discard_path_still_checks_authentication_enabled_state_and_malformed_reports(self):
        now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
        self.callbacks["update_presence"] = lambda person, payload: self.store.update(person, payload, now)
        telemetry = {"_type": "status", "android": {"loc": 1}}
        before = self.store.path.read_bytes()
        self.assertEqual(self.request("/api/v1/presence/chris", self.lisa, telemetry)[0], 401)
        self.config["presence_enabled"] = False
        self.assertEqual(self.request("/api/v1/presence/chris", self.chris, telemetry)[0], 503)
        self.config["presence_enabled"] = True
        bad = [dict(telemetry, tst=now.timestamp() + 121), dict(telemetry, android=[]),
               {"_type": "waypoint", "desc": "Private region", "tst": "bad"},
               {"_type": "waypoints", "waypoints": [{"_type": "cmd"}]},
               {"_type": "lwt"}, {"_type": "lwt", "tst": True},
               {"_type": "location", "tst": now.timestamp() - 5400},
               {"_type": "location", "tst": now.timestamp() + 121, "lat": 33.64, "lon": -84.42},
               {"_type": "transition", "tst": now.timestamp(), "desc": "Other region", "event": "invalid"},
               {"_type": "transition", "tst": now.timestamp(), "desc": " ", "event": "enter"},
               {"_type": "waypoint", "tst": now.timestamp(), "desc": " "},
               {"_type": "transition", "tst": now.timestamp() + 121, "desc": "Other region", "event": "enter"},
               {"_type": "unknown"}, {"_type": "cmd", "action": "reportLocation"},
               {"_type": "configuration", "username": "private"}]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self.request("/api/v1/presence/chris", self.chris, payload)[0], 400)
                self.assertEqual(self.store.path.read_bytes(), before)
        code, error = self.request("/api/v1/presence/chris", self.chris, {"_type": "encrypted", "data": "private"})
        self.assertEqual(code, 400)
        self.assertIn("payload encryption", error["error"])
        self.assertIn("HTTPS", error["error"])
        self.assertNotIn("private", json.dumps(error))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_http_owntracks_same_timestamp_conflicts_and_manual_reports_remain_strict(self):
        now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
        clock = [now]
        self.callbacks["update_presence"] = lambda person, payload: self.store.update(person, payload, clock[0])
        current = {"_type": "location", "lat": 33.6407, "lon": -84.4277, "tst": now.timestamp()}
        self.assertEqual(self.request("/api/v1/presence/chris", self.chris, current), (200, []))
        before = self.store.path.read_bytes()
        transition = {"_type": "transition", "desc": "Home", "event": "enter", "tst": now.timestamp()}
        for at in (now, now + timedelta(minutes=90)):
            clock[0] = at
            self.assertEqual(self.request("/api/v1/presence/chris", self.chris, transition), (200, []))
            self.assertEqual(self.store.path.read_bytes(), before)
            self.assertEqual(self.request("/api/v1/presence/chris", self.chris, dict(current, lat=34))[0], 400)
            self.assertEqual(self.request("/api/v1/presence/chris", self.chris, dict(transition, event="leave"))[0], 400)
            self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot(clock[0])["chris"]["state"], "unknown")
        clock[0] = now
        for seconds in (-1, -5400):
            payload = {"state": "away", "observed_at": (now + timedelta(seconds=seconds)).isoformat()}
            self.assertEqual(self.request("/api/v1/presence/chris", self.chris, payload)[0], 400)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_private_display_requires_static_local_control_token_not_oauth(self):
        self.callbacks["get_display_status"] = lambda: {"private_cards": [{"body": "eligible wall-only content"}]}
        admin = self.server.api_key_path.read_text().strip()
        with self.server._lock:
            oauth = self.server._mint("test-client", "flightwall.control")["access_token"]
        self.assertEqual(self.request("/api/v1/status", oauth, method="GET")[0], 200)
        for token in (oauth, self.chris, self.lisa, self.server.alexa_token_path.read_text().strip()):
            self.assertEqual(self.request("/api/v1/display", token, method="GET")[0], 401)
        status, value = self.request("/api/v1/display", admin, method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(value["private_cards"][0]["body"], "eligible wall-only content")
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_display_status"}}
        self.assertTrue(self.request("/mcp", oauth, rpc)[1]["result"]["isError"])

    def test_notification_credential_is_separate_from_presence_and_cannot_read_display(self):
        from flightwall.messages import MessageFeeds
        self.config.update(messages_enabled=True, chris_notifications_enabled=True,
                           chris_notification_apps='["Airbnb"]')
        feeds = MessageFeeds(self.temp.name, lambda: self.config)
        token = feeds.rotate_notification_token("chris")
        self.callbacks["receive_notification"] = feeds.receive_notification
        payload = {"app": "Airbnb", "sender": "Selected guest", "preview": "A private preview",
                   "timestamp": datetime.now(timezone.utc).isoformat(), "request_id": "notification-test"}
        status, result = self.request("/api/v1/notifications/chris", token, payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["accepted"])
        self.assertTrue(result["received"])
        self.assertNotIn("A private preview", json.dumps(result))
        self.assertEqual(self.request("/api/v1/notifications/chris", self.chris, payload)[0], 401)
        self.assertEqual(self.request("/api/v1/notifications/lisa", token, payload)[0], 401)
        self.assertEqual(self.request("/api/v1/presence/chris", token, self.report())[0], 401)
        self.assertEqual(self.request("/api/v1/display", token, method="GET")[0], 401)


if __name__ == "__main__":
    unittest.main()
