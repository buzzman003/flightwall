"""Personal-card boundaries, persistence, voice retry safety, and process locking."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flightwall.personal import PersonalError, PersonalStore


UTC = timezone.utc


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {"timezone": "America/New_York", "personal_enabled": True,
                       "appointment_duration_minutes": 60, "dinner_start": "14:30", "dinner_end": "19:00"}
        self.store = PersonalStore(self.root, lambda: dict(self.config))
        self.now = dt("2026-10-22T16:00:00Z")  # Noon in New York.
        self.clock = patch("flightwall.personal._now", side_effect=lambda: self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.directory.cleanup()

    @staticmethod
    def appointment(**changes):
        return {"kind": "appointment", "title": "Dentist", "date": "2026-10-23", "time": "09:00", **changes}

    def test_appointment_day_visibility_ends_exactly_at_event_end(self):
        result = self.store.create(self.appointment())
        item = result["item"]
        self.assertTrue(result["created"])
        self.assertEqual(dt(item["start"]), dt("2026-10-23T13:00:00Z"))
        self.assertEqual(dt(item["end"]), dt("2026-10-23T14:00:00Z"))
        self.assertEqual(dt(item["display_start"]), dt("2026-10-23T04:00:00Z"))
        self.assertEqual(dt(item["display_end"]), dt("2026-10-23T14:00:00Z"))
        self.assertEqual(self.store.visible(dt("2026-10-23T03:59:59.999999Z")), [])
        self.assertEqual([card["id"] for card in self.store.visible(dt("2026-10-23T04:00:00Z"))], [item["id"]])
        self.assertEqual(len(self.store.visible(dt("2026-10-23T13:59:59.999999Z"))), 1)
        self.assertEqual(self.store.visible(dt("2026-10-23T14:00:00Z")), [])
        self.assertIn("until 10:00, when the appointment ends", result["speech"])

    def test_dinner_uses_today_in_board_timezone_and_configured_window(self):
        item = self.store.create({"kind": "dinner", "title": "Tacos"})["item"]
        self.assertEqual(item["timezone"], "America/New_York")
        self.assertEqual(dt(item["start"]), dt("2026-10-22T18:30:00Z"))
        self.assertEqual(dt(item["end"]), dt("2026-10-22T23:00:00Z"))
        self.assertEqual(self.store.visible(dt("2026-10-22T18:29:59.999999Z")), [])
        self.assertEqual(len(self.store.visible(dt("2026-10-22T18:30:00Z"))), 1)
        self.assertEqual(len(self.store.visible(dt("2026-10-22T22:59:59.999999Z"))), 1)
        self.assertEqual(self.store.visible(dt("2026-10-22T23:00:00Z")), [])
        self.assertEqual(len(self.store.list_items()), 1)  # Includes future display windows.

    def test_dinner_after_window_is_saved_for_today_without_silent_tomorrow(self):
        self.now = dt("2026-10-23T01:00:00Z")  # Still Oct22,21:00 in NewYork.
        result = self.store.create({"kind": "dinner", "title": "Pasta", "request_id": "late-dinner"})
        self.assertTrue(result["created"])
        self.assertEqual(dt(result["item"]["start"]).astimezone(ZoneInfo("America/New_York")).date().isoformat(), "2026-10-22")
        self.assertIn("has ended", result["speech"])
        self.assertIn("will not appear", result["speech"])
        self.assertEqual(self.store.visible(), [])

    def test_new_dinner_replaces_same_date_meal_without_resurrecting_old_retry(self):
        old_payload = {"kind": "dinner", "title": "Chicken", "request_id": "meal-one"}
        old = self.store.create(old_payload)["item"]
        self.store.create(self.appointment(title="Unrelated appointment"))
        new = self.store.create({"kind": "dinner", "title": "Pizza", "request_id": "meal-two"})
        self.assertEqual(new["replaced"], [old["id"]])
        visible = self.store.visible(dt("2026-10-22T20:00:00Z"))
        self.assertEqual([item["title"] for item in visible], ["Pizza"])
        self.assertEqual(len(self.store.list_items()), 2)  # Pizza and appointment.
        replay = self.store.create(old_payload)
        self.assertFalse(replay["created"])
        self.assertTrue(replay["item"]["cancelled"])
        self.assertEqual(replay["item"]["id"], old["id"])
        self.assertEqual([item["title"] for item in self.store.visible(dt("2026-10-22T20:00:00Z"))], ["Pizza"])

    def test_different_date_dinners_are_preserved(self):
        self.store.create({"kind": "dinner", "title": "Chicken", "date": "2026-10-22"})
        self.store.create({"kind": "dinner", "title": "Pizza", "date": "2026-10-23"})
        self.assertEqual([item["title"] for item in self.store.list_items()], ["Chicken", "Pizza"])

    def test_elapsed_duration_across_spring_forward_and_overnight(self):
        self.now = dt("2026-03-07T12:00:00Z")
        item = self.store.create(self.appointment(date="2026-03-08", time="01:30", duration_minutes=120))["item"]
        self.assertEqual(dt(item["end"]) - dt(item["start"]), timedelta(minutes=120))
        self.assertEqual(dt(item["end"]).astimezone(ZoneInfo("America/New_York")).strftime("%H:%M"), "04:30")
        overnight = self.store.create(self.appointment(date="2026-03-09", time="23:30", duration_minutes=120))["item"]
        self.assertEqual(dt(overnight["end"]).astimezone(ZoneInfo("America/New_York")).date().isoformat(), "2026-03-10")

    def test_dst_gap_and_fold_are_rejected_with_clarification(self):
        self.now = dt("2026-01-01T00:00:00Z")
        for zone, day, clock, expected in [
            ("America/New_York", "2026-03-08", "02:30", "does not exist"),
            ("America/New_York", "2026-11-01", "01:30", "occurs twice"),
            ("Australia/Lord_Howe", "2026-10-04", "02:15", "does not exist"),
            ("Australia/Lord_Howe", "2026-04-05", "01:45", "occurs twice"),
        ]:
            with self.subTest(zone=zone, day=day):
                self.config["timezone"] = zone
                with self.assertRaisesRegex(PersonalError, expected):
                    self.store.create(self.appointment(date=day, time=clock))
        self.assertFalse(self.store.path.exists())

    def test_ambiguous_local_midnight_display_boundary_is_not_guessed(self):
        self.config["timezone"] = "America/Havana"
        self.now = dt("2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(PersonalError, "occurs twice"):
            self.store.create(self.appointment(date="2026-11-01", time="12:00"))

    def test_same_request_retries_after_midnight_and_config_change_keep_original(self):
        payload = {"kind": "dinner", "title": "Tacos", "request_id": "request-for-dinner", "source": "alexa"}
        original = self.store.create(payload)
        self.now += timedelta(days=1)
        self.config.update({"timezone": "Pacific/Auckland", "dinner_start": "16:00", "dinner_end": "20:00"})
        duplicate = PersonalStore(self.root, lambda: dict(self.config)).create(payload)
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["item"], original["item"])
        self.assertEqual(self.store.list_items(), [])

    def test_omitted_duration_retry_is_stable_after_default_change_and_expiration(self):
        payload = self.appointment(request_id="appointment-retry")
        original = self.store.create(payload)["item"]
        self.config["appointment_duration_minutes"] = 120
        self.now += timedelta(days=3)
        # Old appointment ended, but its30-day retry record must win before
        # current-time validation or applying the new default duration.
        result = self.store.create(payload)
        self.assertFalse(result["created"])
        self.assertEqual(result["item"], original)

    def test_request_payload_conflict_preserves_original_file_and_input(self):
        payload = self.appointment(request_id="stable-id")
        original_payload = copy.deepcopy(payload)
        self.store.create(payload)
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(PersonalError, "different card details"):
            self.store.create({**payload, "title": "Different appointment"})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(payload, original_payload)

    def test_cancel_is_persistent_idempotent_and_retry_cannot_resurrect(self):
        payload = self.appointment(request_id="cancelled-request")
        item = self.store.create(payload)["item"]
        self.assertTrue(self.store.cancel(item["id"])["cancelled"])
        self.assertFalse(self.store.cancel(item["id"])["cancelled"])
        fresh = PersonalStore(self.root, lambda: dict(self.config))
        self.assertEqual(fresh.list_items(), [])
        self.assertEqual(fresh.visible(dt("2026-10-23T13:00:00Z")), [])
        self.assertTrue(fresh.list_items(include_cancelled=True)[0]["cancelled"])
        replay = fresh.create(payload)
        self.assertFalse(replay["created"])
        self.assertTrue(replay["item"]["cancelled"])
        self.assertIn("cancelled", replay["speech"])

    def test_display_disabled_does_not_prevent_creation_or_listing(self):
        self.config["personal_enabled"] = False
        self.assertTrue(self.store.create(self.appointment())["created"])
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertEqual(self.store.visible(dt("2026-10-23T13:00:00Z")), [])
        self.config["personal_enabled"] = True
        self.assertEqual(len(self.store.visible(dt("2026-10-23T13:00:00Z"))), 1)

    def test_past_appointment_rejected_after_actual_event_end(self):
        self.now = dt("2026-10-23T14:01:00Z")
        with self.assertRaisesRegex(PersonalError, "already ended"):
            self.store.create(self.appointment())
        self.assertFalse(self.store.path.exists())

    def test_out_of_range_end_is_an_actionable_validation_error(self):
        self.config["timezone"] = "UTC"
        with self.assertRaisesRegex(PersonalError, "supported time range"):
            self.store.create(self.appointment(date="9999-12-31", time="23:59", duration_minutes=120))
        self.assertFalse(self.store.path.exists())

    def test_invalid_payloads_do_not_change_existing_data(self):
        self.store.create(self.appointment())
        original = self.store.path.read_bytes()
        bad = [[], {}, self.appointment(kind=[]), self.appointment(title=""), self.appointment(title="x" * 161),
               self.appointment(title="line\nbreak"), self.appointment(title="line\u2028break"), self.appointment(date="2026-02-30"),
               self.appointment(date="20261023"), self.appointment(time="9:00"), self.appointment(time="25:00"),
               self.appointment(duration_minutes=True), self.appointment(duration_minutes=1.5), self.appointment(duration_minutes=float("nan")),
               self.appointment(duration_minutes=0), self.appointment(duration_minutes=1441), self.appointment(source="injected-script"),
               self.appointment(request_id=""), self.appointment(request_id="x" * 513), self.appointment(request_id="\ud800"),
               self.appointment(unexpected="field"), {"kind": "dinner", "title": "Tacos", "time": "15:00"}]
        for payload in bad:
            with self.subTest(payload=repr(payload)[:80]):
                with self.assertRaises(PersonalError):
                    self.store.create(payload)
                self.assertEqual(self.store.path.read_bytes(), original)

    def test_voice_context_contains_only_public_scheduling_defaults(self):
        self.config["calendar_id"] = "private-calendar-id"
        self.store.create(self.appointment(title="Private appointment"))
        self.assertEqual(self.store.voice_context(), {"timezone": "America/New_York", "appointment_duration_minutes": 60,
                                                     "dinner_start": "14:30", "dinner_end": "19:00",
                                                     "dinner_tomorrow_preview": True, "dinner_requires_kids_home": False})

    def test_later_config_edits_do_not_move_existing_card_boundaries(self):
        original = self.store.create(self.appointment())["item"]
        self.config.update({"timezone": "America/Los_Angeles", "appointment_duration_minutes": 120})
        self.assertEqual(self.store.list_items()[0], original)

    def test_permissions_and_opaque_request_keys_and_finite_json(self):
        request_id = "Alexa-private-unique-request-id"
        self.store.create(self.appointment(request_id=request_id))
        for path in (self.store.path, self.store.lock_path):
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        raw = self.store.path.read_text()
        self.assertNotIn(request_id, raw)
        self.assertNotIn("NaN", raw)
        self.assertEqual(json.loads(raw)["schema_version"], 1)

    def test_capacity_never_evicts_future_cards_or_live_retry_records(self):
        with patch("flightwall.personal.MAX_ITEMS", 2):
            self.store.create(self.appointment(title="First"))
            self.store.create(self.appointment(title="Second", date="2026-12-01"))
            before = self.store.path.read_bytes()
            with self.assertRaisesRegex(PersonalError, "unexpired personal cards"):
                self.store.create(self.appointment(title="Third"))
            self.assertEqual(before, self.store.path.read_bytes())
        # Expired request records can be dropped, but live ones cannot be evicted.
        other = PersonalStore(self.root / "ledger", lambda: dict(self.config))
        with patch("flightwall.personal.MAX_REQUESTS", 2):
            for index in range(2):
                other.create({"kind": "dinner", "title": "Tacos", "request_id": str(index)})
            self.now += timedelta(days=1)
            before = other.path.read_bytes()
            with self.assertRaisesRegex(PersonalError, "request history is full"):
                other.create({"kind": "dinner", "title": "Tacos", "request_id": "third"})
            self.assertEqual(before, other.path.read_bytes())
            self.now += timedelta(days=29)
            self.assertTrue(other.create({"kind": "dinner", "title": "Tacos", "request_id": "third"})["created"])
            self.assertEqual(len(json.loads(other.path.read_text())["requests"]), 1)

    def test_replacement_and_cancellation_release_capacity_without_losing_retry_state(self):
        with patch("flightwall.personal.MAX_ITEMS", 1):
            old_payload = {"kind": "dinner", "title": "Chicken", "request_id": "one"}
            old = self.store.create(old_payload)["item"]
            new = self.store.create({"kind": "dinner", "title": "Pizza", "request_id": "two"})
            self.assertEqual(new["replaced"], [old["id"]])
            self.assertEqual(len(json.loads(self.store.path.read_text())["items"]), 1)
            replay = self.store.create(old_payload)
            self.assertFalse(replay["created"])
            self.assertTrue(replay["item"]["cancelled"])
            self.store.cancel(new["item"]["id"])
            self.assertTrue(self.store.create(self.appointment())["created"])
            self.assertEqual(len(self.store.list_items()), 1)
            self.assertEqual(len(json.loads(self.store.path.read_text())["items"]), 1)

    def test_expired_cards_pruned_but_request_snapshot_still_deduplicates(self):
        payload = {"kind": "dinner", "title": "Tacos", "request_id": "old-dinner"}
        old = self.store.create(payload)["item"]
        self.now += timedelta(days=2)
        self.store.create({"kind": "dinner", "title": "Pasta"})
        self.assertNotIn(old["id"], [item["id"] for item in self.store.list_items()])
        replay = self.store.create(payload)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["item"], old)

    def test_corrupt_store_fails_closed_without_replacing_private_data(self):
        self.store.path.write_text('{"schema_version":1,"items":NaN,"requests":{}}')
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(PersonalError, "could not be read"):
            self.store.create(self.appointment())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_storage_permission_failure_is_safe_personal_error(self):
        with patch("flightwall.personal.os.open", side_effect=PermissionError("private path and system details")):
            with self.assertRaisesRegex(PersonalError, "storage is unavailable") as caught:
                self.store.visible()
        self.assertNotIn("private path", str(caught.exception))

    def test_concurrent_store_instances_create_one_card_for_one_request(self):
        barrier = threading.Barrier(6)
        payload = self.appointment(request_id="same-concurrent-request")
        def create(_index):
            store = PersonalStore(self.root, lambda: dict(self.config))
            barrier.wait(timeout=3)
            return store.create(payload)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(create, range(6)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["item"]["id"] for result in results}), 1)
        self.assertEqual(len(self.store.list_items()), 1)

    @unittest.skipIf(sys.platform == "win32", "Batocera process locking test uses Linux/macOS flock")
    def test_separate_processes_share_the_same_atomic_request_ledger(self):
        # Each child delays its write while holding the stable lock, ensuring
        # overlapping processes exercise flock rather than just sequential I/O.
        script = '''
import json,sys,time
from flightwall.personal import PersonalStore
store=PersonalStore(sys.argv[1],lambda:{"timezone":"UTC"})
write=store._write
def slow_write(state):
    time.sleep(0.04)
    write(state)
store._write=slow_write
result=store.create({"kind":"appointment","title":"Process test","date":"2099-01-01","time":"12:00","request_id":"one-process-request"})
print(json.dumps({"created":result["created"],"id":result["item"]["id"]}))
'''
        directory = self.root / "processes"
        processes = [subprocess.Popen([sys.executable, "-c", script, str(directory)], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[1]) for _ in range(4)]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["id"] for result in results}), 1)


if __name__ == "__main__":
    unittest.main()
