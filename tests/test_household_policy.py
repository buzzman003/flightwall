"""Privacy and flight priority are policy decisions, independent of the TV."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from flightwall.household_policy import build_rotation, card_visible, filter_personal_items, household_context


NOW = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)


def person(state):
    return {"state": state, "stale": False}


def status_fixture():
    return {"presence": {"chris": person("away"), "lisa": person("home")}, "kids": person("away"),
            "snapshot": {"demo": False, "flights": []}, "personal_items": [], "on_duty": False}


def config_fixture():
    return {"rotation_seconds": 15, "lisa_private_enabled": True, "guest_mode": False,
            "kids_filter_enabled": True, "personal_enabled": True,
            "messages_enabled": True, "inspiration_enabled": True, "health_enabled": True}


def card_fixture(**extra):
    return {"id": "fictional-card", "title": "Fictional card for testing", "body": "Test metadata only.",
            "source": "Example source", "source_url": "https://example.org/article",
            "published_at": (NOW - timedelta(days=1)).isoformat(), "observed_at": NOW.isoformat(), **extra}


class HouseholdPolicyTests(unittest.TestCase):
    def setUp(self):
        self.status, self.config = status_fixture(), config_fixture()

    def test_private_lisa_requires_every_explicit_known_condition(self):
        self.assertTrue(household_context(self.status, self.config, NOW)["private_lisa"])
        cases = []
        for who, state in (("chris", "home"), ("chris", "unknown"), ("lisa", "away"), ("lisa", "unknown")):
            status = deepcopy(self.status)
            status["presence"][who] = person(state)
            cases.append(status)
        for who in ("chris", "lisa"):
            for stale in (True, None, "false"):
                status = deepcopy(self.status)
                status["presence"][who]["stale"] = stale
                cases.append(status)
        for kids in (person("home"), person("unknown"), {"state": "away", "stale": True}, {"state": "away"}, None):
            status = deepcopy(self.status)
            status["kids"] = kids
            cases.append(status)
        for index, status in enumerate(cases):
            with self.subTest(index=index):
                self.assertFalse(household_context(status, self.config, NOW)["private_lisa"])
        for patch in ({"guest_mode": True}, {"lisa_private_enabled": False}, {"lisa_private_enabled": "true"}):
            self.assertFalse(household_context(self.status, self.config | patch, NOW)["private_lisa"])
        self.assertFalse(household_context({}, {}, NOW)["private_lisa"])

    def test_dinner_filter_unknown_and_stale_fail_closed_but_optout_preserves_behavior(self):
        dinner = {"id": "dinner", "kind": "dinner", "display_start": (NOW - timedelta(hours=1)).isoformat(),
                  "display_end": (NOW + timedelta(hours=1)).isoformat()}
        appointment = {**dinner, "id": "appointment", "kind": "appointment"}
        for kids in (person("away"), person("unknown"), {"state": "home", "stale": True}, None):
            self.status["kids"] = kids
            self.assertEqual([i["id"] for i in filter_personal_items([dinner, appointment], self.status, self.config, NOW)], ["appointment"])
        self.status["kids"] = person("home")
        self.assertEqual(len(filter_personal_items([dinner, appointment], self.status, self.config, NOW)), 2)
        self.status["kids"] = None
        self.assertEqual(len(filter_personal_items([dinner], self.status, self.config | {"kids_filter_enabled": False}, NOW)), 1)
        self.assertFalse(filter_personal_items([dinner], self.status, self.config | {"personal_enabled": False}, NOW))
        self.assertFalse(filter_personal_items([dinner], self.status, self.config | {"kids_filter_enabled": False}, NOW + timedelta(hours=1)))

    def test_flight_priority_exact_window_and_postflight_selection(self):
        flight = {"id": "calendar-flight", "start": NOW.isoformat(), "end": (NOW + timedelta(hours=2)).isoformat()}
        self.status["snapshot"]["flights"] = [flight]
        for delta, expected in ((-3601, False), (-3600, True), (0, True), (10799, True), (10800, False)):
            with self.subTest(delta=delta):
                self.assertEqual(household_context(self.status, self.config, NOW + timedelta(seconds=delta))["flight_priority"], expected)
        post = build_rotation(self.status, self.config, NOW + timedelta(hours=2, minutes=30))
        self.assertEqual(post[0]["selected_flight"]["id"], "calendar-flight")
        self.status["presence"]["chris"] = person("home")
        self.assertTrue(household_context(self.status, self.config, NOW)["flight_priority"])
        self.status["presence"]["chris"] = person("away")
        self.status["snapshot"]["demo"] = True
        self.assertFalse(household_context(self.status, self.config, NOW)["flight_priority"])

    def test_priority_interleaves_flights_before_every_short_card(self):
        self.status["snapshot"]["flights"] = [{"id": "f", "start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}]
        self.status["personal_items"] = [{"id": f"p{i}", "kind": "appointment", "display_start": (NOW - timedelta(hours=1)).isoformat(),
                                         "display_end": (NOW + timedelta(hours=1)).isoformat()} for i in range(20)]
        rotation = build_rotation(self.status, self.config, NOW)
        flight_views = []
        personal_ids = []
        for index, entry in enumerate(rotation):
            if index % 2 == 0:
                self.assertIn(entry["view"], {"details", "map"})
                self.assertEqual(entry["duration_seconds"], 60)
                flight_views.append(entry["view"])
            else:
                self.assertNotIn(entry["view"], {"details", "map"})
                self.assertEqual(entry["duration_seconds"], 15)
                if entry["view"] == "personal":
                    personal_ids.append(entry["item"]["id"])
        self.assertEqual(set(personal_ids), {f"p{i}" for i in range(20)})
        self.assertEqual(flight_views, ["details", "map"] * (len(flight_views) // 2))
        self.assertEqual(len({item["key"] for item in rotation}), len(rotation))

    def test_nearby_is_suppressed_when_chris_away_even_without_calendar(self):
        self.config["nearby_enabled"] = True
        self.assertNotIn("nearby", [e["view"] for e in build_rotation(self.status, self.config, NOW)])
        self.status["presence"]["chris"] = person("home")
        self.assertIn("nearby", [e["view"] for e in build_rotation(self.status, self.config, NOW)])

    def test_wizard_flight_window_and_focus_adjustments_are_honored(self):
        self.status["snapshot"]["flights"] = [{"id": "f", "start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}]
        config = self.config | {"flight_window_before_minutes": 30, "flight_window_after_minutes": 15, "flight_focus_seconds": 90}
        self.assertFalse(household_context(self.status, config, NOW - timedelta(minutes=31))["flight_priority"])
        self.assertTrue(household_context(self.status, config, NOW - timedelta(minutes=30))["flight_priority"])
        self.assertFalse(household_context(self.status, config, NOW + timedelta(hours=1, minutes=15))["flight_priority"])
        slots = build_rotation(self.status, config, NOW)
        self.assertTrue(all(slot["duration_seconds"] == 90 for slot in slots if slot["view"] in {"details", "map"}))

    def test_airplane_mode_keeps_roster_priority_without_granting_presence(self):
        self.status["presence"]["chris"] = {"state": "unknown", "stale": True}
        self.status["snapshot"]["flights"] = [{"id": "long-flight", "start": (NOW - timedelta(hours=3)).isoformat(), "end": (NOW + timedelta(hours=2)).isoformat()}]
        self.config["nearby_enabled"] = True
        context = household_context(self.status, self.config, NOW)
        self.assertTrue(context["flight_priority"])
        self.assertFalse(context["chris_away"])
        self.assertFalse(context["trip_away"])
        self.assertFalse(context["private_lisa"])
        rotation = build_rotation(self.status, self.config, NOW)
        self.assertEqual(rotation[0]["duration_seconds"], 60)
        self.assertNotIn("nearby", [slot["view"] for slot in rotation])

    def test_message_categories_and_presence_are_required(self):
        self.status["messages"] = {"chris": [card_fixture(id="rental", category="rental"), card_fixture(id="inbox", category="all-inbox")],
                                   "lisa": [card_fixture(id="lisa", category="personal")]}
        entries = build_rotation(self.status, self.config, NOW)
        self.assertEqual([e["item"]["id"] for e in entries if e["view"] == "message"], ["lisa"])
        self.status["presence"]["chris"] = person("home")
        entries = build_rotation(self.status, self.config, NOW)
        self.assertEqual([e["item"]["id"] for e in entries if e["view"] == "message"], ["rental"])
        self.assertNotIn("message", [e["view"] for e in build_rotation(self.status, self.config | {"messages_enabled": False}, NOW)])
        self.assertNotIn("message", [e["view"] for e in build_rotation(self.status, self.config | {"guest_mode": True}, NOW)])
        context = household_context(self.status, self.config, NOW)
        self.assertFalse(card_visible("message", card_fixture(profile="chris", category="rental"), context,
                                      self.config | {"guest_mode": True}, NOW))

    def test_health_requires_optin_verified_source_and_fresh_fetch(self):
        card = card_fixture()
        self.status["health_cards"] = [card]
        self.assertIn("health", [e["view"] for e in build_rotation(self.status, self.config, NOW)])
        failures = [{"source": ""}, {"source_url": "http://example.org/a"}, {"published_at": None},
                    {"observed_at": None}, {"observed_at": (NOW - timedelta(days=8)).isoformat()},
                    {"published_at": (NOW + timedelta(days=1)).isoformat()}, {"stale": True}]
        for patch in failures:
            with self.subTest(patch=patch):
                self.status["health_cards"] = [card | patch]
                self.assertNotIn("health", [e["view"] for e in build_rotation(self.status, self.config, NOW)])
        self.status["health_cards"] = [card]
        self.assertNotIn("health", [e["view"] for e in build_rotation(self.status, self.config | {"health_enabled": False}, NOW)])

    def test_public_inspiration_does_not_unlock_private_lisa_cards(self):
        self.status["inspiration"] = [card_fixture(id="public", profile="public"), card_fixture(id="private", profile="lisa")]
        self.status["presence"]["chris"] = person("home")
        ids = [e["item"]["id"] for e in build_rotation(self.status, self.config, NOW) if e["view"] == "inspiration"]
        self.assertEqual(ids, ["public"])

    def test_renderer_gate_does_not_trust_a_stale_private_boolean_alone(self):
        card = card_fixture(profile="lisa", category="personal")
        self.assertFalse(card_visible("message", card, {"private_lisa": True}, self.config, NOW))
        context = household_context(self.status, self.config, NOW)
        self.assertTrue(card_visible("message", card, context, self.config, NOW))
        self.assertFalse(card_visible("message", card, context, self.config | {"guest_mode": True}, NOW))
        self.assertFalse(card_visible("message", card | {"expires_at": NOW.isoformat()}, context, self.config, NOW))

    def test_policy_does_not_modify_account_snapshots(self):
        before = deepcopy(self.status)
        build_rotation(self.status, self.config, NOW)
        self.assertEqual(self.status, before)


if __name__ == "__main__":
    unittest.main()
