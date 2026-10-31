"""Provider-status TV card: observed clocks, same-event comparisons and expiry."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from flightwall import render
from flightwall.flight_alerts import normalize_operating_status
from flightwall.flight_status_view import render_operating


NOW = datetime(2026, 11, 1, 20, tzinfo=timezone.utc)
CONFIG = {"demo": False, "timezone": "America/Chicago", "flightaware_enabled": True}
SELECTED = {"id": "fictional-operating", "number": "DL123", "origin": "ATL", "destination": "VPS",
            "start": "2026-11-01T18:15:00Z", "end": "2026-11-01T21:00:00Z"}


def status(**changes):
    provider = {"fa_flight_id": "FAKE-PROVIDER-123", "cancelled": False, "diverted": False,
                "scheduled_out": "2026-11-01T18:15:00Z", "actual_out": "2026-11-01T18:25:00Z",
                "scheduled_in": "2026-11-01T21:00:00Z", "estimated_in": "2026-11-01T20:50:00Z",
                **changes}
    return normalize_operating_status(provider, SELECTED, NOW - timedelta(minutes=2))


def capture(value=None, *, now=NOW, config=None, **state_changes):
    state = {"demo": False, "display_timezone": "America/Chicago", "operating_status": status() if value is None else value,
             **state_changes}
    image = Image.new("RGB", (512, 256), render.BG)
    labels = []
    original = render._text
    def text(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=text):
        render_operating(ImageDraw.Draw(image), image, state, CONFIG if config is None else config, now)
    return labels, image


class OperatingStatusViewTests(unittest.TestCase):
    def test_real_provider_projection_shows_actual_estimate_baseline_and_signed_deltas(self):
        labels, image = capture()
        for value in ("DL123", "ATL > VPS", "DEPARTURE / GATE OUT", "ARRIVAL / GATE IN", "ACTUAL", "ESTIMATED",
                      "12:25 PM", "2:50 PM", "SCHED 11/01 12:15 PM", "SCHED 11/01 3:00 PM", "10 MIN LATE", "10 MIN EARLY"):
            self.assertIn(value, labels)
        self.assertIn("FLIGHTAWARE / 11/01 1:58 PM CST / 2M AGO", labels)
        self.assertEqual((image.size, image.mode), ((512, 256), "RGB"))
        self.assertFalse(any("CONFIDENCE" in label or "CANCELLED" in label for label in labels))

    def test_same_event_fallback_keeps_runway_times_distinct_from_gate_times(self):
        value = status(scheduled_out=None, actual_out=None, scheduled_in=None, estimated_in=None,
                       scheduled_off="2026-11-01T18:25:00Z", actual_off="2026-11-01T18:25:00Z",
                       scheduled_on="2026-11-01T20:30:00Z", estimated_on="2026-11-01T20:45:00Z")
        labels, _ = capture(value)
        self.assertIn("DEPARTURE / TAKEOFF", labels)
        self.assertIn("ARRIVAL / LANDING", labels)
        self.assertIn("ON TIME", labels)
        self.assertIn("15 MIN LATE", labels)
        self.assertNotIn("DEPARTURE / GATE OUT", labels)
        self.assertNotIn("ARRIVAL / GATE IN", labels)

    def test_missing_provider_baseline_or_estimate_never_becomes_on_time(self):
        for value in (status(scheduled_out=None), status(actual_out=None)):
            with self.subTest(value=value["departure"]):
                labels, _ = capture(value)
                self.assertIn("DELTA UNKNOWN", labels)
                self.assertNotIn("ON TIME", labels)
        labels, _ = capture(status(actual_out=None, estimated_out=None))
        self.assertIn("TIME UNKNOWN", labels)
        self.assertIn("SCHED 11/01 12:15 PM", labels)

    def test_paused_frame_hides_cached_estimates_after_five_minutes_or_backend_loss(self):
        value = status()
        original = deepcopy(value)
        # Initial fixture is two minutes old. Inclusive age300 remains shown;
        # the first instant afterward must hide estimates without another poll.
        self.assertIn("2:50 PM", capture(value, now=NOW + timedelta(minutes=3))[0])
        for changes in ({"now": NOW + timedelta(minutes=3, microseconds=1)}, {"backend_available": False}):
            with self.subTest(changes=changes):
                labels, _ = capture(value, **changes)
                self.assertIn("PROVIDER UPDATE STALE", labels)
                self.assertNotIn("2:50 PM", labels)
                self.assertNotIn("10 MIN EARLY", labels)
                self.assertNotIn("ACTUAL", labels)
        self.assertEqual(value, original)

    def test_disabled_demo_and_pending_do_not_reveal_a_retained_provider_result(self):
        for changes, expected in (({"config": CONFIG | {"flightaware_enabled": False}}, "FLIGHT STATUS OFF"),
                                  ({"demo": True}, "DEMO / NO PROVIDER REQUESTS")):
            with self.subTest(changes=changes):
                labels, _ = capture(**changes)
                self.assertIn(expected, labels)
                self.assertNotIn("12:25 PM", labels)
                self.assertNotIn("2:50 PM", labels)
                self.assertNotIn("DL123", labels)
        for mode, expected in (("pending", "WAITING FOR FLIGHT STATUS"), ("not_found", "NO MATCHING PROVIDER FLIGHT"),
                               ("ambiguous", "FLIGHT MATCH AMBIGUOUS"), ("unavailable", "STATUS UPDATE UNAVAILABLE"),
                               ("unconfigured", "AEROAPI KEY NOT CONFIGURED")):
            with self.subTest(mode=mode):
                labels, _ = capture(status() | {"status": mode})
                self.assertIn(expected, labels)
                self.assertNotIn("2:50 PM", labels)
                self.assertNotIn("10 MIN LATE", labels)

    def test_provider_cancelled_tracking_flag_does_not_claim_airline_cancellation(self):
        labels, _ = capture(status(cancelled=True))
        self.assertIn("TRACKING ENDED / CHECK AIRLINE", labels)
        self.assertFalse(any("CANCEL" in value for value in labels))
        labels, _ = capture(status(diverted=True))
        self.assertIn("DIVERTED / CHECK AIRLINE", labels)
        labels, _ = capture(status(diverted=True, cancelled=True))
        self.assertIn("DIVERTED / TRACKING ENDED", labels)

    def test_invalid_or_inconsistent_deltas_and_event_bases_remain_unknown(self):
        for change in ({"delta_minutes": None}, {"delta_minutes": True}, {"delta_minutes": float("nan")},
                       {"delta_minutes": 0}, {"basis": "scheduled"}, {"event": "in"}, {"time": None}):
            with self.subTest(change=change):
                value = status()
                value["departure"].update(change)
                labels, _ = capture(value)
                self.assertIn("DELTA UNKNOWN", labels)
                self.assertNotIn("ON TIME", labels)

    def test_subminute_changes_keep_their_direction_and_am_pm_keeps_new_day(self):
        labels, _ = capture(status(actual_out="2026-11-01T18:15:30Z"))
        self.assertIn("<1 MIN LATE", labels)
        labels, _ = capture(status(actual_out="2026-11-01T18:14:30Z"))
        self.assertIn("<1 MIN EARLY", labels)
        value = status(estimated_in="2026-11-02T06:05:00Z")
        labels, _ = capture(value)
        self.assertIn("12:05 AM", labels)
        self.assertIn("Mon 11/02 / CST", labels)

    def test_future_fetched_time_is_stale_and_footer_reserves_deck_progress(self):
        labels, _ = capture(status() | {"fetched_at": (NOW + timedelta(seconds=1)).isoformat()})
        self.assertIn("PROVIDER UPDATE STALE", labels)
        self.assertNotIn("10 MIN LATE", labels)
        with patch.object(render, "_text", wraps=render._text) as text:
            render_operating(ImageDraw.Draw(Image.new("RGB", (512, 256))), None,
                             {"demo": False, "operating_status": status(), "deck_count": 16}, CONFIG, NOW)
        footer = next(call for call in text.call_args_list if call.args[1] == (15, 241))
        self.assertEqual(footer.kwargs["max_width"], 280)
