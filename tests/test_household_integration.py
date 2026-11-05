"""Coordinator privacy tests using real presence, personal and content stores."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from flightwall.backend import Runtime
from flightwall.config import private_json
from flightwall.household_calendar import parse_kids_events
from flightwall.household_policy import build_rotation
from flightwall.remote import RemoteServer


UTC = timezone.utc
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def message(owner, category, title):
    return {"id": owner + "-" + category, "kind": "message", "owner": owner,
            "category": category, "filtered": True, "title": title, "body": title + " preview",
            "source": "Selected source", "observed_at": iso(NOW), "expires_at": iso(NOW + timedelta(hours=24))}


def image_data(color):
    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class HouseholdRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.runtime = Runtime(self.root / "state", self.root / "install")
        self.runtime.config.update({"demo": False, "timezone": "America/Chicago", "presence_enabled": True,
                                    "personal_enabled": True, "messages_enabled": True, "lisa_private_enabled": True,
                                    "inspiration_enabled": True, "health_enabled": True,
                                    "health_query": "PRIVATE-HEALTH-QUERY", "inspiration_topics": "PRIVATE-DESIGN-TOPIC",
                                    "family_calendar_id": "synthetic-shared-custody", "family_calendar_name": "Household",
                                    "family_calendar_profile": "chris", "kids_filter_enabled": True,
                                    "presence_ttl_minutes": 90, "kids_stale_minutes": 30})
        self.flight_snapshot = {"flights": [], "last_sync": iso(NOW), "error": None, "source": "FC View", "demo": False}
        self.snapshot_patch = patch.object(self.runtime, "snapshot", side_effect=lambda: deepcopy(self.flight_snapshot))
        self.snapshot_patch.start()
        self.content_clock = patch("flightwall.content._now", return_value=NOW)
        self.content_clock.start()
        self.mail = {"chris": [message("chris", "rental", "CHRIS-RENTAL"), message("chris", "lawyer", "CHRIS-LAWYER"),
                               message("chris", "all-inbox", "UNFILTERED-INBOX")],
                     "lisa": [message("lisa", "personal", "LISA-PRIVATE-MAIL")]}
        self.mail_patch = patch.object(self.runtime.messages, "status", side_effect=lambda person: {"items": deepcopy(self.mail[person])})
        self.mail_status = self.mail_patch.start()
        self.runtime.content.add_card({"owner": "lisa", "kind": "artwork", "title": "LISA-PRIVATE-ART",
                                       "body": "LISA-PRIVATE-ART-BODY", "image_data": image_data("red")})
        self.runtime.content.add_card({"owner": "public", "kind": "artwork", "title": "PUBLIC-ART",
                                       "body": "Public creative card", "image_data": image_data("blue")})
        self.write_health()
        self.set_presence("away", "home")
        self.set_kids("Dad")

    def tearDown(self):
        self.snapshot_patch.stop()
        self.mail_patch.stop()
        self.content_clock.stop()
        self.runtime.stop()
        self.directory.cleanup()

    def set_presence(self, chris, lisa, observed=NOW):
        self.runtime.presence.path.unlink(missing_ok=True)
        for person, state in (("chris", chris), ("lisa", lisa)):
            self.runtime.presence.update(person, {"state": state, "observed_at": iso(observed)}, now=observed)

    def set_kids(self, parent, synced=NOW):
        intervals = parse_kids_events([{"summary": parent + "'s House", "start": {"date": "2026-10-22"},
                                        "end": {"date": "2026-10-23"}}], "America/Chicago") if parent else []
        self.runtime.household._update("family", {"binding": self.runtime.household._family_binding(self.runtime.config.get()),
                                                  "intervals": intervals, "last_sync": iso(synced), "error": None})

    def write_health(self, published=NOW, fetched=NOW):
        query = self.runtime.config.get()["health_query"]
        private_json(self.runtime.content.research_path, {"selection": hashlib.sha256(query.encode()).hexdigest(),
                     "last_sync": iso(fetched), "error": None, "cards": [
                         {"id": "private-health", "kind": "health", "owner": "lisa", "profile": "lisa",
                          "title": "LISA-PRIVATE-HEALTH", "body": "LISA-PRIVATE-HEALTH-BODY",
                          "source": "ClinicalTrials.gov", "source_url": "https://clinicaltrials.gov/study/SYNTHETIC",
                          "published_at": iso(published), "fetched_at": iso(fetched)}]})

    def test_chris_home_gets_only_filtered_rental_and_lawyer_cards(self):
        self.set_presence("home", "home")
        result = self.runtime.display_status(NOW)
        self.assertEqual([item["category"] for item in result["messages"]["chris"]], ["rental", "lawyer"])
        self.assertEqual(result["messages"]["lisa"], [])
        self.assertFalse(result["household_context"]["private_lisa"])
        self.assertNotIn("UNFILTERED-INBOX", json.dumps(result))
        self.assertNotIn("LISA-PRIVATE", json.dumps(result))
        self.mail_status.assert_called_once_with("chris")

    def test_private_family_agenda_data_stays_on_the_display_route(self):
        household = self.runtime.household.status(now=NOW)
        household["custody_outlook"]["activities"] = [{"title": "Rory Dance", "start": iso(NOW),
                                                        "end": iso(NOW + timedelta(hours=1))}]
        self.flight_snapshot["flights"] = [{"id": "test", "number": "DL111", "origin": "ATL", "destination": "MEM",
                                            "start": iso(NOW), "end": iso(NOW + timedelta(hours=1))}]
        with patch.object(self.runtime.household, "status", return_value=household):
            visible = self.runtime.display_status(NOW)["custody_outlook"]
            maintenance = self.runtime.status(NOW)["custody_outlook"]
        self.assertEqual(visible["activities"][0]["title"], "Rory Dance")
        self.assertTrue(visible["buzz_plan"]["2026-10-22"]["away"])
        self.assertTrue({"activities", "activity_last_sync", "buzz_plan", "roster_last_sync"}.isdisjoint(maintenance))

    def test_lisa_private_cards_require_all_three_fresh_household_conditions(self):
        result = self.runtime.display_status(NOW)
        self.assertTrue(result["household_context"]["private_lisa"])
        self.assertEqual(result["messages"]["chris"], [])
        self.assertEqual(result["messages"]["lisa"][0]["title"], "LISA-PRIVATE-MAIL")
        self.assertEqual(result["health"][0]["title"], "LISA-PRIVATE-HEALTH")
        private_art = next(item for item in result["inspiration"] if item["owner"] == "lisa")
        self.assertTrue(private_art["image_data"].startswith("data:image/png;base64,"))
        self.assertTrue(any(item["owner"] == "public" for item in result["inspiration"]))
        self.mail_status.assert_called_once_with("lisa")

    def test_guest_unknown_and_stale_household_states_fail_closed(self):
        cases = [({"guest_mode": True}, "away", "home", "Dad", NOW, NOW),
                 ({}, "unknown", "home", "Dad", NOW, NOW),
                 ({}, "away", "unknown", "Dad", NOW, NOW),
                 ({}, "home", "home", "Dad", NOW, NOW),
                 ({}, "away", "home", "Mom", NOW, NOW),
                 ({}, "away", "home", None, NOW, NOW),
                 ({}, "away", "home", "Dad", NOW - timedelta(minutes=91), NOW),
                 ({}, "away", "home", "Dad", NOW, NOW - timedelta(minutes=31))]
        for change, chris, lisa, parent, observed, synced in cases:
            with self.subTest(change=change, chris=chris, lisa=lisa, parent=parent, observed=observed, synced=synced):
                self.runtime.config.update({"guest_mode": False, **change})
                self.set_presence(chris, lisa, observed)
                self.set_kids(parent, synced)
                result = self.runtime.display_status(NOW)
                self.assertFalse(result["household_context"]["private_lisa"])
                self.assertEqual(result["messages"]["lisa"], [])
                self.assertEqual(result["health"], [])
                self.assertNotIn("LISA-PRIVATE", json.dumps(result))

    def test_maintenance_status_strips_private_mail_health_and_all_image_bytes(self):
        display = self.runtime.display_status(NOW)
        self.assertIn("LISA-PRIVATE", json.dumps(display))
        self.assertIn("data:image/", json.dumps(display))
        maintenance = self.runtime.status(NOW)
        self.assertEqual(maintenance["messages"]["lisa"], [])
        self.assertEqual(maintenance["health"], [])
        self.assertTrue(all(item["owner"] == "public" for item in maintenance["inspiration"]))
        serialized = json.dumps(maintenance)
        for private in ("LISA-PRIVATE", "PRIVATE-HEALTH-QUERY", "PRIVATE-DESIGN-TOPIC", "image_data", "data:image/"):
            self.assertNotIn(private, serialized)
        # Sanitization of a maintenance response must not erase saved display
        # cards or their private artwork for a later eligible local display.
        again = self.runtime.display_status(NOW)
        self.assertIn("LISA-PRIVATE-MAIL", json.dumps(again))
        self.assertTrue(any(item.get("image_data") for item in again["inspiration"] if item["owner"] == "lisa"))

    def test_actual_http_status_and_mcp_keep_private_display_content_local(self):
        callbacks = {"get_status": lambda: self.runtime.status(NOW),
                     "get_display_status": lambda: self.runtime.display_status(NOW)}
        server = RemoteServer(self.runtime.data_dir, self.runtime.config.get, callbacks, port=0).start()
        def request(path, payload=None):
            connection = http.client.HTTPConnection(*server.address, timeout=3)
            try:
                token = server.api_key_path.read_text().strip()
                connection.request("POST" if payload is not None else "GET", path,
                                   json.dumps(payload) if payload is not None else None,
                                   {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                return json.loads(response.read())
            finally:
                connection.close()
        try:
            display = request("/api/v1/display")
            self.assertIn("LISA-PRIVATE-MAIL", json.dumps(display))
            status = request("/api/v1/status")
            rpc = request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "get_status", "arguments": {}}})
            self.assertFalse(rpc["result"]["isError"])
            for result in (status, rpc):
                serialized = json.dumps(result)
                for private in ("LISA-PRIVATE", "PRIVATE-HEALTH-QUERY", "image_data", "data:image/"):
                    self.assertNotIn(private, serialized)
        finally:
            server.stop()

    def test_stale_or_unsourced_health_does_not_appear_even_when_lisa_is_alone(self):
        self.write_health(published=NOW - timedelta(days=10), fetched=NOW - timedelta(hours=169))
        self.assertEqual(self.runtime.display_status(NOW)["health"], [])
        self.write_health()
        stored = json.loads(self.runtime.content.research_path.read_text())
        stored["cards"][0]["source_url"] = "http://unverified.example.invalid"
        private_json(self.runtime.content.research_path, stored)
        self.assertEqual(self.runtime.display_status(NOW)["health"], [])

    def test_roster_priority_works_without_inventing_phone_presence(self):
        self.set_presence("unknown", "unknown")
        self.set_kids(None)
        self.runtime.config.update({"nearby_enabled": True, "home_latitude": 30.0, "home_longitude": -86.0})
        flight = {"id": "synthetic-flight", "number": "1946", "origin": "ATL", "destination": "MEM",
                  "start": iso(NOW + timedelta(minutes=30)), "end": iso(NOW + timedelta(hours=2)),
                  "deadhead": False, "source": "FC View"}
        self.flight_snapshot["flights"] = [flight]
        self.runtime.feeds.nearby = [{"flight": "Nearby synthetic aircraft"}]
        result = self.runtime.display_status(NOW)
        self.assertEqual(result["selected_flight"]["id"], flight["id"])
        self.assertTrue(result["household_context"]["flight_priority"])
        self.assertFalse(result["household_context"]["chris_away"])
        self.assertEqual(result["presence"]["chris"]["state"], "unknown")
        self.assertFalse(result["household_context"]["private_lisa"])
        slots = build_rotation(result, self.runtime.config.get(), NOW)
        self.assertNotIn("nearby", [slot["view"] for slot in slots])
        self.assertTrue(any(slot["view"] == "details" and slot["duration_seconds"] == 60 for slot in slots))

    def test_exact_clock_drives_personal_expiry_and_selected_flight(self):
        with patch("flightwall.personal._now", return_value=NOW):
            appointment = self.runtime.personal.create({"kind": "appointment", "title": "Doctor", "date": "2026-10-22", "time": "11:25"})["item"]
        before_end = datetime(2026, 10, 22, 17, 24, 59, tzinfo=UTC)
        after_end = datetime(2026, 10, 22, 17, 25, tzinfo=UTC)
        self.assertEqual([item["id"] for item in self.runtime.display_status(before_end)["personal_items"]], [appointment["id"]])
        self.assertEqual(self.runtime.display_status(after_end)["personal_items"], [])
        past = {"id": "ended", "number": "1", "origin": "ATL", "destination": "MEM",
                "start": iso(NOW - timedelta(hours=2)), "end": iso(NOW - timedelta(minutes=1)), "deadhead": False}
        future = {**past, "id": "future", "number": "2", "start": iso(NOW + timedelta(minutes=10)), "end": iso(NOW + timedelta(hours=1))}
        self.flight_snapshot["flights"] = [past, future]
        self.assertEqual(self.runtime.display_status(NOW)["selected_flight"]["id"], "future")


if __name__ == "__main__":
    unittest.main()
