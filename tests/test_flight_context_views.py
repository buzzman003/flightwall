"""Pure 512x256 context cards age sources each frame and retain TAF uncertainty."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image

from flightwall import flight_context_views as views, operating_map, render
from flightwall.flight_alerts import normalize_operating_status
from flightwall.trip_weather import _report_status, parse_metar, parse_taf

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 16, tzinfo=UTC)
CONFIG = {"demo": False, "timezone": "America/Chicago", "trip_weather_enabled": True,
          "flightaware_enabled": True, "national_radar_enabled": True, "airport_alerts_enabled": True}
FLIGHT = {"id": "fictional-context-flight", "number": "DL123", "origin": "ATL", "destination": "VPS",
          "start": "2026-11-01T15:30:00Z", "end": "2026-11-01T17:00:00Z", "deadhead": False}


def conditions(**extra):
    return {"wdir": 90, "wspd": 12, "wgst": 18, "visib": "6+", "wxString": None,
            "clouds": [{"cover": "BKN", "base": 6000}], **extra}


def period(start, end, change=None, **extra):
    return {"timeFrom": start.timestamp(), "timeTo": end.timestamp(), "fcstChange": change,
            "timeBec": None, "probability": None, **conditions(), **extra}


def airport(now=NOW):
    metar = parse_metar({"icaoId": "KATL", "obsTime": (now - timedelta(minutes=10)).timestamp(),
                         "temp": 20, "fltCat": "VFR", **conditions()}, "KATL", now)
    taf = parse_taf({"icaoId": "KATL", "issueTime": (now - timedelta(hours=1)).isoformat(),
                    "validTimeFrom": (now - timedelta(hours=4)).timestamp(),
                    "validTimeTo": (now + timedelta(hours=24)).timestamp(), "fcsts": [
                        period(now - timedelta(hours=4), now + timedelta(hours=24), wspd=3),
                        period(now - timedelta(hours=1), now + timedelta(hours=8), "FM", wspd=16),
                        period(now, now + timedelta(hours=3), "PROB", probability=30, wxString="-TSRA"),
                        period(now, now + timedelta(hours=2), "TEMPO", wxString="BR"),
                        period(now + timedelta(hours=8), now + timedelta(hours=24), "FM", wspd=22)]}, "KATL", now)
    today = now.astimezone(ZoneInfo(CONFIG["timezone"])).date()
    day = {"date": today.isoformat(), "high": 75, "low": 60, "temperature_unit": "F",
           "stale": False, "summary": "Partly cloudy", "night_only": False}
    return {"iata": "ATL", "icao": "KATL", "roles": ["origin"],
            "metar": _report_status("metar", metar, "KATL", now),
            "taf": _report_status("taf", taf, "KATL", now),
            "forecast": {"status": "ready", "source": "National Weather Service", "timezone": CONFIG["timezone"],
                         "checked_at": now.isoformat(), "issued_at": (now - timedelta(hours=1)).isoformat(),
                         "today": day, "tomorrow": day | {"date": (today + timedelta(days=1)).isoformat(),
                                                         "high": 70, "low": 55, "summary": "Chance rain"}}}


def weather_state(now=NOW):
    return {"demo": False, "backend_available": True, "now": now.isoformat(), "view": "trip_weather",
            "trip_weather": {"status": "ready", "airports": [airport(now)]}}


def radar_state(now=NOW):
    return {"demo": False, "backend_available": True, "now": now.isoformat(), "view": "national_radar",
            "national_radar": {"status": "ready", "image_available": True, "image_sha256": "a" * 64,
                               "issued_at": now.isoformat(), "fetched_at": now.isoformat()}}


def map_state():
    status = normalize_operating_status({"fa_flight_id": "fictional-context-provider",
        "actual_off": "2026-11-01T15:45:00Z", "scheduled_out": FLIGHT["start"],
        "scheduled_in": FLIGHT["end"]}, FLIGHT, NOW)
    position = operating_map.route_endpoints(FLIGHT)["origin"]
    return {"demo": False, "backend_available": True, "now": NOW.isoformat(), "view": "operating_map",
            "operating_flight": FLIGHT, "operating_status": status,
            "operating_live": {"latitude": position[1], "longitude": position[0], "observed_at": NOW.isoformat(),
                               "flight_id": FLIGHT["id"], "provider_flight_id": "fictional-context-provider",
                               "source": "FlightAware", "altitude": 24000, "altitude_units": "feet",
                               "groundspeed": 400, "heading": 210}}


def capture(state, config=CONFIG):
    labels = []
    original = render._text
    def text(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=text):
        image = render.render_frame(state, config)
    return labels, image


class TripWeatherViewTests(unittest.TestCase):
    def test_actual_schema_temperature_and_latest_active_fm_are_visible(self):
        labels, image = capture(weather_state())
        self.assertIn("68F / VFR", labels)
        self.assertIn("PREVAILING 9AM-6PM", labels)
        self.assertIn("TODAY 11/01 / NWS CHICAGO", labels)
        self.assertIn("HI 75 / LO 60F", labels)
        self.assertIn("TOMORROW 11/02 / NWS CHICAGO", labels)
        groups, omitted = views._taf_groups(airport()["taf"], NOW)
        self.assertEqual(groups[0][0]["wind_speed_knots"], 16)
        self.assertEqual(groups[0][1], "PREVAILING")
        self.assertEqual(omitted, 2)
        self.assertTrue(any("GROUPS / OFFICIAL BRIEF" in value for value in labels))
        self.assertEqual((image.mode, image.size), ("RGB", (512, 256)))

    def test_prob_tempo_and_becoming_are_explicit_and_additional_groups_counted(self):
        state = weather_state()
        card = state["trip_weather"]["airports"][0]
        next(p for p in card["taf"]["periods"] if p["change"] == "PROB")["end"] = (NOW + timedelta(hours=1)).isoformat()
        labels, _ = capture(state)
        self.assertTrue(any(value.startswith("POSSIBLE 30%") for value in labels))
        self.assertTrue(any("thunderstorms" in value for value in labels))
        card["taf"]["periods"] = [p for p in card["taf"]["periods"] if p["change"] != "PROB"]
        labels, _ = capture(state)
        self.assertTrue(any(value.startswith("TEMPORARY ") for value in labels))
        future = deepcopy(card["taf"]["periods"][0])
        future.update(change="BECMG", start=(NOW - timedelta(minutes=20)).isoformat(),
                      end=(NOW + timedelta(hours=4)).isoformat(), becoming_at=(NOW + timedelta(minutes=40)).isoformat())
        card["taf"]["periods"] = [p for p in card["taf"]["periods"] if p["change"] != "TEMPO"] + [future]
        labels, _ = capture(state)
        self.assertTrue(any(value.startswith("BECOMING ") for value in labels))
        groups, _ = views._taf_groups(card["taf"], NOW)
        self.assertEqual(groups[0][0]["change"], "FM")
        future["becoming_at"] = (NOW - timedelta(minutes=5)).isoformat()
        groups, _ = views._taf_groups(card["taf"], NOW)
        self.assertEqual(groups[0][1], "AFTER BECMG")

    def test_upcoming_taf_and_unknown_change_never_claim_current_observation(self):
        card = airport()
        future = deepcopy(card["taf"]["periods"][0])
        future.update(start=(NOW + timedelta(hours=1)).isoformat(), end=(NOW + timedelta(hours=3)).isoformat())
        unknown = deepcopy(future) | {"change": "UNKNOWN"}
        card["taf"]["periods"] = [future, unknown]
        groups, omitted = views._taf_groups(card["taf"], NOW)
        self.assertEqual(groups[0][1], "UPCOMING")
        self.assertEqual(views._taf_label(groups[1][0], groups[1][1]), "UNDECODED GROUP")
        self.assertEqual(omitted, 0)

    def test_stale_future_offline_disabled_backend_loss_and_station_mismatch_hide_reports(self):
        changes = ({"metar": {"stale": True}}, {"metar": {"status": "unavailable"}},
                   {"metar": {"observed_at": (NOW + timedelta(seconds=1)).isoformat()}},
                   {"metar": {"observed_at": (NOW - timedelta(hours=2)).isoformat()}},
                   {"metar": {"checked_at": (NOW - timedelta(hours=2)).isoformat()}},
                   {"metar": {"icao": "KVPS"}}, {"metar": {"source": "Unknown"}})
        for change in changes:
            with self.subTest(change=change):
                state = weather_state()
                card = state["trip_weather"]["airports"][0]
                card["metar"].update(change["metar"])
                labels, _ = capture(state)
                self.assertNotIn("68F / VFR", labels)
                self.assertIn("CURRENT CONDITIONS UNKNOWN", labels)
        for state_changes, config in (({"backend_available": False}, CONFIG), ({"demo": True}, CONFIG),
                                      ({}, CONFIG | {"demo": True}), ({}, CONFIG | {"trip_weather_enabled": False})):
            with self.subTest(state=state_changes, config=config):
                labels, _ = capture(weather_state() | state_changes, config)
                self.assertNotIn("68F / VFR", labels)
                self.assertNotIn("HI 75 / LO 60F", labels)
                self.assertFalse(any(value.startswith("PREVAILING") for value in labels))

    def test_taf_expiry_and_future_issuance_fail_closed_each_frame(self):
        for changes in ({"valid_end": NOW.isoformat()}, {"stale": True}, {"status": "stale"},
                        {"issued_at": (NOW + timedelta(seconds=1)).isoformat()},
                        {"checked_at": (NOW + timedelta(seconds=1)).isoformat()},
                        {"issued_at": (NOW - timedelta(hours=12)).isoformat()}):
            with self.subTest(changes=changes):
                state = weather_state()
                state["trip_weather"]["airports"][0]["taf"].update(changes)
                labels, _ = capture(state)
                self.assertIn("FUTURE CONDITIONS UNKNOWN", labels)
                self.assertFalse(any(value.startswith("PREVAILING") for value in labels))

    def test_taf_fall_dst_span_keeps_both_clock_offsets_explicit(self):
        now = datetime(2026, 11, 1, 6, tzinfo=UTC)
        period = {"start": "2026-11-01T06:30:00Z", "end": "2026-11-01T07:15:00Z"}
        self.assertEqual(views._period_span(period, CONFIG, now), "1:30AM CDT-1:15AM CST")

    def test_airport_forecast_date_and_zone_are_not_replaced_with_central_date(self):
        now = datetime(2026, 11, 1, 6, tzinfo=UTC)  # Central Sunday 01:00, Pacific Saturday 23:00.
        state = weather_state(now)
        forecast = state["trip_weather"]["airports"][0]["forecast"]
        forecast["timezone"] = "America/Los_Angeles"
        forecast["today"]["date"] = "2026-10-31"
        forecast["tomorrow"]["date"] = "2026-11-01"
        labels, _ = capture(state)
        self.assertIn("TODAY 10/31 / NWS LOS ANGELES", labels)
        self.assertIn("TOMORROW 11/01 / NWS LOS ANGELES", labels)
        self.assertIn("HI 75 / LO 60F", labels)
        self.assertIn("AWC / NWS / TIMES CDT / FAMILY SUMMARY", labels)
        # The date must match that declared forecast timezone, not just the PC.
        forecast["today"]["date"] = "2026-11-01"
        labels, _ = capture(state)
        self.assertNotIn("HI 75 / LO 60F", labels)

    def test_nws_missing_day_high_or_stale_daily_data_does_not_use_hourly_temperature(self):
        state = weather_state()
        forecast = state["trip_weather"]["airports"][0]["forecast"]
        forecast["today"].update(high=None, night_only=True)
        forecast["hourly_temperature"] = 99
        labels, _ = capture(state)
        self.assertIn("LOW 60F", labels)
        self.assertFalse(any("99" in label for label in labels))
        for field, value in (("checked_at", NOW - timedelta(hours=2)), ("issued_at", NOW + timedelta(seconds=1))):
            with self.subTest(field=field):
                changed = deepcopy(state)
                changed["trip_weather"]["airports"][0]["forecast"][field] = value.isoformat()
                labels, _ = capture(changed)
                self.assertNotIn("LOW 60F", labels)
        forecast["today"]["temperature_unit"] = "C"
        labels, _ = capture(state)
        self.assertNotIn("LOW 60F", labels)
        forecast.pop("timezone")
        labels, _ = capture(state)
        self.assertIn("TODAY DATE UNKNOWN / NWS ", labels)


class OtherContextViewTests(unittest.TestCase):
    def test_all_four_views_are_rgb_512_with_and_without_data(self):
        for state in (weather_state(), radar_state(), map_state(),
                      {"view": "airport_alerts", "now": NOW.isoformat(), "demo": False}):
            for empty in (False, True):
                with self.subTest(view=state["view"], empty=empty):
                    value = {"view": state["view"], "now": NOW.isoformat()} if empty else state
                    _, image = capture(value)
                    self.assertEqual(image.mode, "RGB")
                    self.assertEqual(image.size, (512, 256))
                    self.assertIsNotNone(image.getbbox())

    def test_operating_map_metadata_has_route_date_actual_fix_and_telemetry(self):
        labels, _ = capture(map_state())
        for text in ("ATL > VPS", "SCHED 11/01 9:30 AM CST", "ALT 24000 FT", "GS 400 KT", "HDG 210 DEG",
                     "FLIGHTAWARE / FIX 11/01 10:00 AM CST"):
            self.assertIn(text, labels)
        self.assertNotIn("TAXI", " ".join(labels))

    def test_operating_map_uses_identical_plan_and_pixel_gates_and_hides_invalid_fix(self):
        original = operating_map.render_map
        with patch.object(operating_map, "render_map", wraps=original) as pixels:
            capture(map_state())
            self.assertEqual(pixels.call_args.kwargs, {"enabled": True, "demo": False, "backend_available": True})
        for changes, config in (({"backend_available": False}, CONFIG), ({}, CONFIG | {"demo": True}),
                                ({}, CONFIG | {"flightaware_enabled": False})):
            with self.subTest(changes=changes, config=config), patch.object(operating_map, "render_map") as pixels:
                labels, _ = capture(map_state() | changes, config)
                pixels.assert_not_called()
                self.assertIn("WAITING FOR A FRESH FLIGHT POSITION", labels)
        for key, value in (("observed_at", (NOW + timedelta(seconds=1)).isoformat()),
                           ("observed_at", (NOW - timedelta(seconds=301)).isoformat()), ("flight_id", "another-flight")):
            with self.subTest(key=key, value=value):
                state = map_state()
                state["operating_live"][key] = value
                labels, _ = capture(state)
                self.assertIn("WAITING FOR A FRESH FLIGHT POSITION", labels)

    def test_radar_freshness_hash_demo_and_backend_gate_each_frame(self):
        self.assertTrue(views.radar_ready(radar_state(), CONFIG, NOW))
        for changes in ({"fetched_at": (NOW - timedelta(seconds=1200)).isoformat()},
                        {"issued_at": (NOW - timedelta(seconds=1800)).isoformat()},
                        {"fetched_at": (NOW + timedelta(seconds=1)).isoformat()},
                        {"issued_at": (NOW + timedelta(seconds=1)).isoformat()},
                        {"issued_at": None}, {"image_sha256": "invalid"}, {"image_available": False},
                        {"status": "unknown_time"}, {"status": "stale"}):
            with self.subTest(changes=changes):
                state = radar_state()
                state["national_radar"].update(changes)
                self.assertFalse(views.radar_ready(state, CONFIG, NOW))
                labels, _ = capture(state)
                self.assertIn("NO CURRENT NATIONAL IMAGE", labels)
        for state, config in ((radar_state() | {"backend_available": False}, CONFIG),
                              (radar_state() | {"demo": True}, CONFIG),
                              (radar_state(), CONFIG | {"demo": True}),
                              (radar_state(), CONFIG | {"national_radar_enabled": False})):
            self.assertFalse(views.radar_ready(state, config, NOW))

    def test_faa_unavailable_is_unknown_and_program_does_not_claim_flight_impact(self):
        state = {"view": "airport_alerts", "now": NOW.isoformat(), "demo": False, "airport_alerts": {
            "status": "ready", "issued_at": NOW.isoformat(), "fetched_at": NOW.isoformat(),
            "events": [{"airport": "ATL", "kind": "ground_stop", "reason": "Weather",
                        "delay_text": "Until 18:00 UTC"}]}}
        labels, _ = capture(state)
        self.assertIn("GROUND STOP", labels)
        self.assertIn("AIRPORT PROGRAM / FLIGHT IMPACT NOT CONFIRMED", labels)
        self.assertNotIn("CANCELLED", " ".join(labels))
        state["airport_alerts"]["events"] = []
        labels, _ = capture(state)
        self.assertIn("NO CURRENT MATCHED ADVISORY", labels)
        state["backend_available"] = False
        labels, _ = capture(state)
        self.assertIn("FAA ADVISORIES UNAVAILABLE", labels)
        self.assertNotIn("NO CURRENT MATCHED ADVISORY", labels)


if __name__ == "__main__":
    unittest.main()
