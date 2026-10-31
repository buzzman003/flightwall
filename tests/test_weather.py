"""Synthetic NWS forecasts: exact intervals, unknowns, cache and opt-in privacy."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from flightwall.weather import MAX_RESPONSE, POLL_SECONDS, WeatherFeed, request_json

UTC = timezone.utc
ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 31, 17, tzinfo=UTC)
DAILY = "https://api.weather.gov/gridpoints/TOP/31,80/forecast"
HOURLY = DAILY + "/hourly"
POINTS = {"properties": {"forecast": DAILY, "forecastHourly": HOURLY}}


def period(start, end, *, probability=0, temperature=70, daytime=True, summary="Mostly clear", unit="F"):
    return {"startTime": start.isoformat(), "endTime": end.isoformat(),
            "isDaytime": daytime, "temperature": temperature, "temperatureUnit": unit,
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": probability},
            "shortForecast": summary, "detailedForecast": "NOT_RETAINED_PRIVATE_SOURCE_METADATA"}


def documents(now=NOW, probability=0):
    date = now.astimezone(ZONE).date()
    start = datetime.combine(date, datetime.min.time(), ZONE)
    end = start + timedelta(days=3)
    hourly, daily = [], []
    tick = start.astimezone(UTC)
    while tick < end.astimezone(UTC):
        hourly.append(period(tick, tick + timedelta(hours=1), probability=probability))
        tick += timedelta(hours=1)
    for offset in range(3):
        day = start + timedelta(days=offset)
        daily.append(period(day.replace(hour=6), day.replace(hour=18), temperature=70 + offset))
        daily.append(period(day.replace(hour=18), (day + timedelta(days=1)).replace(hour=6), temperature=50 + offset, daytime=False))
    return [{"properties": {"updateTime": now.isoformat(), "periods": hourly}},
            {"properties": {"updateTime": now.isoformat(), "periods": daily}}]


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        # NWS documentation's public example point, never household coordinates.
        self.config = {"demo": False, "weather_enabled": True, "weather_latitude": 39.7456,
                       "weather_longitude": -97.0892, "timezone": "America/Chicago"}
        self.feed = WeatherFeed(self.path, lambda: self.config)
        self.tick = patch("flightwall.weather.time.monotonic", return_value=100)
        self.clock = self.tick.start()
        self.addCleanup(self.tick.stop)

    def poll(self, docs=None, now=NOW):
        with patch("flightwall.weather.request_json", side_effect=[POINTS, *(docs or documents(now))]) as request:
            result = self.feed.poll(now)
        return result, request

    def test_demo_disabled_missing_coordinates_do_not_fetch_or_write(self):
        for changes, expected in (({"demo": True}, "demo"), ({"weather_enabled": False}, "disabled"),
                                  ({"weather_latitude": None}, "unconfigured"),
                                  ({"weather_latitude": True}, "unconfigured"),
                                  ({"timezone": "Not/AZone"}, "unconfigured")):
            with self.subTest(changes=changes):
                old = self.config.copy()
                self.config.update(changes)
                with patch("flightwall.weather.request_json") as request:
                    self.assertEqual(self.feed.poll(NOW)["status"], expected)
                    request.assert_not_called()
                self.config = old
        self.assertFalse((self.path / "weather.json").exists())

    def test_two_days_high_low_and_fall_dst_overnight_exact(self):
        state, request = self.poll()
        self.assertEqual(request.call_count, 3)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["hourly_fetched_at"], NOW.isoformat())
        self.assertEqual(state["daily_fetched_at"], NOW.isoformat())
        self.assertEqual(state["today"]["date"], "2026-10-31")
        self.assertEqual((state["today"]["high"], state["today"]["low"]), (70, 50))
        self.assertEqual((state["tomorrow"]["high"], state["tomorrow"]["low"]), (71, 51))
        night = state["overnight"]
        self.assertEqual(night["start"], "2026-10-31T18:00:00-05:00")
        self.assertEqual(night["end"], "2026-11-01T08:00:00-06:00")
        self.assertEqual(night["expected_hours"], 15)
        self.assertEqual(night["covered_hours"], 15)
        self.assertEqual(night["risk"], "low")
        self.assertTrue(night["coverage_complete"])
        self.assertNotIn("forecast", state)

    def test_spring_dst_and_repeated_fall_hour_are_counted_in_utc(self):
        now = datetime(2026, 3, 7, 17, tzinfo=UTC)
        state, _ = self.poll(now=now)
        self.assertEqual(state["overnight"]["expected_hours"], 13)
        self.assertEqual(state["overnight"]["covered_hours"], 13)
        self.assertTrue(state["overnight"]["coverage_complete"])

    def test_remaining_hours_after_twenty_and_before_eight_use_active_night(self):
        for now, start, end, expected in (
                (datetime(2026, 11, 1, 1, tzinfo=UTC), "2026-10-31T18:00:00-05:00", "2026-11-01T08:00:00-06:00", 13),
                (datetime(2026, 11, 1, 6, tzinfo=UTC), "2026-10-31T18:00:00-05:00", "2026-11-01T08:00:00-06:00", 8),
                (datetime(2026, 11, 1, 7, tzinfo=UTC), "2026-10-31T18:00:00-05:00", "2026-11-01T08:00:00-06:00", 7),
                (datetime(2026, 3, 8, 7, tzinfo=UTC), "2026-03-07T18:00:00-06:00", "2026-03-08T08:00:00-05:00", 6)):
            with self.subTest(now=now):
                self.feed = WeatherFeed(self.path, lambda: self.config)
                docs = documents(now)
                # Model NWS's future-only forecast. Expired early-evening rain
                # is not included in the remaining-hours peak probability.
                docs[0]["properties"]["periods"] = [row for row in docs[0]["properties"]["periods"]
                                                    if datetime.fromisoformat(row["endTime"]) > now]
                state, _ = self.poll(docs, now)
                night = state["overnight"]
                self.assertEqual((night["start"], night["end"]), (start, end))
                self.assertEqual(night["coverage_start"], now.isoformat())
                self.assertEqual(night["expected_hours"], expected)
                self.assertEqual(night["covered_hours"], expected)
                self.assertTrue(night["coverage_complete"])
                self.assertEqual(night["risk"], "low")

    def test_eight_am_starts_upcoming_night_and_partial_hours_are_rounded(self):
        now = datetime(2026, 11, 1, 14, tzinfo=UTC)
        state, _ = self.poll(now=now)
        self.assertEqual(state["overnight"]["start"], "2026-11-01T18:00:00-06:00")
        self.assertEqual(state["overnight"]["expected_hours"], 14)
        self.feed = WeatherFeed(self.path, lambda: self.config)
        later = datetime(2026, 11, 2, 2, 31, 15, 500000, tzinfo=UTC)
        state, _ = self.poll(now=later)
        night = state["overnight"]
        self.assertTrue(night["coverage_complete"])
        self.assertEqual(night["covered_hours"], night["expected_hours"])
        self.assertEqual(night["expected_hours"], round((datetime.fromisoformat(night["end"]) - later.replace(microsecond=0)).total_seconds() / 3600, 2))

    def test_elapsed_evening_rain_is_not_tonights_remaining_risk(self):
        now = datetime(2026, 11, 1, 1, tzinfo=UTC)  # 20:00 CDT
        docs = documents(now)
        for row in docs[0]["properties"]["periods"]:
            if datetime.fromisoformat(row["endTime"]) <= now:
                row["probabilityOfPrecipitation"]["value"] = 100
                row["shortForecast"] = "Rain showers"
        state, _ = self.poll(docs, now)
        self.assertEqual(state["overnight"]["max_precipitation_probability"], 0)
        self.assertFalse(state["overnight"]["rain_mentioned"])
        self.assertEqual(state["overnight"]["risk"], "low")

    def test_peak_is_not_combined_probability_and_end_is_exclusive(self):
        docs = documents(probability=10)
        rows = docs[0]["properties"]["periods"]
        for row in rows:
            stamp = datetime.fromisoformat(row["startTime"]).astimezone(ZONE)
            if stamp.date().isoformat() == "2026-11-01" and stamp.hour == 7:
                row["probabilityOfPrecipitation"]["value"] = 30
            if stamp.date().isoformat() == "2026-11-01" and stamp.hour == 8:
                row["probabilityOfPrecipitation"]["value"] = 100
        state, _ = self.poll(docs)
        self.assertEqual(state["overnight"]["max_precipitation_probability"], 30)
        self.assertEqual(state["overnight"]["risk"], "possible")
        self.assertEqual(state["overnight"]["probability_label"], "Peak hourly precipitation chance")

    def test_gap_null_wrong_units_and_duplicate_never_report_low(self):
        for damage in ("gap", "null", "unit", "duplicate", "naive", "nan", "boolean"):
            with self.subTest(damage=damage):
                self.feed = WeatherFeed(self.path, lambda: self.config)
                docs = documents()
                rows = docs[0]["properties"]["periods"]
                index = next(i for i, row in enumerate(rows) if row["startTime"].startswith("2026-11-01T02:"))
                if damage == "gap":
                    rows.pop(index)
                elif damage == "duplicate":
                    rows.append(deepcopy(rows[index]))
                elif damage == "naive":
                    rows[index]["startTime"] = "2026-11-01T02:00:00"
                elif damage == "unit":
                    rows[index]["probabilityOfPrecipitation"]["unitCode"] = "unknown"
                else:
                    rows[index]["probabilityOfPrecipitation"]["value"] = {"null": None, "nan": float("nan"), "boolean": False}[damage]
                state, _ = self.poll(docs)
                self.assertFalse(state["overnight"]["coverage_complete"])
                self.assertEqual(state["overnight"]["risk"], "unknown")

    def test_rain_text_without_probability_warns_but_does_not_claim_coverage(self):
        docs = documents()
        row = next(row for row in docs[0]["properties"]["periods"] if row["startTime"].startswith("2026-11-01T02:"))
        row["probabilityOfPrecipitation"]["value"] = None
        row["shortForecast"] = "Chance Rain Showers"
        state, _ = self.poll(docs)
        self.assertEqual(state["overnight"]["risk"], "possible")
        self.assertTrue(state["overnight"]["rain_mentioned"])
        self.assertFalse(state["overnight"]["coverage_complete"])

    def test_cache_and_provider_issuance_have_independent_freshness_limits(self):
        self.poll()
        with patch("flightwall.weather.request_json") as network:
            old = self.feed.status(NOW + timedelta(hours=2))
            self.assertEqual(old["status"], "stale")
            self.assertEqual(old["overnight"]["risk"], "stale")
            network.assert_not_called()
        docs = documents()
        for document in docs:
            document["properties"]["updateTime"] = (NOW - timedelta(hours=12)).isoformat()
        self.feed = WeatherFeed(self.path, lambda: self.config)
        stale, _ = self.poll(docs)
        self.assertEqual(stale["overnight"]["risk"], "stale")

    def test_failure_retains_cache_and_is_rate_limited(self):
        before, _ = self.poll()
        self.clock.return_value = 1000
        with patch("flightwall.weather.request_json", side_effect=OSError("PRIVATE_URL_AND_COORDINATES")) as network:
            failed = self.feed.poll(NOW + timedelta(minutes=15))
            self.clock.return_value = 1899.999
            self.feed.poll(NOW + timedelta(minutes=29, seconds=59))
            self.assertEqual(network.call_count, 2)  # hourly/daily; cached points reused
        self.assertEqual(failed["today"], before["today"])
        self.assertEqual(failed["status"], "partial")
        self.assertNotIn("PRIVATE", json.dumps(failed))

    def test_fifteen_minute_poll_boundary_uses_cached_grid_mapping(self):
        self.assertEqual(POLL_SECONDS, 15 * 60)
        self.poll()
        self.clock.return_value = 999.999
        with patch("flightwall.weather.request_json") as network:
            self.feed.poll(NOW + timedelta(minutes=15, microseconds=-1))
            network.assert_not_called()
        later = NOW + timedelta(minutes=15)
        self.clock.return_value = 1000
        with patch("flightwall.weather.request_json", side_effect=documents(later)) as network:
            result = self.feed.poll(later)
            self.assertEqual([call.args[0] for call in network.call_args_list], [HOURLY, DAILY])
        self.assertEqual(result["hourly_fetched_at"], later.isoformat())
        self.assertEqual(result["daily_fetched_at"], later.isoformat())

    def test_disabled_and_demo_skip_even_when_refresh_due_then_resume_without_burst(self):
        self.poll()
        self.clock.return_value = 1000
        with patch("flightwall.weather.request_json") as network:
            self.config["weather_enabled"] = False
            self.assertEqual(self.feed.poll(NOW + timedelta(minutes=15))["status"], "disabled")
            self.config["weather_enabled"] = True
            self.config["demo"] = True
            self.assertEqual(self.feed.poll(NOW + timedelta(minutes=15))["status"], "demo")
            network.assert_not_called()
        self.config["demo"] = False
        later = NOW + timedelta(minutes=15)
        with patch("flightwall.weather.request_json", side_effect=documents(later)) as network:
            self.assertEqual(self.feed.poll(later)["hourly_fetched_at"], later.isoformat())
            self.feed.poll(later)
            self.assertEqual(network.call_count, 2)

    def test_daily_failure_still_allows_fresh_hourly_rain_warning(self):
        with patch("flightwall.weather.request_json", side_effect=[POINTS, documents(probability=60)[0], OSError("unavailable")]):
            state = self.feed.poll(NOW)
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["overnight"]["risk"], "possible")
        self.assertIsNone(state["today"]["high"])

    def test_per_section_fetch_times_survive_partial_refresh(self):
        self.poll()
        later = NOW + timedelta(hours=3)
        self.clock.return_value = 10900
        with patch("flightwall.weather.request_json", side_effect=[documents(later)[0], OSError("daily unavailable")]):
            state = self.feed.poll(later)
        self.assertEqual(state["status"], "stale")
        self.assertEqual(state["hourly_fetched_at"], later.isoformat())
        self.assertEqual(state["daily_fetched_at"], NOW.isoformat())
        self.assertFalse(state["overnight"]["stale"])
        self.assertTrue(state["today"]["stale"])

    def test_reload_cache_private_projection_and_change_location_invalidates(self):
        self.poll()
        content = (self.path / "weather.json").read_text()
        self.assertNotIn("NOT_RETAINED", content)
        self.assertNotIn("39.7456", content)
        self.assertNotIn("gridpoints", content)
        if os.name != "nt":
            self.assertEqual((self.path / "weather.json").stat().st_mode & 0o777, 0o600)
        reloaded = WeatherFeed(self.path, lambda: self.config)
        with patch("flightwall.weather.request_json") as network:
            self.assertEqual(reloaded.status(NOW)["status"], "ready")
            network.assert_not_called()
        projected = json.dumps(reloaded.status(NOW))
        self.assertNotIn("39.7456", projected)
        self.assertNotIn("TOP/31", projected)
        self.config["weather_latitude"] = 40
        self.assertEqual(reloaded.status(NOW)["status"], "unavailable")
        self.config["weather_enabled"] = False
        self.assertIsNone(reloaded.status(NOW)["today"])

    def test_location_change_during_fetch_does_not_publish_old_forecast(self):
        responses = iter([POINTS, *documents()])
        def fetch(url):
            result = next(responses)
            if url == DAILY:
                self.config["weather_longitude"] = -96
            return result
        with patch("flightwall.weather.request_json", side_effect=fetch):
            state = self.feed.poll(NOW)
        self.assertEqual(state["status"], "unavailable")
        self.assertFalse((self.path / "weather.json").exists())

    def test_quantitative_celsius_temperature_and_invalid_values(self):
        docs = documents()
        docs[1]["properties"]["periods"][0]["temperature"] = {"unitCode": "wmoUnit:degC", "value": 20}
        docs[1]["properties"]["periods"][1]["temperature"] = True
        state, _ = self.poll(docs)
        self.assertEqual(state["today"]["high"], 68)
        self.assertEqual(state["today"]["temperature_unit"], "F")
        self.assertIsNone(state["today"]["low"])
        self.assertFalse(state["today"]["complete"])

    def test_malformed_or_future_cache_is_never_low_risk(self):
        self.poll()
        self.feed._cache["hourly"]["periods"][0]["precipitation_probability"] = float("nan")
        state = self.feed.status(NOW)
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["overnight"]["risk"], "unknown")
        self.assertEqual(state["today"]["high"], 70)
        self.feed = WeatherFeed(self.path, lambda: self.config)
        future = self.feed.status(NOW - timedelta(hours=1))
        self.assertEqual(future["overnight"]["risk"], "stale")

    def test_corrupt_daily_cache_does_not_suppress_fresh_hourly_rain(self):
        self.poll(documents(probability=60))
        self.feed._cache["daily"]["periods"][0]["temperature"] = True
        with patch("flightwall.weather.request_json") as network:
            state = self.feed.status(NOW)
            network.assert_not_called()
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["overnight"]["risk"], "possible")
        self.assertEqual(state["overnight"]["max_precipitation_probability"], 60)
        self.assertIsNone(state["today"]["high"])
        self.assertIsNone(state["daily_fetched_at"])
        self.assertEqual(state["error"], "Part of the stored weather forecast is unavailable.")

    def test_untrusted_discovery_urls_never_fetched(self):
        for url in ("http://api.weather.gov/gridpoints/TOP/31,80/forecast/hourly",
                    "https://evil.example/forecast/hourly", "https://api.weather.gov@evil.example/forecast/hourly",
                    "https://api.weather.gov/gridpoints/TOP/31,80/forecast/hourly?secret=test"):
            self.feed = WeatherFeed(self.path, lambda: self.config)
            with patch("flightwall.weather.request_json", return_value={"properties": {"forecast": DAILY, "forecastHourly": url}}) as network:
                state = self.feed.poll(NOW)
                self.assertEqual(network.call_count, 1)
                self.assertEqual(state["status"], "unavailable")


class WeatherTransportTests(unittest.TestCase):
    def test_transport_headers_timeout_and_size_bound(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = HOURLY
        response.read.return_value = b'{"properties":{}}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch("flightwall.weather.urllib.request.build_opener", return_value=opener):
            request_json(HOURLY)
        request = opener.open.call_args.args[0]
        self.assertIn("FlightWall/", request.get_header("User-agent"))
        self.assertEqual(request.get_header("Accept"), "application/geo+json")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 8)
        response.read.assert_called_once_with(MAX_RESPONSE + 1)

    def test_transport_rejects_oversize_redirect_and_insecure_host(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = HOURLY
        response.read.return_value = b' ' * (MAX_RESPONSE + 1)
        opener = MagicMock()
        opener.open.return_value = response
        with patch("flightwall.weather.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                request_json(HOURLY)
            response.geturl.return_value = "https://other.example/"
            with self.assertRaises(ValueError):
                request_json(HOURLY)
            with self.assertRaises(ValueError):
                request_json("http://api.weather.gov/")
            with self.assertRaises(ValueError):
                request_json("https://api.weather.gov:443/")


if __name__ == "__main__":
    unittest.main()
