"""Actual GUI wiring must keep privacy gates active even while paused."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from flightwall.config import DEFAULTS
from flightwall.display import (BackendConnection, RotationCursor, decode_artwork,
                                entry_render_state, fresh_household_status)
from flightwall.household_policy import build_rotation
from flightwall.render import demo_snapshot


NOW = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)


def fixture():
    config = deepcopy(DEFAULTS) | {"presence_enabled": True, "lisa_private_enabled": True,
                                  "messages_enabled": True, "guest_mode": False}
    status = {"config": config, "snapshot": demo_snapshot(NOW), "on_duty": False,
              "presence": {"chris": {"state": "away", "stale": False, "last_seen": NOW.isoformat()},
                           "lisa": {"state": "home", "stale": False, "last_seen": NOW.isoformat()}},
              "kids": {"state": "away", "stale": False, "last_sync": NOW.isoformat(),
                       "until": (NOW + timedelta(days=1)).isoformat()},
              "messages": {"lisa": [{"id": "private-message", "profile": "lisa", "category": "personal",
                                       "title": "FICTIONAL PRIVATE TITLE", "body": "Private fixture.", "source": "Demo sender",
                                       "observed_at": NOW.isoformat(), "expires_at": (NOW + timedelta(days=1)).isoformat()}]},
              "personal_items": [], "setup_required": False}
    return status, config


class DisplayHouseholdUnitTests(unittest.TestCase):
    def test_connection_uses_static_admin_display_route(self):
        connection = BackendConnection("/unused", {"control_port": 8765})
        with patch.object(connection, "request", return_value={}) as request:
            connection.status()
        request.assert_called_once_with("display")

    def test_cached_presence_expires_without_another_backend_poll(self):
        status, config = fixture()
        before = deepcopy(status)
        self.assertTrue(fresh_household_status(status, config, NOW)["household_context"]["private_lisa"])
        expired = fresh_household_status(status, config, NOW + timedelta(minutes=config["presence_ttl_minutes"]))
        self.assertEqual(expired["presence"]["chris"]["state"], "unknown")
        self.assertFalse(expired["household_context"]["private_lisa"])
        for record in ({"state": "away", "stale": False},
                       {"state": "away", "stale": False, "last_seen": (NOW + timedelta(seconds=301)).isoformat()}):
            candidate = deepcopy(status)
            candidate["presence"]["chris"] = record
            self.assertFalse(fresh_household_status(candidate, config, NOW)["household_context"]["private_lisa"])
        self.assertEqual(status, before)

    def test_custody_interval_end_and_cache_freshness_both_matter(self):
        status, config = fixture()
        status["kids"]["until"] = (NOW + timedelta(seconds=1)).isoformat()
        at_end = fresh_household_status(status, config, NOW + timedelta(seconds=1))
        self.assertEqual(at_end["kids"]["state"], "unknown")
        self.assertFalse(at_end["household_context"]["private_lisa"])
        status["kids"]["until"] = (NOW + timedelta(days=1)).isoformat()
        status["kids"]["last_sync"] = (NOW - timedelta(minutes=31)).isoformat()
        self.assertFalse(fresh_household_status(status, config, NOW)["household_context"]["private_lisa"])
        status["kids"].pop("last_sync")
        self.assertFalse(fresh_household_status(status, config, NOW)["household_context"]["private_lisa"])

    def test_backend_outage_immediately_revokes_presence_and_live_data(self):
        status, config = fixture()
        status["live"] = {"flight_id": "x"}
        status["nearby"] = [{"flight": "fixture"}]
        offline = fresh_household_status(status, config, NOW, backend_available=False)
        self.assertFalse(offline["household_context"]["private_lisa"])
        self.assertFalse(offline["household_context"]["chris_home"])
        self.assertEqual(offline["snapshot"]["flights"], status["snapshot"]["flights"])
        self.assertIsNone(offline["live"])
        self.assertEqual(offline["nearby"], [])
        self.assertNotIn("message", [entry["view"] for entry in build_rotation(offline, config, NOW)])

    def test_cursor_uses_each_slots_dwell_and_keeps_stable_keys(self):
        entries = [{"key": "details", "view": "details", "duration_seconds": 60},
                   {"key": "message:a", "view": "message", "duration_seconds": 15},
                   {"key": "map", "view": "map", "duration_seconds": 60}]
        cursor = RotationCursor(entries, 0)
        self.assertEqual(cursor.tick(entries, 59)["key"], "details")
        self.assertEqual(cursor.tick(entries, 60)["key"], "message:a")
        inserted = [{"key": "setup", "view": "setup", "duration_seconds": 15}] + entries
        self.assertEqual(cursor.tick(inserted, 74)["key"], "message:a")
        self.assertEqual(cursor.index, 2)
        self.assertEqual(cursor.tick(inserted, 75)["key"], "map")
        cursor.select(inserted, 2, 100)
        self.assertEqual(cursor.tick(inserted, 999, automatic=False)["key"], "message:a")
        removed = [entry for entry in inserted if entry["key"] != "message:a"]
        self.assertEqual(cursor.tick(removed, 1000, automatic=False)["key"], "setup")

    def test_postflight_entry_overrides_upcoming_selection_and_rejects_wrong_live_id(self):
        status, config = fixture()
        recent = {"id": "recent", "start": (NOW - timedelta(hours=2)).isoformat(), "end": (NOW - timedelta(minutes=30)).isoformat()}
        status["selected_flight"] = {"id": "next"}
        status["live"] = {"flight_id": "next", "source": "Test provider", "observed_at": NOW.isoformat()}
        entry = {"key": "map", "view": "map", "duration_seconds": 60, "selected_flight": recent}
        state = entry_render_state(status, config, entry, NOW)
        self.assertEqual(state["selected_flight"]["id"], "recent")
        self.assertIsNone(state["live"])
        status["live"]["flight_id"] = "recent"
        self.assertIsNotNone(entry_render_state(status, config, entry, NOW)["live"])
        status["live"]["observed_at"] = (NOW - timedelta(minutes=6)).isoformat()
        self.assertIsNone(entry_render_state(status, config, entry, NOW)["live"])

    def test_only_bounded_png_data_urls_can_become_artwork(self):
        image = Image.new("RGB", (32, 16), (100, 150, 200))
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        encoded = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
        self.assertEqual(decode_artwork(encoded).size, (32, 16))
        for invalid in (None, {}, "/etc/passwd", "https://example.org/image.png", "data:image/png;base64,!!!", "data:image/jpeg;base64,AAAA"):
            self.assertIsNone(decode_artwork(invalid))
        stream = io.BytesIO()
        Image.new("RGB", (513, 1)).save(stream, format="PNG")
        self.assertIsNone(decode_artwork("data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()))
        status, config = fixture()
        config["inspiration_enabled"] = True
        entry = {"key": "art", "view": "inspiration", "duration_seconds": 15,
                 "item": {"id": "public-art", "profile": "public", "title": "Demo art", "image_data": encoded}}
        state = entry_render_state(status, config, entry, NOW)
        self.assertIsInstance(state["artwork_image"], Image.Image)
        self.assertNotIn("image_data", state["selected_card"])
        self.assertIn("image_data", entry["item"])
        self.assertFalse(state["demo"])


@unittest.skipUnless(importlib.util.find_spec("pygame"), "pygame not installed")
class HouseholdGuiTests(unittest.TestCase):
    def run_frames(self, status, events, *, monotonic_times=None, wall_times=None, fail_poll=False, fail_settings=False):
        import pygame
        from flightwall.display import run_gui
        os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
        states, configs, writes = [], [], []
        monotonic = [0]
        count = [0]

        class Connection:
            calls = 0
            def status(self):
                self.calls += 1
                if fail_poll and self.calls > 1:
                    raise OSError("Offline")
                return deepcopy(status)
            def settings(self, patch_value):
                writes.append(patch_value)
                if fail_settings:
                    raise OSError("Offline")

        class Screen:
            gl = None
            def __init__(self, *args):
                pass
            def draw(self, *args):
                pass

        def render(state, config):
            states.append(deepcopy(state))
            configs.append(deepcopy(config))
            return Image.new("RGB", (512, 256))

        with patch("flightwall.display.datetime") as clock, patch("flightwall.display.DisplayScreen", Screen), \
                patch("flightwall.display.render_frame", side_effect=render), patch("flightwall.display.time.monotonic", side_effect=lambda: monotonic[0]), \
                patch.object(pygame.time, "Clock", return_value=SimpleNamespace(tick=lambda *args: None)):
            clock.now.return_value = NOW
            def next_events():
                index = count[0]
                count[0] += 1
                monotonic[0] = monotonic_times[index] if monotonic_times else index * .1
                clock.now.return_value = wall_times[index] if wall_times else NOW
                return events[index]
            with patch.object(pygame.event, "get", side_effect=next_events):
                run_gui(Connection(), windowed=True, max_frames=len(events))
        return states, configs, writes

    def test_paused_private_view_disappears_when_presence_expires(self):
        import pygame
        status, config = fixture()
        select = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_4)
        states, configs, writes = self.run_frames(status, [[select], []],
                                                  wall_times=[NOW, NOW + timedelta(minutes=config["presence_ttl_minutes"])])
        self.assertEqual(states[0]["view"], "message")
        self.assertFalse(states[0]["demo"])
        self.assertEqual(states[1]["view"], "details")
        self.assertIsNone(states[1]["selected_card"])
        self.assertFalse(states[1]["household_context"]["private_lisa"])

    def test_poll_failure_hides_paused_private_view_and_keeps_flights(self):
        import pygame
        status, config = fixture()
        select = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_4)
        states, configs, writes = self.run_frames(status, [[select], []], monotonic_times=[0, 3], fail_poll=True)
        self.assertEqual(states[0]["view"], "message")
        self.assertEqual(states[1]["view"], "details")
        self.assertTrue(states[1]["flights"])
        self.assertFalse(states[1]["household_context"]["private_lisa"])

    def test_guest_hotkey_hides_cards_even_if_save_fails_and_poll_returns_old_config(self):
        import pygame
        status, config = fixture()
        select = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_4)
        guest = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_g)
        states, configs, writes = self.run_frames(status, [[select], [guest], []], monotonic_times=[0, .5, 3], fail_settings=True)
        self.assertEqual(states[0]["view"], "message")
        self.assertNotEqual(states[1]["view"], "message")
        self.assertNotEqual(states[2]["view"], "message")
        self.assertTrue(configs[2]["guest_mode"])
        self.assertIn({"guest_mode": True}, writes)


if __name__ == "__main__":
    unittest.main()
