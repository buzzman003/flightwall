"""Fictional dinner cards obey the current TV-local snack cutoff each frame."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flightwall import render


class DinnerSnackTests(unittest.TestCase):
    def setUp(self):
        self.config = {"timezone": "America/Chicago", "demo": False}

    def frame(self, stamp, *, kind="dinner", backend_error=False):
        now = datetime.fromisoformat(stamp)
        zone = ZoneInfo(self.config["timezone"])
        local = now.astimezone(zone)
        start = local.replace(hour=14, minute=30, second=0, microsecond=0)
        end = local.replace(hour=19, minute=0, second=0, microsecond=0)
        item = {"id": "fictional-dinner", "kind": kind, "title": "Chicken and rice",
                "start": start.isoformat(), "end": end.isoformat(),
                "display_start": start.isoformat(), "display_end": end.isoformat(),
                # A stale card zone must not move the household's 17:00 cutoff.
                "timezone": "Pacific/Honolulu", "source": "Manual"}
        state = {"view": "personal", "now": stamp, "personal_items": [item],
                 "selected_personal_item": item, "backend_error": backend_error}
        before = deepcopy(state)
        labels = []
        original = render._text

        def capture(draw, xy, value, *args, **kwargs):
            labels.append(str(value))
            return original(draw, xy, value, *args, **kwargs)

        with patch.object(render, "_text", side_effect=capture):
            image = render.render_frame(state, self.config)
        self.assertEqual(state, before)
        return image, labels

    def test_exact_five_pm_cutoff_in_central_summer_and_winter(self):
        for date in ("2026-10-22", "2026-11-01", "2026-03-08"):
            boundary = datetime.fromisoformat(date + "T17:00:00").replace(tzinfo=ZoneInfo("America/Chicago"))
            before, labels = self.frame((boundary - timedelta(microseconds=1)).astimezone(timezone.utc).isoformat())
            self.assertIn("SNACKS OK", labels)
            self.assertNotIn("NO MORE SNACKS", labels)
            self.assertEqual(before.getpixel((25, 171)), render.GREEN)
            after, labels = self.frame(boundary.astimezone(timezone.utc).isoformat(), backend_error=True)
            self.assertIn("NO MORE SNACKS", labels)
            self.assertNotIn("SNACKS OK", labels)
            self.assertEqual(after.getpixel((25, 171)), (244, 92, 92))

    def test_visibility_still_starts_at_two_thirty_and_ends_at_seven(self):
        for stamp, expected in (("2026-10-22T14:29:59-05:00", None),
                                ("2026-10-22T14:30:00-05:00", "SNACKS OK"),
                                ("2026-10-22T18:59:59-05:00", "NO MORE SNACKS"),
                                ("2026-10-22T19:00:00-05:00", None)):
            _, labels = self.frame(stamp, backend_error=True)
            snack_labels = [value for value in labels if value in {"SNACKS OK", "NO MORE SNACKS"}]
            self.assertEqual(snack_labels, [expected] if expected else [])

    def test_appointments_never_receive_the_snack_rule(self):
        _, labels = self.frame("2026-10-22T17:30:00-05:00", kind="appointment")
        self.assertNotIn("SNACKS OK", labels)
        self.assertNotIn("NO MORE SNACKS", labels)

    def test_rule_uses_configured_zone_not_utc_or_card_timezone(self):
        # 22:30 UTC is 17:30 Chicago, but 16:30 Denver.
        _, labels = self.frame("2026-10-22T22:30:00+00:00")
        self.assertIn("NO MORE SNACKS", labels)
        self.config["timezone"] = "America/Denver"
        _, labels = self.frame("2026-10-22T22:30:00+00:00")
        self.assertIn("SNACKS OK", labels)


if __name__ == "__main__":
    unittest.main()
