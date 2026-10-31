"""Optional feeds: association, units, freshness, privacy and cost limits."""
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds, nearby_distance_miles, request_json


NOW = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)


def selected(**changes):
    return {"id": "roster-1", "number": "1946", "origin": "ATL", "destination": "MEM",
            "start": (NOW - timedelta(minutes=20)).isoformat(),
            "end": (NOW + timedelta(minutes=60)).isoformat(), **changes}


def scheduled(identifier="matching-flight", minutes=0, **changes):
    return {"fa_flight_id": identifier, "ident": "DAL1946", "ident_iata": "DL1946", "origin": {"code_iata": "ATL"},
            "destination": {"code_iata": "MEM"},
            "scheduled_out": (NOW - timedelta(minutes=20) + timedelta(minutes=minutes)).isoformat(), **changes}


def position(**changes):
    return {"last_position": {"latitude": 34.5, "longitude": -87.0,
                              "timestamp": NOW.isoformat(), "altitude": 350,
                              "groundspeed": 420, "heading": 280, **changes}}


def nearby_aircraft(ident="abcdef", miles=1, **changes):
    # Synthetic equatorial pin only; no real household location in fixtures.
    return {"hex": ident, "lat": math.degrees(miles * 1609.344 / 6_371_008.8), "lon": 0,
            "seen_pos": 5, "alt_baro": 12000, **changes}


def nearby_snapshot(rows, observed=NOW):
    return {"now": observed.timestamp() * 1000, "ac": rows}


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / "flightaware.key").write_text("synthetic-test-key")
        self.feed = FlightFeeds(self.directory)
        self.config = DEFAULTS | {"demo": False, "flightaware_enabled": True, "airline": "DAL"}
        self.clock = patch("flightwall.feeds.utcnow", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.ticker = patch("flightwall.feeds.time.monotonic", return_value=10)
        self.ticker.start()
        self.addCleanup(self.ticker.stop)

    def poll_live(self, flights=None, observation=None, chosen=None):
        response = [{"flights": flights if flights is not None else [scheduled()]}, observation or position()]
        fake = patch.object(self.feed, "_flightaware", side_effect=response)
        with fake as call:
            self.feed.poll(self.config, chosen or selected(), on_duty=True)
        return call

    def test_demo_or_disabled_never_calls_optional_services(self):
        with patch("flightwall.feeds.request_json") as network:
            self.feed.poll(DEFAULTS, selected())
            self.feed.poll(self.config | {"demo": True}, selected())
            network.assert_not_called()
        self.assertIsNone(self.feed.live)

    def test_first_poll_after_boot_matches_unique_dated_route_and_units(self):
        call = self.poll_live([scheduled("yesterday", -1440), scheduled("correct", 0),
                               scheduled("wrong-route", destination={"code_iata": "LAX"})])
        self.assertEqual(self.feed._live_id, "correct")
        self.assertEqual(self.feed.live["flight_id"], "roster-1")
        self.assertEqual(self.feed.live["altitude"], 35000)
        self.assertEqual(self.feed.live["altitude_units"], "feet")
        self.assertEqual(self.feed.live["groundspeed"], 420)
        self.assertEqual(self.feed.live["source"], "FlightAware")
        self.assertIn("/flights/DAL1946?", call.call_args_list[0].args[0])
        query = parse_qs(urlsplit(call.call_args_list[0].args[0]).query)
        self.assertEqual(datetime.fromisoformat(query["start"][0]).utcoffset(), timedelta(0))
        self.assertEqual(call.call_args_list[1].args[0], "/flights/correct/position")

    def test_existing_airline_prefix_is_preserved(self):
        call = self.poll_live(chosen=selected(number="AA 1946"))
        self.assertIn("/flights/AA1946?", call.call_args_list[0].args[0])

    def test_bare_number_requires_user_airline(self):
        with patch.object(self.feed, "_flightaware") as network:
            self.feed.poll(self.config | {"airline": ""}, selected())
            network.assert_not_called()
        self.assertIsNone(self.feed.live)
        self.assertIn("lookup unavailable", self.feed.error)

    def test_wrong_date_or_ambiguous_match_never_attaches_position(self):
        for matches in ([scheduled("yesterday", -1440)], [scheduled("a", -10), scheduled("b", 10)]):
            self.feed = FlightFeeds(self.directory)
            call = self.poll_live(matches)
            self.assertEqual(call.call_count, 1)
            self.assertIsNone(self.feed.live)

    def test_position_older_than_five_minutes_or_future_is_not_live(self):
        for minutes in (-6, 6):
            self.feed = FlightFeeds(self.directory)
            self.poll_live(observation=position(timestamp=(NOW + timedelta(minutes=minutes)).isoformat()))
            self.assertIsNone(self.feed.live)

    def test_bad_coordinate_is_not_live(self):
        for latitude in (True, float("nan"), 91):
            self.feed = FlightFeeds(self.directory)
            self.poll_live(observation=position(latitude=latitude))
            self.assertIsNone(self.feed.live)

    def test_changing_selected_flight_clears_prior_position_immediately(self):
        self.poll_live()
        self.assertIsNotNone(self.feed.live)
        with patch.object(self.feed, "_flightaware", side_effect=OSError("offline")):
            self.feed.poll(self.config, selected(id="roster-2"))
        self.assertIsNone(self.feed.live)

    def test_poll_rate_limit_and_expired_cached_position(self):
        self.poll_live()
        with patch.object(self.feed, "_flightaware") as network:
            self.feed.poll(self.config, selected())
            network.assert_not_called()
        self.feed.live["observed_at"] = (NOW - timedelta(minutes=6)).isoformat()
        self.feed.poll(self.config, selected())
        self.assertIsNone(self.feed.live)

    def test_daily_budget_counts_requests_and_rejects_corrupt_counter(self):
        self.config["flightaware_daily_limit"] = 2
        self.feed._allow_paid_request(self.config, "/flights/DAL1946?max_pages=1")
        self.feed._allow_paid_request(self.config, "/flights/DAL1946?max_pages=1")
        with self.assertRaises(ValueError):
            self.feed._allow_paid_request(self.config, "/flights/DAL1946?max_pages=1")
        usage_path = self.directory / "flightaware-usage.json"
        if os.name == "posix":
            self.assertEqual(usage_path.stat().st_mode & 0o777, 0o600)
        usage_path.write_text("corrupt")
        with self.assertRaises(ValueError):
            self.feed._allow_paid_request(self.config, "/flights/DAL1946?max_pages=1")

    def test_paid_key_is_header_only_and_not_in_request_url(self):
        (self.directory / "flightaware.key").write_text("private-example-key")
        with patch("flightwall.feeds.request_json", return_value={}) as network:
            self.feed._flightaware("/flights/DAL1946", self.config)
        self.assertNotIn("private-example-key", network.call_args.args[0])
        self.assertEqual(network.call_args.args[1], {"x-apikey": "private-example-key"})

    def test_nearby_runs_only_off_duty_and_filters_old_or_invalid_positions(self):
        config = self.config | {"flightaware_enabled": False, "nearby_enabled": True,
                                "home_latitude": 33.64, "home_longitude": -84.43}
        aircraft = [{"hex": "abcdef", "lat": 34, "lon": -84, "seen_pos": 5, "flight": "DAL123 ", "alt_baro": 12000},
                    {"hex": "abcde1", "lat": 34, "lon": -84, "seen_pos": 200},
                    {"hex": "abcde2", "lat": True, "lon": -84, "seen_pos": 1},
                    {"hex": "abcde3", "lat": 34, "lon": -84, "seen_pos": 1, "alt_baro": "ground"},
                    {"hex": "abcde4", "lat": 34, "lon": -84, "seen_pos": 1}]
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot(aircraft)) as network:
            self.feed.poll(config, selected(), on_duty=True)
            network.assert_not_called()
            self.feed.poll(config, selected(), on_duty=False)
            self.assertIn("/v2/point/33.64/-84.43/50", network.call_args.args[0])
        self.assertEqual(len(self.feed.nearby), 2)
        by_id = {row["hex"]: row for row in self.feed.nearby}
        self.assertEqual(by_id["abcdef"]["altitude"], 12000)
        self.assertEqual(by_id["abcdef"]["flight"], "DAL123")
        self.assertIsNone(by_id["abcde4"]["altitude"])
        self.assertEqual(by_id["abcdef"]["observed_at"], (NOW - timedelta(seconds=5)).isoformat())

    def nearby_config(self, **changes):
        return self.config | {"flightaware_enabled": False, "nearby_enabled": True,
                              "home_latitude": 0.0, "home_longitude": 0.0,
                              "nearby_radius_miles": 5.0, **changes}

    def test_five_statute_miles_filters_exact_circle_and_sanitizes_provider_metadata(self):
        rows = [nearby_aircraft("abcde1", 5.001), nearby_aircraft("abcde2", 5),
                nearby_aircraft("abcde3", 2, flight=" DAL123 ", r="N123AB", t="A321", gs=420, track=270,
                                origin="NOT VERIFIED", destination="NOT VERIFIED"),
                nearby_aircraft("abcde4", 1, flight=None, r=None, t={"bad": "value"},
                                alt_baro=None, gs=float("nan"), track=True),
                nearby_aircraft("abcde5", 0.5, alt_baro="ground")]
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot(rows)) as network:
            self.feed.poll(self.nearby_config(), None)
        self.assertTrue(network.call_args.args[0].endswith("/0.0/0.0/5"))
        self.assertEqual([row["hex"] for row in self.feed.nearby], ["abcde4", "abcde3", "abcde2"])
        unknown, known, edge = self.feed.nearby
        self.assertEqual(known["callsign"], "DAL123")
        self.assertEqual(known["registration"], "N123AB")
        self.assertEqual(known["aircraft_type"], "A321")
        self.assertNotIn("origin", known)
        self.assertNotIn("destination", known)
        self.assertEqual(unknown["flight"], "ABCDE4")
        self.assertTrue(all(unknown[key] is None for key in ("registration", "aircraft_type", "altitude", "groundspeed", "track")))
        self.assertAlmostEqual(edge["distance_miles"], 5, places=7)

    def test_nearby_deduplicates_freshest_identity_before_limiting_nearest_forty(self):
        rows = [nearby_aircraft(f"{index:06x}", index / 100) for index in range(60, 0, -1)]
        # The nearer but older duplicate must not displace its fresh observation.
        rows.append(nearby_aircraft("000001", 0.001, seen_pos=100))
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot(rows)):
            self.feed.poll(self.nearby_config(), None)
        self.assertEqual(len(self.feed.nearby), 40)
        self.assertEqual([row["hex"] for row in self.feed.nearby], [f"{index:06x}" for index in range(1, 41)])
        self.assertAlmostEqual(self.feed.nearby[0]["distance_miles"], 0.01)

    def test_nearby_map_offsets_are_north_up_statute_miles_without_home_fields(self):
        angle = math.degrees(1609.344 / 6_371_008.8)
        rows = [nearby_aircraft("abcde1", lat=angle, lon=0),
                nearby_aircraft("abcde2", lat=0, lon=angle),
                nearby_aircraft("abcde3", lat=-angle, lon=0),
                nearby_aircraft("abcde4", lat=0, lon=-angle),
                nearby_aircraft("abcde5", lat=angle, lon=angle),
                nearby_aircraft("abcde6", lat=0, lon=0)]
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot(rows)):
            self.feed.poll(self.nearby_config(), None)
        by_id = {row["hex"]: row for row in self.feed.nearby}
        for ident, (east, north) in {"abcde1": (0, 1), "abcde2": (1, 0), "abcde3": (0, -1),
                                     "abcde4": (-1, 0), "abcde6": (0, 0)}.items():
            with self.subTest(ident=ident):
                self.assertAlmostEqual(by_id[ident]["east_miles"], east, places=7)
                self.assertAlmostEqual(by_id[ident]["north_miles"], north, places=7)
        self.assertGreater(by_id["abcde5"]["east_miles"], 0)
        self.assertGreater(by_id["abcde5"]["north_miles"], 0)
        for row in self.feed.nearby:
            self.assertAlmostEqual(math.hypot(row["east_miles"], row["north_miles"]), row["distance_miles"], places=7)
            self.assertNotIn("home_latitude", row)
            self.assertNotIn("home_longitude", row)

    def test_nearby_map_offsets_handle_dateline_and_polar_bearing(self):
        for center, point, expected_direction in (((0, 179.99), (0, -179.99), "east_miles"),
                                                   ((89.99, 0), (89.99, 180), "north_miles")):
            with self.subTest(center=center):
                feed = FlightFeeds(self.directory)
                with patch("flightwall.feeds.request_json", return_value=nearby_snapshot([
                    nearby_aircraft(lat=point[0], lon=point[1])
                ])):
                    feed.poll(self.nearby_config(home_latitude=center[0], home_longitude=center[1]), None)
                row = feed.nearby[0]
                self.assertGreater(row[expected_direction], 0)
                self.assertAlmostEqual(row[expected_direction], row["distance_miles"], places=7)

    def test_nearby_uses_provider_snapshot_age_not_receipt_or_latest_message(self):
        rows = [nearby_aircraft("abcde1", seen_pos=5),
                nearby_aircraft("abcde2", seen_pos=21),
                nearby_aircraft("abcde3", seen_pos=None, seen=0),
                nearby_aircraft("abcde4", seen_pos=-1),
                nearby_aircraft("abcde5", seen_pos=True)]
        observed = NOW - timedelta(seconds=100)
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot(rows, observed)):
            self.feed.poll(self.nearby_config(), None)
        self.assertEqual([row["hex"] for row in self.feed.nearby], ["abcde1"])
        self.assertEqual(self.feed.nearby[0]["observed_at"], (NOW - timedelta(seconds=105)).isoformat())

    def test_nearby_rejects_malformed_stale_or_future_envelopes(self):
        good = nearby_snapshot([nearby_aircraft()])
        for response in ([], {"ac": []}, good | {"now": True}, good | {"now": float("inf")},
                         good | {"now": NOW.timestamp()}, good | {"now": NOW.timestamp() * 1000 + 1},
                         nearby_snapshot([], NOW - timedelta(seconds=121)), good | {"ac": {}},
                         good | {"ac": [None] * 5001}):
            with self.subTest(response_type=type(response).__name__):
                feed = FlightFeeds(self.directory)
                with patch("flightwall.feeds.request_json", return_value=response):
                    feed.poll(self.nearby_config(), None)
                self.assertEqual(feed.nearby, [])
                self.assertEqual(feed.error, "Nearby traffic feed unavailable.")

    def test_nearby_cached_positions_expire_without_an_extra_network_request(self):
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot([nearby_aircraft(seen_pos=100)])) as network:
            self.feed.poll(self.nearby_config(), None)
            self.assertEqual(len(self.feed.nearby), 1)
            with patch("flightwall.feeds.utcnow", return_value=NOW + timedelta(seconds=21)):
                self.feed.poll(self.nearby_config(), None)
            self.assertEqual(self.feed.nearby, [])
            network.assert_called_once()

    def test_nearby_changed_pin_or_radius_clears_previous_query_immediately(self):
        new_latitude = nearby_aircraft(miles=0.5)["lat"]
        for changes in ({"home_latitude": new_latitude}, {"nearby_radius_miles": 0.5}):
            with self.subTest(changes=changes):
                self.feed = FlightFeeds(self.directory)
                def snapshot_after_clear(*args):
                    self.assertEqual(self.feed.nearby, [])
                    return nearby_snapshot([nearby_aircraft()])
                with patch("flightwall.feeds.request_json", side_effect=snapshot_after_clear) as network:
                    self.feed.poll(self.nearby_config(), None)
                    self.assertAlmostEqual(self.feed.nearby[0]["north_miles"], 1)
                    self.feed.poll(self.nearby_config(**changes), None)
                    self.assertEqual(network.call_count, 2)
                    if "home_latitude" in changes:
                        self.assertAlmostEqual(self.feed.nearby[0]["north_miles"], 0.5)
                    else:
                        self.assertEqual(self.feed.nearby, [])
        with patch("flightwall.feeds.request_json") as network:
            self.feed.poll(self.nearby_config(home_latitude=None), None)
            network.assert_not_called()
            self.assertEqual(self.feed.nearby, [])

    def test_nearby_publication_matches_completed_pin_and_radius_and_returns_copies(self):
        config = self.nearby_config()
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot([nearby_aircraft()])):
            self.feed.poll(config, None)
        published = self.feed.nearby_for_config(config, NOW)
        self.assertEqual(len(published), 1)
        published[0]["east_miles"] = 123
        self.assertNotEqual(self.feed.nearby_for_config(config, NOW)[0]["east_miles"], 123)
        for changes in ({"nearby_enabled": False}, {"demo": True}, {"home_latitude": 1},
                        {"home_longitude": 1}, {"nearby_radius_miles": 4}, {"home_latitude": None},
                        {"nearby_radius_miles": True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.feed.nearby_for_config(config | changes, NOW), [])
        self.assertEqual(self.feed.nearby_for_config(config, NOW + timedelta(seconds=116)), [])

    def test_nearby_inflight_old_pin_response_is_not_published_for_new_config(self):
        initial = self.nearby_config()
        updated = initial | {"home_latitude": nearby_aircraft(miles=0.5)["lat"]}
        def old_fetch(*args):
            # An admin has saved `updated` while this older request is in flight.
            self.assertEqual(self.feed.nearby_for_config(updated, NOW), [])
            return nearby_snapshot([nearby_aircraft()])
        with patch("flightwall.feeds.request_json", side_effect=old_fetch):
            self.feed.poll(initial, None)
        self.assertEqual(self.feed.nearby_for_config(updated, NOW), [])
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot([nearby_aircraft()])) as network:
            self.feed.poll(updated, None)
            network.assert_called_once()
        self.assertAlmostEqual(self.feed.nearby_for_config(updated, NOW)[0]["north_miles"], 0.5)
        self.assertEqual(self.feed.nearby_for_config(initial, NOW), [])

    def test_nearby_publication_clears_after_failure_or_on_duty(self):
        config = self.nearby_config()
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot([nearby_aircraft()])):
            self.feed.poll(config, None)
        with patch("flightwall.feeds.time.monotonic", return_value=71), \
                patch("flightwall.feeds.request_json", side_effect=OSError("offline")):
            self.feed.poll(config, None)
        self.assertEqual(self.feed.nearby_for_config(config, NOW), [])
        with patch("flightwall.feeds.time.monotonic", return_value=132), \
                patch("flightwall.feeds.request_json", return_value=nearby_snapshot([nearby_aircraft()])):
            self.feed.poll(config, None)
        self.assertEqual(len(self.feed.nearby_for_config(config, NOW)), 1)
        self.feed.poll(config, None, on_duty=True)
        self.assertEqual(self.feed.nearby_for_config(config, NOW), [])

    def test_nearby_distance_wraps_dateline_and_poles(self):
        self.assertLess(nearby_distance_miles(0, 179.99, 0, -179.99), 2)
        self.assertLess(nearby_distance_miles(89.99, 0, 89.99, 180), 2)
        self.assertEqual(nearby_distance_miles(0, 0, 0, 0), 0)

    def test_legacy_nautical_mile_radius_is_not_rounded_up_during_conversion(self):
        with patch("flightwall.feeds.request_json", return_value=nearby_snapshot([])) as network:
            self.feed.poll(self.nearby_config(nearby_radius_miles=None, nearby_radius_nm=7), None)
        self.assertTrue(network.call_args.args[0].endswith("/0.0/0.0/7"))

    def test_nearby_network_failure_clears_old_aircraft(self):
        self.feed.nearby = [{"flight": "old"}]
        with patch("flightwall.feeds.request_json", side_effect=OSError("offline")):
            self.feed.poll(self.config | {"flightaware_enabled": False, "nearby_enabled": True,
                                          "home_latitude": 33, "home_longitude": -84}, None)
        self.assertEqual(self.feed.nearby, [])
        self.assertEqual(self.feed.error, "Nearby traffic feed unavailable.")

    def test_feed_network_rejects_http_and_redirects(self):
        with self.assertRaises(ValueError):
            request_json("http://example.com/feed")
        with patch("flightwall.feeds.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = OSError("blocked")
            with self.assertRaises(OSError):
                request_json("https://example.com/feed")
            handler = opener.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://another.example"))


if __name__ == "__main__":
    unittest.main()
