"""Household cards must coexist with the flight board and expire while viewed."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from flightwall.backend import Runtime
from flightwall.config import DEFAULTS, ConfigStore
from flightwall.display import active_personal_items, rotation_views
from flightwall.render import demo_snapshot

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)


def card(number="a"):
    return {"id": number, "kind": "appointment", "title": "Example appointment", "timezone": "America/New_York",
            "display_start": "2026-11-03T05:00:00Z", "display_end": "2026-11-03T17:25:00Z",
            "start": "2026-11-03T16:25:00Z", "end": "2026-11-03T17:25:00Z", "source": "manual"}


class HouseholdIntegrationTests(unittest.TestCase):
    def test_cards_join_rotation_without_replacing_operating_flights(self):
        config = dict(DEFAULTS)
        status = {"on_duty": True, "personal_items": [card(str(i)) for i in range(20)]}
        self.assertEqual(rotation_views(status, config, NOW), ["details", "map", "schedule", "personal"])
        self.assertEqual(len(active_personal_items(status, config, NOW)), 20)
        self.assertEqual(rotation_views(status, config, datetime(2026, 11, 3, 17, 25, tzinfo=UTC)), ["details", "map", "schedule"])
        config["personal_enabled"] = False
        self.assertEqual(rotation_views(status, config, NOW), ["details", "map", "schedule"])

    def test_expiry_is_enforced_between_backend_polls(self):
        status = {"personal_items": [card()]}
        for stamp, count in (("2026-11-03T04:59:59+00:00", 0), ("2026-11-03T05:00:00+00:00", 1),
                             ("2026-11-03T17:24:59+00:00", 1), ("2026-11-03T17:25:00+00:00", 0)):
            self.assertEqual(len(active_personal_items(status, DEFAULTS, datetime.fromisoformat(stamp))), count)

    def test_personal_storage_failure_preserves_flight_status(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Runtime(Path(folder) / "data", Path(folder) / "install")
            runtime.personal.path.write_text("broken-json")
            status = runtime.status()
            self.assertTrue(status["snapshot"]["demo"])
            self.assertTrue(status["snapshot"]["flights"])
            self.assertEqual(status["personal_items"], [])
            self.assertIn("could not be read", status["personal_error"])

    def test_real_example_survives_new_runtime_and_calendar_stays_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            data, install = Path(folder) / "data", Path(folder) / "install"
            runtime = Runtime(data, install)
            runtime.config.update({"timezone": "America/New_York"})
            with patch("flightwall.personal._now", return_value=datetime(2026, 10, 22, tzinfo=UTC)):
                created = runtime.create_personal_item({"kind": "appointment", "title": "Emerson doctor appointment",
                                                      "date": "2026-11-03", "time": "11:25", "source": "alexa", "request_id": "request-appointment"})
            item = created["item"]
            self.assertEqual(datetime.fromisoformat(item["end"].replace("Z", "+00:00")), datetime(2026, 11, 3, 17, 25, tzinfo=UTC))
            with patch("flightwall.personal._now", return_value=NOW):
                status = Runtime(data, install).status(now=NOW)
                self.assertEqual([entry["id"] for entry in status["personal_items"]], [item["id"]])
            self.assertFalse((data / "google-token.json").exists())
            self.assertFalse((data / "calendar-cache.json").exists())
            self.assertTrue(ConfigStore(data).get()["demo"])

    @unittest.skipUnless(importlib.util.find_spec("pygame"), "pygame display dependency not available")
    def test_real_personal_card_is_not_mislabeled_as_a_demo_flight(self):
        import pygame
        from flightwall.display import run_gui

        status = {"config": deepcopy(DEFAULTS), "snapshot": demo_snapshot(NOW), "on_duty": True, "personal_items": [card()]}
        states = []

        class Connection:
            def status(self):
                return status

        class Screen:
            gl = None
            def __init__(self, *args):
                pass
            def draw(self, *args):
                pass

        def render(state, config):
            states.append(deepcopy(state))
            return Image.new("RGB", (512, 256))

        import os
        os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
        select = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_4)
        with patch("flightwall.display.datetime") as clock, patch("flightwall.display.DisplayScreen", Screen), \
                patch("flightwall.display.render_frame", side_effect=render), patch.object(pygame.event, "get", return_value=[select]):
            clock.now.return_value = NOW
            run_gui(Connection(), windowed=True, max_frames=1)
        self.assertEqual(states[0]["view"], "personal")
        self.assertFalse(states[0]["demo"])
        self.assertEqual(states[0]["selected_personal_item"]["id"], "a")
        self.assertTrue(status["snapshot"]["demo"])


if __name__ == "__main__":
    unittest.main()
