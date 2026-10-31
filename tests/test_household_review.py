"""Independent review of whole-display household rotation and setup validation."""
from copy import deepcopy
from collections import Counter
from datetime import datetime, timedelta, timezone
import importlib.util
import unittest
from unittest.mock import patch

from PIL import Image

from flightwall.config import DEFAULTS, validate
from flightwall.render import demo_snapshot


NOW = datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc)


class HouseholdReviewTests(unittest.TestCase):
    def test_saved_time_settings_must_match_personal_store_clock_format(self):
        # A successful save must never make the household store reject its own
        # scheduling defaults. All four fields share the same UI format.
        for key in ("sleep_start", "sleep_end", "dinner_start", "dinner_end"):
            for invalid in ("1:005", " 1:30", "1: 30", "01:+5", "١٤:٣٠"):
                with self.subTest(key=key, value=invalid):
                    with self.assertRaises(ValueError):
                        validate(key, invalid)
            self.assertEqual(validate(key, "14:30"), "14:30")

    @unittest.skipUnless(importlib.util.find_spec("pygame"), "pygame dependency not available")
    def test_individual_cards_receive_fair_rotation_and_expire_without_displacing_flights(self):
        import pygame
        from flightwall.display import run_gui

        status = {"config": deepcopy(DEFAULTS) | {"rotation_seconds": 3},
                  "snapshot": demo_snapshot(NOW), "on_duty": True,
                  "personal_items": [{"id": name, "kind": "appointment", "title": name,
                                      "start": "2026-11-03T16:25:00Z", "end": "2026-11-03T17:25:00Z",
                                      "display_start": "2026-11-03T05:00:00Z",
                                      "display_end": "2026-11-03T17:25:00Z"}
                                     for name in ("first", "second", "third")]}
        expiry = NOW + timedelta(seconds=24)
        status["personal_items"][-1].update(start=(NOW - timedelta(minutes=5)).isoformat(),
                                            end=expiry.isoformat(), display_end=expiry.isoformat())
        captured = []

        class Connection:
            def status(self):
                return deepcopy(status)

        class Screen:
            gl = None
            def __init__(self, *args):
                pass
            def draw(self, *args):
                pass

        class Clock:
            def tick(self, *args):
                pass

        def render(state, config):
            captured.append((datetime.fromisoformat(state["now"]), state["view"],
                             (state.get("selected_personal_item") or {}).get("id")))
            return Image.new("RGB", (512, 256))

        ticks = [0, 0] + [3 * index for index in range(1, 17)]
        with patch("flightwall.display.datetime") as wall, \
                patch("flightwall.display.time.monotonic", side_effect=ticks), \
                patch("flightwall.display.ShaderScreen", Screen), \
                patch("flightwall.display.render_frame", side_effect=render), \
                patch.object(pygame.event, "get") as events, \
                patch.object(pygame.display, "init"), \
                patch.object(pygame.joystick, "init"), \
                patch.object(pygame.joystick, "get_count", return_value=0), \
                patch.object(pygame.time, "Clock", Clock), \
                patch.object(pygame, "quit"):
            wall.now.return_value = NOW
            frame = [0]
            def advance_wall():
                frame[0] += 1
                wall.now.return_value = NOW + timedelta(seconds=3 * frame[0])
                return []
            events.side_effect = advance_wall
            run_gui(Connection(), windowed=True, max_frames=16)

        # Each of the first three eligible cards gets one slot in a complete
        # round, alongside all three aviation views.
        first_round = captured[:6]
        self.assertEqual(Counter(item for stamp, view, item in first_round if view == "personal"),
                         Counter({"first": 1, "second": 1, "third": 1}))
        self.assertEqual({view for stamp, view, item in first_round if view != "personal"},
                         {"details", "map", "schedule"})
        remaining = [(view, item) for stamp, view, item in captured if stamp >= expiry]
        self.assertNotIn("third", [item for view, item in remaining])
        counts = Counter(item for view, item in remaining if view == "personal")
        self.assertEqual(set(counts), {"first", "second"})
        self.assertLessEqual(abs(counts["first"] - counts["second"]), 1)
        self.assertEqual({view for view, item in remaining if view != "personal"},
                         {"details", "map", "schedule"})


if __name__ == "__main__":
    unittest.main()
