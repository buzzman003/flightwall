"""Per-frame forecast/position expiry and readable, fictional TV projections."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from flightwall import render
from flightwall.extra_views import render_nearby, render_weather

NOW = datetime(2026, 10, 31, 17, tzinfo=timezone.utc)


def weather_state():
    day = {"date": "2026-10-31", "high": 72, "low": 51, "temperature_unit": "F",
           "summary": "Partly cloudy", "complete": True, "stale": False}
    return {"demo": False, "display_timezone": "America/Chicago", "weather": {
        "status": "ready", "source": "National Weather Service",
        "hourly_fetched_at": NOW.isoformat(), "daily_fetched_at": NOW.isoformat(),
        "hourly_updated_at": NOW.isoformat(), "daily_updated_at": NOW.isoformat(),
        "today": day, "tomorrow": day | {"date": "2026-11-01", "high": 68, "low": 48, "summary": "Rain showers"},
        "overnight": {"start": "2026-10-31T18:00:00-05:00", "end": "2026-11-01T08:00:00-06:00",
                      "coverage_start": "2026-10-31T23:00:00+00:00",
                      "max_precipitation_probability": 60, "coverage_complete": True,
                      "covered_hours": 15, "expected_hours": 15, "rain_mentioned": True,
                      "stale": False, "risk": "possible"}}}


def aircraft(ident="111111", *, east=1, north=1, age=0, **changes):
    return {"hex": ident, "registration": "TEST01", "callsign": "DEMO101", "aircraft_type": "C172",
            "latitude": 39.75, "longitude": -97.08, "east_miles": east, "north_miles": north,
            "distance_miles": math.hypot(east, north), "altitude": 2400, "groundspeed": 110,
            "observed_at": (NOW - timedelta(seconds=age)).isoformat(), **changes}


def capture(view, state, config, now=NOW):
    image = Image.new("RGB", (512, 256), render.BG)
    labels = []
    original = render._text
    def text(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=text):
        view(ImageDraw.Draw(image), image, state, config, now)
    return labels, image


class WeatherViewTests(unittest.TestCase):
    config = {"weather_enabled": True, "demo": False, "timezone": "America/Chicago"}

    def test_two_days_rain_and_peak_hourly_label_are_prominent(self):
        labels, image = capture(render_weather, weather_state(), self.config)
        for value in ("TODAY", "TOMORROW", "72", "51", "68", "48", "RAIN POSSIBLE", "PUT CUSHIONS IN", "PEAK HOUR 60%"):
            self.assertIn(value, labels)
        self.assertIn("6 PM-8 AM LOCAL", labels)
        self.assertIn("NWS / AS OF 10/31 12:00 PM CDT", labels)
        self.assertEqual(image.size, (512, 256))
        self.assertEqual(image.mode, "RGB")
        self.assertNotIn("4 DAY", " ".join(labels))

    def test_icons_follow_known_fresh_summary_and_hide_stale_conditions(self):
        from flightwall.extra_views import _weather_icon
        state = weather_state()
        seen = []
        def icon(draw, xy, kind):
            seen.append(kind)
            _weather_icon(draw, xy, kind)
        with patch("flightwall.extra_views._weather_icon", side_effect=icon):
            capture(render_weather, state, self.config)
            self.assertEqual(seen, ["partly", "rain"])
            seen.clear()
            state["weather"]["daily_fetched_at"] = (NOW - timedelta(hours=2)).isoformat()
            capture(render_weather, state, self.config)
            self.assertEqual(seen, ["unknown", "unknown"])
            seen.clear()
            capture(render_weather, weather_state(), self.config | {"weather_enabled": False})
            self.assertEqual(seen, ["unknown", "unknown"])

    def test_overnight_and_issuance_use_unambiguous_am_pm_through_dst(self):
        state = weather_state()
        now = datetime(2026, 11, 1, 7, 45, tzinfo=timezone.utc)  # second 1:45 AM, CST
        state["weather"]["hourly_fetched_at"] = now.isoformat()
        state["weather"]["hourly_updated_at"] = "2026-11-01T07:30:00Z"
        labels, _ = capture(render_weather, state, self.config, now)
        self.assertIn("NOW-8 AM LOCAL", labels)
        self.assertIn("NWS / AS OF 11/01 1:30 AM CST", labels)
        self.assertNotIn("NOW-08:00 LOCAL", labels)
        self.assertNotIn("18:00-08:00 LOCAL", labels)

    def test_crisp_weather_symbols_do_not_invent_conditions_for_unknown_text(self):
        from flightwall.extra_views import _weather_icon, _weather_icon_kind
        cases = (("Sunny", "sun"), ("Partly Sunny", "partly"), ("Mostly Cloudy", "cloud"),
                 ("Chance Rain Showers", "rain"), ("Slight Chance Thunderstorms", "thunder"),
                 ("Snow", "snow"), ("Patchy Fog", "fog"), ("Unavailable", "unknown"),
                 (None, "unknown"), ("Hot", "unknown"))
        for summary, expected in cases:
            with self.subTest(summary=summary):
                self.assertEqual(_weather_icon_kind(summary), expected)
                image = Image.new("RGB", (24, 18))
                _weather_icon(ImageDraw.Draw(image), (0, 0), expected)
                self.assertIsNotNone(image.getbbox())

    def test_low_requires_complete_exact_window_and_probability(self):
        state = weather_state()
        state["weather"]["overnight"].update(max_precipitation_probability=10, rain_mentioned=False, risk="low")
        self.assertIn("LOW PRECIP CHANCE", capture(render_weather, state, self.config)[0])
        for change in ({"coverage_complete": False}, {"covered_hours": 14}, {"expected_hours": 14},
                       {"max_precipitation_probability": None}, {"max_precipitation_probability": False},
                       {"start": "2026-10-30T18:00:00-05:00"}, {"coverage_start": None},
                       {"coverage_start": "2026-11-01T06:00:00+00:00"}):
            with self.subTest(change=change):
                modified = deepcopy(state)
                modified["weather"]["overnight"].update(change)
                labels, _ = capture(render_weather, modified, self.config)
                self.assertNotIn("LOW PRECIP CHANCE", labels)
                self.assertIn("RAIN RISK UNKNOWN", labels)

    def test_partial_rain_warning_preserves_unknown_coverage(self):
        state = weather_state()
        state["weather"]["overnight"].update(coverage_complete=False, covered_hours=5)
        labels, _ = capture(render_weather, state, self.config)
        self.assertIn("PUT CUSHIONS IN", labels)
        self.assertIn("PARTIAL DATA", labels)

    def test_paused_frame_ages_out_without_backend_update(self):
        state = weather_state()
        later = NOW + timedelta(hours=2)
        labels, _ = capture(render_weather, state, self.config, later)
        self.assertIn("FORECAST STALE", labels)
        self.assertNotIn("PUT CUSHIONS IN", labels)
        self.assertNotIn("LOW PRECIP CHANCE", labels)

    def test_backend_loss_and_provider_age_disable_assurance(self):
        for change in ({"weather_stale": True}, {"weather": weather_state()["weather"] | {"stale": True}},
                       {"weather": weather_state()["weather"] | {"hourly_updated_at": (NOW - timedelta(hours=12)).isoformat()}}):
            with self.subTest(change=change):
                labels, _ = capture(render_weather, weather_state() | change, self.config)
                self.assertIn("FORECAST STALE", labels)

    def test_midnight_invalidates_daily_values_but_preserves_active_night(self):
        state = weather_state()
        tomorrow = datetime(2026, 11, 1, 5, 1, tzinfo=timezone.utc)
        # Even a falsely fresh timestamp cannot relabel yesterday's values.
        state["weather"]["hourly_fetched_at"] = tomorrow.isoformat()
        state["weather"]["daily_fetched_at"] = tomorrow.isoformat()
        state["weather"]["hourly_updated_at"] = tomorrow.isoformat()
        labels, _ = capture(render_weather, state, self.config, tomorrow)
        self.assertNotIn("72", labels)
        self.assertNotIn("68", labels)
        self.assertIn("RAIN POSSIBLE", labels)
        self.assertIn("NOW-8 AM LOCAL", labels)

    def test_remaining_night_snapshot_does_not_lose_coverage_as_frame_ages(self):
        state = weather_state()
        now = datetime(2026, 11, 1, 1, 31, 15, tzinfo=timezone.utc)  # 20:31 CDT
        for section in ("hourly", "daily"):
            state["weather"][section + "_fetched_at"] = now.isoformat()
            state["weather"][section + "_updated_at"] = now.isoformat()
        night = state["weather"]["overnight"]
        hours = round((datetime.fromisoformat(night["end"]) - now).total_seconds() / 3600, 2)
        night.update(coverage_start=now.isoformat(), expected_hours=hours, covered_hours=hours,
                     max_precipitation_probability=10, rain_mentioned=False)
        labels, _ = capture(render_weather, state, self.config, now + timedelta(minutes=10, microseconds=500))
        self.assertIn("NOW-8 AM LOCAL", labels)
        self.assertIn("LOW PRECIP CHANCE", labels)

    def test_hourly_warning_survives_missing_daily_forecasts(self):
        state = weather_state()
        state["weather"].update(status="partial", today=None, tomorrow=None, daily_fetched_at=None, daily_updated_at=None)
        labels, _ = capture(render_weather, state, self.config)
        self.assertIn("RAIN POSSIBLE", labels)
        self.assertIn("PUT CUSHIONS IN", labels)
        self.assertIn("Forecast unavailable", labels)

    def test_eight_am_rejects_previous_active_night(self):
        state = weather_state()
        now = datetime(2026, 11, 1, 14, tzinfo=timezone.utc)  # 08:00 CST
        state["weather"]["hourly_fetched_at"] = now.isoformat()
        state["weather"]["hourly_updated_at"] = now.isoformat()
        labels, _ = capture(render_weather, state, self.config, now)
        self.assertIn("RAIN RISK UNKNOWN", labels)
        self.assertNotIn("RAIN POSSIBLE", labels)
        self.assertIn("6 PM-8 AM LOCAL", labels)

    def test_fresh_hourly_survives_stale_daily_section(self):
        state = weather_state()
        state["weather"]["status"] = "stale"
        state["weather"]["daily_fetched_at"] = (NOW - timedelta(hours=3)).isoformat()
        labels, _ = capture(render_weather, state, self.config)
        self.assertIn("STALE FORECAST / CHECK UPDATE", labels)
        self.assertIn("RAIN POSSIBLE", labels)

    def test_missing_disabled_and_unknown_units_do_not_invent_forecast(self):
        for state, config in (({}, self.config), (weather_state(), self.config | {"weather_enabled": False})):
            labels, _ = capture(render_weather, state, config)
            self.assertIn("RAIN RISK UNKNOWN", labels)
            self.assertNotIn("72", labels)
        state = weather_state()
        state["weather"]["today"]["temperature_unit"] = "C"
        self.assertNotIn("72", capture(render_weather, state, self.config)[0])


class NearbyViewTests(unittest.TestCase):
    config = {"nearby_enabled": True, "nearby_radius_miles": 5, "demo": False}

    def test_three_nearest_are_numbered_and_unknown_metadata_is_explicit(self):
        rows = [aircraft("333333", east=3, north=0, registration="TEST03"),
                aircraft("222222", east=2, north=0, registration="TEST02"),
                aircraft("111111", east=1, north=0),
                aircraft("444444", east=4, north=0, registration="TEST04")]
        labels, _ = capture(render_nearby, {"nearby": rows}, self.config)
        self.assertIn("1 TEST01", labels)
        self.assertIn("2 TEST02", labels)
        self.assertIn("3 TEST03", labels)
        self.assertNotIn("4 TEST04", labels)
        self.assertIn("4 FRESH", labels)
        self.assertIn("ORIGIN / DESTINATION UNAVAILABLE", labels)
        unknown = aircraft(registration=None, callsign=None, aircraft_type=None, altitude=None, groundspeed=None)
        labels, _ = capture(render_nearby, {"nearby": [unknown]}, self.config)
        for value in ("1 TAIL UNAVAIL", "TYPE N/A / 1.4 MI", "ALT N/A / SPD N/A", "TAIL UNAVAILABLE"):
            self.assertIn(value, labels)

    def test_future_missing_old_and_outside_positions_are_hidden_every_frame(self):
        rows = [aircraft("111111", age=121), aircraft("222222", age=-1),
                aircraft("333333", observed_at=None), aircraft("444444", east=6, north=0),
                aircraft("555555", east_miles=None), aircraft("666666", latitude=100)]
        labels, _ = capture(render_nearby, {"nearby": rows}, self.config)
        self.assertIn("NO FRESH", labels)
        self.assertIn("0 FRESH", labels)
        fresh = {"nearby": [aircraft(age=119)]}
        self.assertIn("1 FRESH", capture(render_nearby, fresh, self.config)[0])
        self.assertIn("0 FRESH", capture(render_nearby, fresh, self.config, NOW + timedelta(seconds=2))[0])

    def test_actual_vector_radius_overrides_claimed_distance_and_nmi_fallback(self):
        row = aircraft(east=5.01, north=0, distance_miles=0)
        self.assertIn("0 FRESH", capture(render_nearby, {"nearby": [row]}, self.config)[0])
        config = self.config | {"nearby_radius_miles": None, "nearby_radius_nm": 5}
        self.assertIn("1 FRESH", capture(render_nearby, {"nearby": [row]}, config)[0])

    def test_map_uses_received_vector_without_extrapolation_or_private_home(self):
        row = aircraft(east=2, north=1, track=90, groundspeed=400)
        state = {"nearby": [row]}
        _, before = capture(render_nearby, state, self.config)
        _, later = capture(render_nearby, state, self.config, NOW + timedelta(seconds=30))
        self.assertEqual(before.crop((14, 56, 310, 204)).tobytes(), later.crop((14, 56, 310, 204)).tobytes())
        changed = deepcopy(state)
        changed["nearby"][0]["east_miles"] = -2
        _, other = capture(render_nearby, changed, self.config)
        self.assertNotEqual(before.crop((14, 56, 310, 204)).tobytes(), other.crop((14, 56, 310, 204)).tobytes())
        labels, _ = capture(render_nearby, state, self.config)
        self.assertNotIn("39.75", " ".join(labels))
        self.assertNotIn("-97.08", " ".join(labels))

    def test_duplicate_aircraft_and_more_than_forty_are_bounded(self):
        rows = [aircraft(f"{index:06x}", east=0.1 + index / 100, north=0) for index in range(60)]
        rows += [deepcopy(rows[0])]
        labels, _ = capture(render_nearby, {"nearby": rows}, self.config)
        self.assertIn("40 FRESH", labels)

    def test_rendering_never_mutates_source_and_demo_hides_live_rows(self):
        state = {"nearby": [aircraft()]}
        original = deepcopy(state)
        _, image = capture(render_nearby, state, self.config)
        self.assertEqual(state, original)
        self.assertEqual(image.size, (512, 256))
        labels, _ = capture(render_nearby, state | {"demo": True}, self.config)
        self.assertIn("0 FRESH", labels)
        self.assertNotIn("1 TEST01", labels)


if __name__ == "__main__":
    unittest.main()
