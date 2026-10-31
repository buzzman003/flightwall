"""Synthetic public-flight requests; never call a real paid provider."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds
from flightwall.flight_watches import FlightWatchError, FlightWatchStore, day_bounds, tracking_available


NOW = datetime(2026, 10, 31, 15, tzinfo=timezone.utc)
CONFIG = DEFAULTS | {"timezone": "America/Chicago", "demo": False, "flightaware_enabled": True}


def request(**changes):
    return {"airline": "Delta", "flight_number": "123", "date": "2026-10-31", "source": "alexa",
            "request_id": "synthetic-watch-request", **changes}


def flight(**changes):
    return {"ident": "DAL123", "ident_iata": "DL123", "ident_icao": "DAL123", "fa_flight_id": "opaque-flight-123",
            "origin": {"code_iata": "ATL", "unneeded": "discard"}, "destination": {"code_iata": "MCO"},
            "scheduled_out": NOW.isoformat(), "scheduled_in": (NOW + timedelta(hours=2)).isoformat(),
            "unneeded_private_payload": "must never escape", **changes}


def position(**changes):
    return {"last_position": {"latitude": 33.6, "longitude": -84.4, "timestamp": NOW.isoformat(),
                              "altitude": 330, "groundspeed": 440, "heading": 120, **changes}}


class WatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = copy.deepcopy(CONFIG)
        self.store = FlightWatchStore(self.directory, lambda: self.config)
        self.clock = patch("flightwall.flight_watches._now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_request_is_private_separate_bounded_and_deduplicated(self):
        result = self.store.create(request())
        self.assertTrue(result["created"])
        self.assertFalse(result["tracking_available"])
        self.assertEqual(result["watch"]["ident"], "DL123")
        self.assertEqual(result["watch"]["timezone"], "America/Chicago")
        self.assertFalse((self.directory / "personal.json").exists())
        self.assertFalse((self.directory / "calendar-cache.json").exists())
        self.assertNotIn("synthetic-watch-request", self.store.path.read_text())
        again = self.store.create(request())
        distinct_request = self.store.create(request(request_id="second-request"))
        self.assertFalse(again["created"])
        self.assertFalse(distinct_request["created"])
        self.assertEqual(again["watch"], result["watch"])
        self.assertEqual(len(self.store.list_watches()), 1)
        self.assertEqual(FlightWatchStore(self.directory, lambda: self.config).list_watches(), self.store.list_watches())

    def test_airline_aliases_do_not_confuse_allegiant_with_air_arabia(self):
        for name, code in (("Allegiant", "G4"), ("G9", "G9"), ("Frontier", "F9"), ("American", "AA"), ("United", "UA")):
            saved = self.store.create(request(airline=name, request_id=name))
            self.assertEqual(saved["watch"]["ident"], code + "123")
        with self.assertRaises(FlightWatchError):
            self.store.create(request())

    def test_capacity_and_expiry_never_remove_future_requests(self):
        for day in range(31, 36):
            self.store.create(request(flight_number=str(day), request_id=str(day)))
        with self.assertRaises(FlightWatchError):
            self.store.create(request())
        tomorrow = NOW + timedelta(days=1)
        self.assertEqual(self.store.list_watches(tomorrow), [])
        with patch("flightwall.flight_watches._now", return_value=tomorrow):
            self.store.create(request(date="2026-11-01", request_id="next-day"))
        self.assertEqual(len(self.store.list_watches(tomorrow)), 1)

    def test_expired_retry_does_not_restore_request_or_change_timezone(self):
        result = self.store.create(request())
        self.config["timezone"] = "Pacific/Honolulu"
        with patch("flightwall.flight_watches._now", return_value=NOW + timedelta(days=2)):
            again = self.store.create(request())
            self.assertEqual(again["watch"], result["watch"])
            self.assertFalse(again["created"])
            self.assertEqual(self.store.list_watches(), [])

    def test_changed_retry_is_rejected_without_overwrite(self):
        self.store.create(request())
        before = self.store.path.read_bytes()
        with self.assertRaises(FlightWatchError):
            self.store.create(request(flight_number="124"))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_dates_require_current_or_next_fourteen_local_days(self):
        for value in ("2026-10-30", "2026-11-15", "2026-02-30", "2026-10", True):
            with self.subTest(value=value), self.assertRaises(FlightWatchError):
                self.store.create(request(date=value))
        self.store.create(request(date="2026-11-14"))

    def test_bad_fields_never_create_data_file(self):
        for values in ({"airline": "unknown airline"}, {"airline": "99"}, {"airline": "DL/evil"},
                       {"flight_number": 123}, {"flight_number": "0"}, {"flight_number": "12345"},
                       {"flight_number": "١٢٣"}, {"source": "roster"}, {"request_id": ""},
                       {"request_id": "a\nb"}, {"request_id": "a" * 513}, {"description": "do not store"}):
            with self.subTest(values=values), self.assertRaises(FlightWatchError):
                self.store.create(request(**values))
        self.assertFalse(self.store.path.exists())

    def test_provider_readiness_never_calls_network_or_means_live(self):
        self.assertFalse(tracking_available(self.directory, self.config))
        key = self.directory / "flightaware.key"
        key.write_text("synthetic-test-key")
        with patch("flightwall.feeds.request_json") as network:
            result = self.store.create(request())
            self.assertTrue(result["tracking_available"])
            network.assert_not_called()
        self.assertFalse(tracking_available(self.directory, self.config | {"demo": True}))
        self.assertFalse(tracking_available(self.directory, self.config | {"flightaware_enabled": False}))
        key.write_text(" " * 10)
        self.assertFalse(tracking_available(self.directory, self.config))

    def test_day_bounds_are_utc_and_account_for_both_dst_transitions(self):
        for day, hours in (("2026-03-08", 23), ("2026-11-01", 25), ("2026-10-31", 24)):
            lower, upper = day_bounds({"date": day, "timezone": "America/Chicago"})
            self.assertEqual(lower.utcoffset(), timedelta(0))
            self.assertEqual((upper - lower).total_seconds(), hours * 3600)

    def test_corruption_fails_closed_and_never_echoes_private_contents(self):
        self.store.path.write_text('{"private": "synthetic secret"}')
        before = self.store.path.read_bytes()
        for method in (self.store.list_watches, lambda: self.store.create(request())):
            with self.assertRaises(FlightWatchError) as caught:
                method()
            self.assertNotIn("synthetic secret", str(caught.exception))
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_failed_atomic_write_preserves_previous_requests(self):
        self.store.create(request())
        before = self.store.path.read_bytes()
        with patch("flightwall.personal.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(FlightWatchError):
                self.store.create(request(flight_number="124", request_id="second"))
        self.assertEqual(before, self.store.path.read_bytes())


class WatchFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / "flightaware.key").write_text("synthetic-provider-key")
        self.config = copy.deepcopy(CONFIG)
        with patch("flightwall.flight_watches._now", return_value=NOW):
            self.watch = FlightWatchStore(self.directory, lambda: self.config).create(request())["watch"]
        self.feed = FlightFeeds(self.directory)
        self.clock = patch("flightwall.feeds.utcnow", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.tick = patch("flightwall.feeds.time.monotonic", return_value=100)
        self.tick_mock = self.tick.start()
        self.addCleanup(self.tick.stop)

    def poll(self, response, watches=None, config=None):
        with patch.object(self.feed, "_flightaware", return_value=response) as call:
            self.feed.poll(config or self.config, None, watches=watches or [self.watch], on_duty=True)
        return call

    def result(self):
        return self.feed.watch_results[self.watch["id"]]

    def resolve(self, record=None):
        return self.poll({"flights": [record or flight()]})

    def test_exact_day_designator_query_and_only_sanitized_projection(self):
        call = self.resolve()
        self.assertEqual(call.call_count, 1)
        query = parse_qs(urlsplit(call.call_args.args[0]).query)
        self.assertEqual(query, {"start": ["2026-10-31T05:00:00+00:00"], "end": ["2026-11-01T05:00:00+00:00"],
                                 "ident_type": ["designator"], "max_pages": ["1"]})
        self.assertTrue(call.call_args.args[0].startswith("/flights/DL123?"))
        self.assertEqual(self.result()["status"], "scheduled")
        self.assertNotIn("must never escape", json.dumps(self.feed.watch_results))
        self.assertEqual(set(self.result()["flight"]), {"id", "number", "origin", "destination", "start", "end", "deadhead", "source"})
        self.assertIsNone(self.feed.live)

    def test_matching_is_end_exclusive_and_wrong_airline_never_attaches(self):
        values = [flight(ident="AAL123", ident_iata="AA123", ident_icao="AAL123"),
                  flight(scheduled_out="2026-11-01T05:00:00Z"), flight(scheduled_out="2026-10-31T04:59:59Z")]
        for item in values:
            self.feed = FlightFeeds(self.directory)
            self.resolve(item)
            self.assertEqual(self.result(), {"status": "not_found"})

    def test_multiple_legs_diversions_and_more_pages_are_ambiguous(self):
        for response in ({"flights": [flight(), flight(fa_flight_id="different-leg")]},
                         {"flights": [flight(), flight(destination={"code_iata": "JAX"})]},
                         {"flights": [flight()], "links": {"next": "https://untrusted.invalid/page"}}):
            self.feed = FlightFeeds(self.directory)
            call = self.poll(response)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(self.result(), {"status": "ambiguous"})

    def test_no_configuration_and_demo_do_not_call_any_optional_provider(self):
        for config in (self.config | {"demo": True}, self.config | {"flightaware_enabled": False}):
            call = self.poll({}, config=config)
            call.assert_not_called()
            self.assertEqual(self.result(), {"status": "unconfigured"})
        (self.directory / "flightaware.key").unlink()
        self.poll({}).assert_not_called()

    def test_demo_never_calls_nearby_even_when_configured(self):
        with patch("flightwall.feeds.request_json") as network:
            self.feed.poll(self.config | {"demo": True, "nearby_enabled": True,
                                          "home_latitude": 33, "home_longitude": -84}, None, watches=[self.watch])
            network.assert_not_called()
        self.assertEqual(self.feed.nearby, [])
        self.assertIsNone(self.feed.live)

    def test_far_future_request_waits_without_charging_until_supported_window(self):
        watch = self.watch | {"date": "2026-11-10"}
        self.poll({}, watches=[watch]).assert_not_called()
        self.assertEqual(self.result(), {"status": "pending"})

    def test_two_step_lookup_position_is_fresh_associated_and_unit_safe(self):
        self.resolve()
        call = self.poll(position())
        self.assertEqual(call.call_args.args[0], "/flights/opaque-flight-123/position")
        result = self.result()
        self.assertEqual(result["status"], "tracking")
        self.assertEqual(result["live"]["flight_id"], result["flight"]["id"])
        self.assertEqual(result["live"]["altitude"], 33000)
        self.assertEqual(result["live"]["altitude_units"], "feet")
        self.poll({}).assert_not_called()

    def test_stale_future_or_invalid_position_never_claims_tracking(self):
        for changes in ({"timestamp": (NOW - timedelta(seconds=301)).isoformat()},
                        {"timestamp": (NOW + timedelta(seconds=1)).isoformat()},
                        {"latitude": True}, {"longitude": float("nan")}, {"latitude": 91}):
            self.feed = FlightFeeds(self.directory)
            self.resolve()
            self.poll(position(**changes))
            self.assertEqual(self.result()["status"], "scheduled")
            self.assertNotIn("live", self.result())
        self.feed = FlightFeeds(self.directory)
        self.resolve()
        self.poll(position() | {"fa_flight_id": "wrong"})
        self.assertNotIn("live", self.result())

    def test_cached_position_expires_even_when_poll_throttled(self):
        self.resolve()
        self.poll(position())
        with patch("flightwall.feeds.utcnow", return_value=NOW + timedelta(seconds=301)):
            self.poll({}).assert_not_called()
        self.assertNotIn("live", self.result())
        self.assertEqual(self.result()["status"], "scheduled")

    def test_provider_failure_is_generic_and_does_not_disclose_contents(self):
        stream = io.StringIO()
        with patch.object(self.feed, "_flightaware", side_effect=OSError("secret-provider-payload")), patch("sys.stdout", stream), patch("sys.stderr", stream):
            self.feed.poll(self.config, None, watches=[self.watch])
        self.assertEqual(self.result(), {"status": "unavailable"})
        self.assertEqual(stream.getvalue(), "")
        self.assertNotIn("secret", json.dumps(self.feed.watch_results))

    def test_removed_and_mutated_identity_never_keep_another_flights_position(self):
        self.resolve()
        self.poll(position())
        new = self.watch | {"airline": "AA", "ident": "AA123"}
        self.poll({"flights": []}, watches=[new])
        self.assertEqual(self.result(), {"status": "not_found"})
        self.feed.poll(self.config, None, watches=[])
        self.assertEqual(self.feed.watch_results, {})
        self.assertEqual(self.feed._watch_cache, {})

    def test_one_extra_request_per_pass_round_robin_and_lookup_backoff(self):
        second = self.watch | {"id": "b" * 24, "flight_number": "124", "ident": "DL124"}
        self.poll({"flights": []}, watches=[self.watch, second])
        call = self.poll({"flights": []}, watches=[self.watch, second])
        self.assertEqual(call.call_count, 1)
        self.assertIn("/flights/DL124?", call.call_args.args[0])
        self.poll({}, watches=[self.watch, second]).assert_not_called()
        self.tick_mock.return_value += 301
        self.assertEqual(self.poll({"flights": []}, watches=[self.watch, second]).call_count, 1)

    def test_operating_requests_use_shared_daily_budget_first(self):
        chosen = {"id": "operating-id", "number": "UA999", "origin": "ATL", "destination": "MCO",
                  "start": NOW.isoformat(), "end": (NOW + timedelta(hours=2)).isoformat()}
        replies = [{"flights": [flight(ident="UAL999", ident_iata="UA999")]}, position()]
        with patch("flightwall.feeds.request_json", side_effect=replies) as network:
            self.feed.poll(self.config | {"flightaware_daily_limit": 2}, chosen, watches=[self.watch], on_duty=True)
        self.assertEqual(network.call_count, 2)
        self.assertIn("/flights/UA999?", network.call_args_list[0].args[0])
        self.assertEqual(self.feed.live["flight_id"], "operating-id")
        self.assertEqual(self.result(), {"status": "unavailable"})
        usage = json.loads((self.directory / "flightaware-usage.json").read_text())
        self.assertEqual(usage["count"], 2)

    def test_malformed_blocked_and_terminated_provider_results_are_unavailable(self):
        for item in (flight(blocked=True), flight(cancelled=True), flight(scheduled_in=None),
                     flight(fa_flight_id="../../private"), flight(origin={"code_iata": "BAD-CODE"})):
            self.feed = FlightFeeds(self.directory)
            self.resolve(item)
            self.assertEqual(self.result(), {"status": "unavailable"})


if __name__ == "__main__":
    unittest.main()
