"""Optional feeds: association, units, freshness, privacy and cost limits."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds, request_json


NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def selected(**changes):
    return {"id": "roster-1", "number": "1946", "origin": "ATL", "destination": "MEM",
            "start": (NOW - timedelta(minutes=20)).isoformat(),
            "end": (NOW + timedelta(minutes=60)).isoformat(), **changes}


def scheduled(identifier="matching-flight", minutes=0, **changes):
    return {"fa_flight_id": identifier, "origin": {"code_iata": "ATL"},
            "destination": {"code_iata": "MEM"},
            "scheduled_out": (NOW - timedelta(minutes=20) + timedelta(minutes=minutes)).isoformat(), **changes}


def position(**changes):
    return {"last_position": {"latitude": 34.5, "longitude": -87.0,
                              "timestamp": NOW.isoformat(), "altitude": 350,
                              "groundspeed": 420, "heading": 280, **changes}}


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
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

    def test_first_poll_after_boot_matches_nearest_departure_and_units(self):
        call = self.poll_live([scheduled("earlier", -180), scheduled("correct", 0),
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
        self.feed._allow_paid_request(self.config)
        self.feed._allow_paid_request(self.config)
        with self.assertRaises(ValueError):
            self.feed._allow_paid_request(self.config)
        usage_path = self.directory / "flightaware-usage.json"
        if os.name == "posix":
            self.assertEqual(usage_path.stat().st_mode & 0o777, 0o600)
        usage_path.write_text("corrupt")
        with self.assertRaises(ValueError):
            self.feed._allow_paid_request(self.config)

    def test_paid_key_is_header_only_and_not_in_request_url(self):
        (self.directory / "flightaware.key").write_text("private-example-key")
        with patch("flightwall.feeds.request_json", return_value={}) as network:
            self.feed._flightaware("/flights/DAL1946", self.config)
        self.assertNotIn("private-example-key", network.call_args.args[0])
        self.assertEqual(network.call_args.args[1], {"x-apikey": "private-example-key"})

    def test_nearby_runs_only_off_duty_and_filters_old_or_invalid_positions(self):
        config = self.config | {"flightaware_enabled": False, "nearby_enabled": True,
                                "home_latitude": 33.64, "home_longitude": -84.43}
        aircraft = [{"lat": 34, "lon": -84, "seen_pos": 5, "flight": "DAL123 ", "alt_baro": 12000},
                    {"lat": 34, "lon": -84, "seen_pos": 200},
                    {"lat": True, "lon": -84, "seen_pos": 1},
                    {"lat": 34, "lon": -84, "seen_pos": 1, "alt_baro": "ground"}]
        with patch("flightwall.feeds.request_json", return_value={"ac": aircraft}) as network:
            self.feed.poll(config, selected(), on_duty=True)
            network.assert_not_called()
            self.feed.poll(config, selected(), on_duty=False)
            self.assertIn("/v2/point/33.64/-84.43/50", network.call_args.args[0])
        self.assertEqual(len(self.feed.nearby), 2)
        self.assertEqual(self.feed.nearby[0]["altitude"], 12000)
        self.assertEqual(self.feed.nearby[0]["flight"], "DAL123")
        self.assertIsNone(self.feed.nearby[1]["altitude"])
        self.assertEqual(self.feed.nearby[0]["observed_at"], (NOW - timedelta(seconds=5)).isoformat())

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
