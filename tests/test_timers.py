"""Persistence, limits and retry safety for household countdowns."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flightwall.timers import TimerError, TimerStore


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.now = datetime(2026, 11, 1, 6, 59, 30, tzinfo=timezone.utc)
        self.clock = patch("flightwall.timers._now", side_effect=lambda: self.now)
        self.clock.start()
        self.store = TimerStore(self.root)

    def tearDown(self):
        self.clock.stop()
        self.directory.cleanup()

    def start(self, label="Pasta", duration=60, request="request-one"):
        return self.store.start({"label": label, "duration_seconds": duration, "source": "alexa", "request_id": request})

    def test_absolute_deadline_survives_restart_and_clock_change(self):
        timer = self.start(duration=120)["timer"]
        self.assertEqual(timer["ends_at"], "2026-11-01T07:01:30.000000Z")
        self.now += timedelta(seconds=61, microseconds=1)
        after = TimerStore(self.root).list_timers()[0]
        self.assertEqual(after["id"], timer["id"])
        self.assertEqual(after["ends_at"], timer["ends_at"])
        self.assertEqual(after["remaining_seconds"], 59)
        self.now += timedelta(seconds=59)
        self.assertEqual(TimerStore(self.root).list_timers()[0]["status"], "expired")
        self.assertEqual(TimerStore(self.root).list_timers()[0]["remaining_seconds"], 0)

    def test_expired_stays_done_until_dismissed(self):
        timer = self.start(duration=1)["timer"]
        self.now += timedelta(seconds=1)
        self.assertEqual(self.store.list_timers()[0]["status"], "expired")
        result = self.store.cancel(timer["id"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["timer"]["status"], "cancelled")
        self.assertEqual(self.store.list_timers(), [])
        self.assertFalse(self.store.cancel(timer["id"])["cancelled"])

    def test_five_running_limit_and_oldest_expired_eviction(self):
        timers = [self.start(str(i), i + 1, str(i))["timer"] for i in range(5)]
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(TimerError, "Five timers"):
            self.start("Sixth", 60, "sixth")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.now += timedelta(seconds=3)
        added = self.start("Sixth", 60, "sixth")
        self.assertEqual(added["replaced"], [timers[0]["id"]])
        self.assertEqual(len(self.store.list_timers()), 5)
        self.assertIn("expired", {timer["status"] for timer in self.store.list_timers()})

    def test_retries_never_extend_restart_or_restore_timer(self):
        timer = self.start()["timer"]
        self.now += timedelta(seconds=10)
        repeated = self.start()
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["timer"]["ends_at"], timer["ends_at"])
        self.assertEqual(repeated["timer"]["remaining_seconds"], 50)
        self.store.cancel(timer["id"])
        self.start("Tea", 60, "tea")  # Compacts the cancelled row.
        replay = self.start()
        self.assertFalse(replay["created"])
        self.assertTrue(replay["timer"]["cancelled"])
        self.assertEqual([row["label"] for row in self.store.list_timers()], ["Tea"])
        self.assertFalse(self.store.cancel(timer["id"])["cancelled"])

    def test_replay_of_evicted_expired_timer_does_not_restore_it(self):
        timers = {}
        for i in range(5):
            timer = self.start(str(i), 1, str(i))["timer"]
            timers[timer["id"]] = str(i)
        self.now += timedelta(seconds=2)
        replaced = self.start("New", 60, "new")["replaced"][0]
        ids = {row["id"] for row in self.store.list_timers()}
        self.assertNotIn(replaced, ids)
        replay = self.start(timers[replaced], 1, timers[replaced])
        self.assertFalse(replay["created"])
        self.assertEqual(replay["timer"]["status"], "expired")
        self.assertEqual({row["id"] for row in self.store.list_timers()}, ids)

    def test_conflicting_request_id_does_not_modify_storage(self):
        self.start()
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(TimerError, "different timer details"):
            self.start("Tea")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertNotIn("request-one", self.store.path.read_text())

    def test_invalid_payloads_fail_without_creating_store(self):
        good = {"label": "Pasta", "duration_seconds": 10}
        changes = [{"label": ""}, {"label": "a" * 81}, {"label": "a\nb"}, {"label": "a\u2028b"},
                   {"label": "a\ud800"}, {"duration_seconds": True}, {"duration_seconds": 0},
                   {"duration_seconds": 86401}, {"duration_seconds": 1.5}, {"duration_seconds": "5"},
                   {"request_id": "\n"}, {"request_id": None}, {"request_id": "x" * 513},
                   {"source": "phone"}, {"command": "anything"}]
        for change in changes:
            with self.subTest(change=repr(change)):
                with self.assertRaises(TimerError):
                    self.store.start({**good, **change})
                self.assertFalse(self.store.path.exists())
        self.assertEqual(self.start("a" * 80, 86400)["timer"]["duration_seconds"], 86400)

    def test_cancel_is_scoped_to_selected_id_and_rejects_invalid_id(self):
        first = self.start()["timer"]
        second = self.start("Tea", 60, "second")["timer"]
        self.store.cancel(first["id"])
        self.assertEqual(self.store.list_timers()[0]["id"], second["id"])
        for item_id in ("../../config.json", "", "f" * 24):
            with self.assertRaises(TimerError):
                self.store.cancel(item_id)
        self.assertEqual(len(self.store.list_timers()), 1)

    def test_corruption_is_not_overwritten_and_error_does_not_echo_private_data(self):
        self.store.path.write_text('{"private":"secret sentinel"')
        before = self.store.path.read_bytes()
        for action in (self.store.list_timers, self.start, lambda: self.store.cancel("a" * 24)):
            with self.assertRaises(TimerError) as error:
                action()
            self.assertNotIn("sentinel", str(error.exception))
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_persisted_deadline_fails_closed(self):
        self.start()
        state = json.loads(self.store.path.read_text())
        state["timers"][0]["ends_at"] = "2026-11-01T07:05:00Z"
        self.store.path.write_text(json.dumps(state))
        before = self.store.path.read_bytes()
        with self.assertRaises(TimerError):
            self.store.list_timers()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_atomic_write_failure_preserves_previous_store(self):
        self.start()
        before = self.store.path.read_bytes()
        with patch("flightwall.personal.os.replace", side_effect=OSError("private disk detail")):
            with self.assertRaises(TimerError) as error:
                self.start("Tea", 60, "new")
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_concurrent_stores_cannot_exceed_five_or_duplicate_retries(self):
        def start(index):
            try:
                return TimerStore(self.root).start({"label": "Timer", "duration_seconds": 60, "request_id": str(index)})
            except TimerError:
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(start, range(8)))
        self.assertEqual(sum(result is not None for result in results), 5)
        self.assertEqual(len(self.store.list_timers()), 5)
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(start, [next(i for i, row in enumerate(results) if row)] * 6))
        self.assertTrue(all(result and not result["created"] for result in results))
        self.assertEqual(len(self.store.list_timers()), 5)

    def test_two_processes_share_a_single_retry(self):
        code = ("from flightwall.timers import TimerStore; import sys; "
                "TimerStore(sys.argv[1]).start({'label':'Pasta','duration_seconds':60,'request_id':'process-retry'})")
        processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(self.root)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for process in processes:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error.decode(errors="replace"))
            self.assertEqual(output, b"")
        self.assertEqual(len(self.store.list_timers()), 1)

    def test_request_capacity_and_ttl_are_bounded(self):
        with patch("flightwall.timers.MAX_REQUESTS", 2):
            self.start(request="one")
            self.start(request="two")
            with self.assertRaisesRegex(TimerError, "history is full"):
                self.start(request="three")
            self.now += timedelta(days=31)
            self.assertTrue(self.start(request="three")["created"])
            self.assertEqual(len(json.loads(self.store.path.read_text())["requests"]), 1)

    @unittest.skipIf(os.name == "nt", "POSIX permissions are validated on Linux")
    def test_private_posix_modes(self):
        self.start()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.lock_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
