"""Service/CLI integration with real config files and the real calendar cache."""

import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flightwall.backend import Runtime, choose_flight, is_on_duty
from flightwall.calendar_client import CalendarClient, CalendarError, parse_events
from flightwall.config import ConfigStore


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def timed_event(title, start="2026-11-06T18:00:00Z", end="2026-11-06T20:00:00Z"):
    return {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}}


class FlightSelectionTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"flights": parse_events([
            timed_event("Report: 1200L", "2026-11-06T17:00:00Z", "2026-11-07T08:00:00Z"),
            timed_event("900: ATL-MEM", "2026-11-06T10:00:00Z", "2026-11-06T11:30:00Z"),
            timed_event("1946(DH): ATL-SJU(1319-1749)", "2026-11-06T18:19:00Z", "2026-11-06T21:49:00Z"),
            timed_event("DL1946: SJU-ATL(1859-2151)", "2026-11-06T22:59:00Z", "2026-11-07T02:51:00Z"),
            timed_event("400: ATL-LAX", "2026-11-07T03:30:00Z", "2026-11-07T08:00:00Z"),
        ])}

    def test_operating_flight_takes_over_when_deadheads_are_hidden(self):
        now = dt("2026-11-06T17:00:00Z")
        including = choose_flight(self.snapshot, {"show_deadheads": True}, now)
        operating = choose_flight(self.snapshot, {"show_deadheads": False}, now)
        self.assertEqual(including["number"], "1946")
        self.assertTrue(including["deadhead"])
        self.assertEqual(operating["number"], "DL1946")
        self.assertFalse(operating["deadhead"])

    def test_calendar_number_is_preserved_without_assuming_airline(self):
        now = dt("2026-11-06T17:00:00Z")
        bare = choose_flight(self.snapshot, {"airline": "UA", "show_deadheads": True}, now)
        prefixed = choose_flight(self.snapshot, {"airline": "UA", "show_deadheads": False}, now)
        self.assertEqual(bare["number"], "1946")
        self.assertEqual(prefixed["number"], "DL1946")
        self.assertNotIn("UA", bare["number"])

    def test_overnight_leg_stays_selected_until_actual_arrival(self):
        config = {"show_deadheads": False}
        flight = choose_flight(self.snapshot, config, dt("2026-11-07T01:30:00Z"))
        self.assertEqual(flight["destination"], "ATL")
        next_flight = choose_flight(self.snapshot, config, dt("2026-11-07T02:51:00Z"))
        self.assertEqual(next_flight["destination"], "LAX")
        self.assertIsNone(choose_flight(self.snapshot, config, dt("2026-11-07T08:00:00Z")))

    def test_report_time_controls_takeover_and_end_time_releases_it(self):
        flight = choose_flight(self.snapshot, {"show_deadheads": False}, dt("2026-11-06T17:00:00Z"))
        config = {"takeover_hours": 2}
        self.assertFalse(is_on_duty(flight, config, dt("2026-11-06T14:59:59Z")))
        self.assertTrue(is_on_duty(flight, config, dt("2026-11-06T15:00:00Z")))
        self.assertTrue(is_on_duty(flight, config, dt("2026-11-07T01:00:00Z")))
        self.assertFalse(is_on_duty(flight, config, dt("2026-11-07T02:51:00Z")))

    def test_invalid_dates_do_not_interrupt_selection(self):
        malformed = {"flights": [{"number": "bad", "start": "invalid", "end": "2026-11-06T23:00:00Z"},
                                  {"number": "bad-end", "start": "2026-11-06T18:00:00Z", "end": "invalid"},
                                  *self.snapshot["flights"]]}
        selected = choose_flight(malformed, {}, dt("2026-11-06T17:00:00Z"))
        self.assertEqual(selected["number"], "1946")
        self.assertFalse(is_on_duty({"start": "invalid", "end": "invalid"}, {}, dt("2026-11-06T17:00:00Z")))


class ServiceConfigIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data_dir = self.root / "state"
        self.runtime = Runtime(self.data_dir, self.root / "install")
        # A distinct instance represents a separate authorize/select-calendar
        # CLI process writing atomically while the service remains running.
        self.cli_store = ConfigStore(self.data_dir)

    def tearDown(self):
        self.runtime.stop()
        self.directory.cleanup()

    def cache_schedule(self, events):
        config = self.cli_store.get()
        with patch.object(CalendarClient, "_api", return_value={"items": copy.deepcopy(events)}):
            snapshot = CalendarClient(self.data_dir, config).sync()
        self.assertIsNone(snapshot["error"])
        return snapshot

    def select(self, calendar_id="calendar-a", calendar_name="FC View"):
        self.cli_store.update({"calendar_id": calendar_id, "calendar_name": calendar_name, "demo": False})

    def test_separate_cli_updates_are_visible_and_later_settings_preserve_them(self):
        self.assertTrue(self.runtime.config.get()["demo"])
        self.cli_store.update({"demo": False, "calendar_id": "calendar-a", "airline": "dl"})
        observed = self.runtime.config.get()
        self.assertFalse(observed["demo"])
        self.assertEqual(observed["calendar_id"], "calendar-a")
        self.assertEqual(observed["airline"], "DL")
        self.runtime.update_settings({"brightness": 0.42})
        cli_observed = self.cli_store.get()
        self.assertEqual(cli_observed["brightness"], 0.42)
        self.assertEqual(cli_observed["calendar_id"], "calendar-a")
        self.assertFalse(cli_observed["demo"])

    def test_authorized_cli_selection_replaces_running_demo_from_real_cache(self):
        self.assertTrue(self.runtime.snapshot()["demo"])
        self.select()
        expected = self.cache_schedule([timed_event("123: ATL-MEM")])
        observed = self.runtime.snapshot()
        self.assertFalse(observed["demo"])
        self.assertEqual(observed["flights"], expected["flights"])
        self.assertEqual(observed["last_sync"], expected["last_sync"])
        self.assertTrue(self.runtime.refresh_event.is_set())

    def test_calendar_switch_immediately_clears_old_flights_then_loads_new_cache(self):
        self.select()
        self.cache_schedule([timed_event("123: ATL-MEM")])
        self.assertEqual(self.runtime.snapshot()["flights"][0]["destination"], "MEM")
        self.runtime.refresh_event.clear()
        self.select("calendar-b", "Buzz Flight Brief")
        # Even before network sync completes, the old calendar cannot appear
        # under the newly selected calendar's source label.
        changed = self.runtime.snapshot()
        self.assertEqual(changed["flights"], [])
        self.assertEqual(changed["source"], "Buzz Flight Brief")
        self.assertTrue(self.runtime.refresh_event.is_set())
        expected = self.cache_schedule([timed_event("456: ATL-LAX")])
        self.assertEqual(self.runtime.snapshot()["flights"], expected["flights"])

    def test_manual_cli_sync_with_same_calendar_is_visible_without_poll_delay(self):
        self.select()
        self.cache_schedule([timed_event("123: ATL-MEM")])
        self.assertEqual(self.runtime.snapshot()["flights"][0]["destination"], "MEM")
        updated = self.cache_schedule([timed_event("456: ATL-LAX")])
        self.assertEqual(self.runtime.snapshot()["flights"], updated["flights"])

    def test_inflight_old_calendar_response_cannot_publish_after_cli_switch(self):
        self.select()
        old = self.cache_schedule([timed_event("123: ATL-MEM")])
        self.runtime.snapshot()
        self.runtime.refresh_event.clear()

        def complete_old_request(_calendar_client):
            self.select("calendar-b", "Buzz Flight Brief")
            return old

        with patch.object(CalendarClient, "sync", complete_old_request):
            self.runtime.sync()
        observed = self.runtime.snapshot()
        self.assertEqual(observed["flights"], [])
        self.assertEqual(observed["source"], "Buzz Flight Brief")
        self.assertTrue(self.runtime.refresh_event.is_set())

    def test_calendar_error_keeps_cached_schedule_and_displays_error(self):
        self.select()
        expected = self.cache_schedule([timed_event("123: ATL-MEM")])
        with patch.object(CalendarClient, "sync", side_effect=CalendarError("Google authorization expired; run authorize again.")):
            self.runtime.sync()
        observed = self.runtime.snapshot()
        self.assertEqual(observed["flights"], expected["flights"])
        self.assertIn("authorization expired", observed["error"])


if __name__ == "__main__":
    unittest.main()
