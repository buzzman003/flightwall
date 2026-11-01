"""Public synthetic airport weather: timestamps, uncertainty, bounded opt-in IO."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flightwall.trip_weather import (CACHE_MAX_AGE, MAX_RESPONSE, POLL_SECONDS, TripWeatherFeed,
                                     _report_status, parse_metar, parse_taf, request_json, select_airports)

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 1, tzinfo=UTC)


def flight(origin="ATL", destination="VPS", start=None, end=None, **extra):
    return {"id": "synthetic-never-persist", "origin": origin, "destination": destination,
            "start": (start or NOW + timedelta(hours=2)).isoformat(),
            "end": (end or NOW + timedelta(hours=3)).isoformat(), "deadhead": False, **extra}


def snapshot(*legs):
    return {"demo": False, "flights": list(legs or [flight()])}


def metar(station="KATL", **extra):
    return {"icaoId": station, "obsTime": (NOW - timedelta(minutes=10)).timestamp(), "temp": 20,
            "wdir": 90, "wspd": 12, "wgst": 18, "visib": "10+", "fltCat": "VFR",
            "clouds": [{"cover": "FEW", "base": 1000}, {"cover": "BKN", "base": 6000}],
            "wxString": "-SHRA BR", "rawOb": "MUST_NOT_PERSIST_RAW_METAR",
            "lat": 33.6367, "lon": -84.4281, "name": "MUST_NOT_PERSIST_STATION_METADATA", **extra}


def period(start=NOW, end=None, **extra):
    return {"timeFrom": start.timestamp(), "timeTo": (end or start + timedelta(hours=6)).timestamp(),
            "timeBec": None, "fcstChange": None, "probability": None,
            "wdir": "VRB", "wspd": 5, "visib": "6+", "wxString": "VCSH",
            "clouds": [{"cover": "OVC", "base": 2500}], **extra}


def taf(station="KATL", **extra):
    return {"icaoId": station, "issueTime": (NOW - timedelta(hours=1)).isoformat(),
            "validTimeFrom": NOW.timestamp(), "validTimeTo": (NOW + timedelta(hours=24)).timestamp(),
            "rawTAF": "MUST_NOT_PERSIST_RAW_TAF", "remarks": "MUST_NOT_PERSIST_REMARKS",
            "fcsts": [period(), period(fcstChange="PROB", probability=30, wxString="-TSRA"),
                      period(fcstChange="TEMPO", visib=2, wxString="BR"),
                      period(start=NOW + timedelta(hours=6), fcstChange="BECMG",
                             timeBec=(NOW + timedelta(hours=7)).timestamp(), wxString="NSW")], **extra}


class ParseTests(unittest.TestCase):
    def report(self, kind, value, now=NOW):
        parser = parse_metar if kind == "metar" else parse_taf
        return _report_status(kind, parser(value, "KATL", NOW), "KATL", now)

    def test_decoded_metar_uses_provider_category_and_ceiling_not_few_clouds(self):
        data = self.report("metar", metar())
        self.assertEqual(data["temperature_f"], 68)
        self.assertEqual(data["wind"], "090 degrees 12 kt, gusts 18 kt")
        self.assertEqual(data["visibility"], ">10 mi")
        self.assertEqual(data["ceiling_ft"], 6000)
        self.assertEqual(data["flight_category"], "VFR")
        self.assertIn("light showers of rain", data["summary"])
        self.assertIn("mist", data["summary"])
        self.assertEqual(data["checked_at"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(data["source_url"], "https://aviationweather.gov/data/api/")

    def test_unknown_values_do_not_become_clear_calm_or_vfr(self):
        data = self.report("metar", metar(wdir=None, wspd=None, wgst=None, temp=True, visib="M1/4",
                                          clouds=[], fltCat="SUPER", wxString="ALIEN"))
        self.assertIsNone(data["temperature_f"])
        self.assertEqual(data["wind"], "Wind not specified")
        self.assertEqual(data["visibility"], "Visibility not specified")
        self.assertIsNone(data["ceiling_ft"])
        self.assertIsNone(data["flight_category"])
        self.assertTrue(data["unknown_weather"])
        self.assertNotIn("ALIEN", json.dumps(data))
        self.assertIn("undecoded", data["summary"])

    def test_explicit_calm_vertical_visibility_and_variable(self):
        calm = self.report("metar", metar(wdir=0, wspd=0, wgst=None, vertVis=200,
                                          clouds=[{"cover": "VV", "base": None}], fltCat="LIFR"))
        self.assertEqual(calm["wind"], "Calm")
        self.assertEqual(calm["ceiling_ft"], 200)
        self.assertEqual(calm["flight_category"], "LIFR")
        self.assertIn("Variable", self.report("metar", metar(wdir="VRB"))["wind"])

    def test_taf_overlapping_probability_temporary_and_becoming_are_preserved(self):
        data = self.report("taf", taf())
        self.assertEqual(len(data["periods"]), 4)
        prob = next(p for p in data["periods"] if p["change"] == "PROB")
        temporary = next(p for p in data["periods"] if p["change"] == "TEMPO")
        becoming = next(p for p in data["periods"] if p["change"] == "BECMG")
        self.assertEqual(prob["probability"], 30)
        self.assertIn("30% probability", prob["summary"])
        self.assertIn("Temporary", temporary["summary"])
        self.assertEqual(prob["start"], temporary["start"])
        self.assertEqual(becoming["becoming_at"], (NOW + timedelta(hours=7)).isoformat())
        self.assertIn("Becoming", becoming["summary"])
        self.assertIn("no significant weather", becoming["summary"])
        self.assertNotIn("rawTAF", json.dumps(data))

    def test_taf_unspecified_fields_do_not_inherit_previous_period_certainty(self):
        data = self.report("taf", taf(fcsts=[period(), period(fcstChange="TEMPO", wdir=None,
                          wspd=None, clouds=[], visib=None, wxString=None)]))
        temporary = next(p for p in data["periods"] if p["change"] == "TEMPO")
        self.assertEqual(temporary["wind"], "Wind not specified")
        self.assertEqual(temporary["cloud_summary"], "Clouds not specified")
        self.assertIsNone(temporary["ceiling_ft"])

    def test_unknown_taf_change_is_explicit(self):
        data = self.report("taf", taf(fcsts=[period(fcstChange="UNSUPPORTED")]))
        self.assertEqual(data["periods"][0]["change"], "UNKNOWN")
        self.assertIn("Unspecified change", data["periods"][0]["summary"])

    def test_taf_labeled_extrema_are_fahrenheit_with_their_own_times(self):
        data = self.report("taf", taf(fcsts=[period(temp=[
            {"validTime": (NOW + timedelta(hours=3)).timestamp(), "surfaceTemp": 24, "maxOrMin": "MAX"},
            {"validTime": (NOW + timedelta(hours=4)).timestamp(), "surfaceTemp": 19, "maxOrMin": "MIN"},
            {"validTime": NOW.timestamp(), "surfaceTemp": 99, "maxOrMin": "MAX"},
            {"validTime": NOW.timestamp(), "surfaceTemp": 20, "maxOrMin": "UNKNOWN"}])]))
        extrema = data["periods"][0]["temperatures"]
        self.assertEqual([(p["kind"], p["temperature_f"]) for p in extrema], [("high", 75.2), ("low", 66.2)])
        self.assertEqual(extrema[0]["at"], (NOW + timedelta(hours=3)).isoformat())
        self.assertIsNone(data["periods"][0]["temperature_f"])

    def test_wrong_station_and_future_invalid_times_rejected(self):
        for parser, value in ((parse_metar, metar("KVPS")), (parse_taf, taf("KVPS")),
                              (parse_metar, metar(obsTime=(NOW + timedelta(minutes=6)).timestamp())),
                              (parse_taf, taf(issueTime=(NOW + timedelta(minutes=6)).isoformat())),
                              (parse_taf, taf(validTimeTo=NOW.timestamp())),
                              (parse_taf, taf(fcsts=[period(timeBec=(NOW + timedelta(days=2)).timestamp())]))):
            with self.subTest(parser=parser.__name__, value=value):
                with self.assertRaises(ValueError):
                    parser(value, "KATL", NOW)

    def test_freshness_boundary_rechecks_reports_without_network(self):
        data = parse_metar(metar(obsTime=NOW.timestamp()), "KATL", NOW)
        self.assertFalse(_report_status("metar", data, "KATL", NOW + timedelta(seconds=7199))["stale"])
        self.assertTrue(_report_status("metar", data, "KATL", NOW + timedelta(seconds=7200))["stale"])
        self.assertTrue(_report_status("metar", data, "KATL", NOW - timedelta(seconds=1))["stale"])
        data = parse_taf(taf(validTimeTo=(NOW + timedelta(hours=6)).timestamp()), "KATL", NOW)
        data["checked_at"] = (NOW + timedelta(hours=6)).isoformat()
        self.assertTrue(_report_status("taf", data, "KATL", NOW + timedelta(hours=6))["stale"])

    def test_cache_conditions_tampering_is_rejected(self):
        for key, value in (("temperature_f", True), ("unknown_weather", None),
                           ("wind_speed_knots", -1), ("visibility_miles", 10000),
                           ("weather_codes", ["RAW SECRET REPORT"]), ("clouds", [{"cover": "ALIEN"}])):
            with self.subTest(key=key):
                data = parse_metar(metar(), "KATL", NOW)
                data["conditions"][key] = value
                with self.assertRaises((ValueError, KeyError)):
                    _report_status("metar", data, "KATL", NOW)


class ContextTests(unittest.TestCase):
    def test_local_day_before_boundary_active_leg_and_exact_icao_mapping(self):
        rows = select_airports(snapshot(flight(start=NOW + timedelta(hours=24), end=NOW + timedelta(hours=25))), NOW)
        self.assertEqual([(r["iata"], r["icao"]) for r in rows], [("ATL", "KATL"), ("VPS", "KVPS")])
        day_after = datetime(2026, 11, 3, tzinfo=UTC)
        self.assertEqual(select_airports(snapshot(flight(start=day_after, end=day_after + timedelta(hours=1))), NOW), [])
        self.assertEqual(len(select_airports(snapshot(flight(start=NOW - timedelta(hours=1),
                                                              end=NOW + timedelta(hours=1))), NOW)), 2)

    def test_central_tomorrow_late_flight_across_fall_dst_is_included(self):
        now = datetime(2026, 10, 31, 17, tzinfo=UTC)  # Noon CDT, before the 25-hour Sunday.
        tomorrow_late = datetime(2026, 11, 2, 5, 30, tzinfo=UTC)  # Sunday 23:30 CST.
        leg = flight(start=tomorrow_late, end=tomorrow_late + timedelta(hours=1))
        self.assertGreater((tomorrow_late - now).total_seconds(), 24 * 3600)
        self.assertEqual(len(select_airports(snapshot(leg), now, timezone="America/Chicago")), 2)
        self.assertEqual(select_airports(snapshot(leg), now), [])  # Already Monday UTC.
        midnight = datetime(2026, 11, 2, 6, tzinfo=UTC)  # Monday 00:00 CST.
        self.assertEqual(select_airports(snapshot(flight(start=midnight, end=midnight + timedelta(hours=1))),
                                         now, timezone="America/Chicago"), [])

    def test_completed_today_context_survives_landing_but_not_next_local_day(self):
        leg = flight(start=NOW - timedelta(hours=6), end=NOW - timedelta(hours=4))
        self.assertEqual(len(select_airports(snapshot(leg), NOW, timezone="America/Chicago")), 2)
        self.assertEqual(select_airports(snapshot(leg), NOW), [])
        self.assertEqual(select_airports(snapshot(leg), datetime(2026, 11, 1, 5, tzinfo=UTC),
                                         timezone="America/Chicago"), [])
        self.assertEqual(select_airports(snapshot(leg), NOW, timezone="Not/AZone"), [])
        overnight = flight(start=NOW - timedelta(hours=27), end=NOW - timedelta(hours=15))
        self.assertEqual(len(select_airports(snapshot(overnight), NOW, timezone="America/Chicago")), 2)

    def test_current_and_next_legs_take_precedence_over_older_completed_airports(self):
        now = datetime(2026, 11, 1, 18, tzinfo=UTC)
        legs = [flight("ATL", "BNA", start=now - timedelta(hours=8), end=now - timedelta(hours=7)),
                flight("PNS", "ECP", start=now - timedelta(hours=6), end=now - timedelta(hours=5)),
                flight("LHR", "CDG", start=now - timedelta(hours=1), end=now + timedelta(hours=1)),
                flight("VPS", "MIA", start=now + timedelta(hours=3), end=now + timedelta(hours=4))]
        self.assertEqual([r["iata"] for r in select_airports(snapshot(*legs), now)], ["LHR", "CDG", "VPS", "MIA"])

    def test_schedule_layover_gap_bounded_and_intervening_deadhead_breaks_it(self):
        now = datetime(2026, 11, 1, 12, tzinfo=UTC)
        inbound = flight(end=now - timedelta(hours=13), start=now - timedelta(hours=14))
        outbound = flight("VPS", "ATL", start=now + timedelta(hours=36), end=now + timedelta(hours=37))
        self.assertEqual(select_airports(snapshot(inbound, outbound), now),
                         [{"iata": "VPS", "icao": "KVPS", "roles": ["layover"]}])
        deadhead = flight("VPS", "BNA", start=now + timedelta(hours=1), end=now + timedelta(hours=2), deadhead=True)
        self.assertEqual(select_airports(snapshot(inbound, deadhead, outbound), now), [])
        far = flight("VPS", "ATL", start=now + timedelta(hours=73), end=now + timedelta(hours=74))
        self.assertEqual(select_airports(snapshot(inbound, far), now), [])

    def test_maximum_four_unknown_station_demo_invalid_and_deadheads(self):
        legs = [flight("ATL", "VPS"), flight("VPS", "BNA"), flight("BNA", "PNS"), flight("PNS", "ECP")]
        self.assertEqual(len(select_airports(snapshot(*legs), NOW)), 4)
        self.assertEqual(select_airports({"demo": True, "flights": legs}, NOW), [])
        self.assertEqual(select_airports(snapshot(flight("ZZZ", "XXX")), NOW), [])
        self.assertEqual(select_airports(snapshot(flight(deadhead=True)), NOW), [])
        self.assertEqual(select_airports(snapshot(flight(start=NOW + timedelta(hours=1), end=NOW)), NOW), [])
        self.assertEqual(select_airports({"demo": False, "flights": None}, NOW), [])


class FeedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.config = {"demo": False, "trip_weather_enabled": True, "timezone": "America/Chicago"}
        self.feed = TripWeatherFeed(self.path, lambda: self.config)
        self.clock = patch("flightwall.trip_weather.time.monotonic", return_value=100).start()
        self.addCleanup(patch.stopall)
        self.child = MagicMock()
        self.child.status.return_value = {"status": "ready", "source": "National Weather Service",
            "source_url": "https://www.weather.gov/", "daily_updated_at": NOW.isoformat(),
            "daily_fetched_at": NOW.isoformat(), "today": {"high": 75, "low": 60},
            "tomorrow": {"high": 70, "low": 55}, "overnight": {"not_for_trip": True}}
        self.child_factory = patch("flightwall.trip_weather.WeatherFeed", return_value=self.child).start()

    def poll(self, responses=None, snap=None, now=NOW):
        if responses is None:
            responses = [[metar("KATL"), metar("KVPS")], [taf("KATL"), taf("KVPS")]]
        with patch("flightwall.trip_weather.request_json", side_effect=responses) as request:
            result = self.feed.poll(snap or snapshot(), now)
        return result, request

    def test_ready_batch_public_station_forecast_and_parsed_private_cache(self):
        state, request = self.poll()
        self.assertEqual(state["status"], "ready")
        self.assertEqual(request.call_count, 2)
        self.assertIn("ids=KATL%2CKVPS&format=json", request.call_args_list[0].args[0])
        self.assertEqual(self.child.poll.call_count, 2)
        first = state["airports"][0]
        self.assertEqual(first["forecast"]["issued_at"], NOW.isoformat())
        self.assertNotIn("overnight", first["forecast"])
        cache = self.path.joinpath("trip-weather.json").read_text()
        for forbidden in ("rawOb", "rawTAF", "MUST_NOT", "synthetic-never-persist", "latitude", "longitude", "\"lat\"", "\"lon\""):
            self.assertNotIn(forbidden, cache)
            self.assertNotIn(forbidden, json.dumps(state))
        self.assertIn("weather_codes", cache)
        # Cache contains only parsed reports, not generated/free-form prose.
        self.assertNotIn("summary", cache)

    def test_feed_passes_configured_zone_for_completed_local_flight_day(self):
        leg = flight(start=NOW - timedelta(hours=6), end=NOW - timedelta(hours=4))
        state, request = self.poll(snap=snapshot(leg))
        self.assertEqual(state["status"], "ready")
        self.assertEqual(request.call_count, 2)

    def test_rate_exact_boundary_status_never_requests_and_target_change(self):
        self.poll()
        self.clock.return_value = 100 + POLL_SECONDS - 1
        _, request = self.poll()
        request.assert_not_called()
        with patch("flightwall.trip_weather.request_json") as request:
            self.feed.status(NOW + timedelta(seconds=50))
            request.assert_not_called()
        self.clock.return_value = 100 + POLL_SECONDS
        _, request = self.poll()
        self.assertEqual(request.call_count, 2)
        _, request = self.poll(snap=snapshot(flight("BNA", "PNS")),
                               responses=[[metar("KBNA"), metar("KPNS")], [taf("KBNA"), taf("KPNS")]])
        self.assertEqual(request.call_count, 2)

    def test_disabled_demo_idle_make_no_requests_no_files_and_hide_cache(self):
        for changes, snap, status in (({"demo": True}, snapshot(), "demo"),
                                      ({"trip_weather_enabled": False}, snapshot(), "off"),
                                      ({}, {"demo": False, "flights": []}, "idle"),
                                      ({}, {"demo": True, "flights": [flight()]}, "idle")):
            with self.subTest(changes=changes):
                self.config = {"demo": False, "trip_weather_enabled": True, **changes}
                state, request = self.poll(snap=snap)
                self.assertEqual(state["status"], status)
                self.assertEqual(state["airports"], [])
                request.assert_not_called()
                self.child.poll.assert_not_called()
        self.assertFalse(self.path.joinpath("trip-weather.json").exists())
        self.config = {"demo": False, "trip_weather_enabled": True}
        self.poll()
        self.config["trip_weather_enabled"] = False
        self.assertEqual(self.feed.status(NOW)["airports"], [])

    def test_offline_ages_cache_without_claiming_fresh_or_erasing_it(self):
        self.poll()
        self.clock.return_value += POLL_SECONDS
        state, _ = self.poll(responses=[OSError("MUST_NOT_EXPOSE_NETWORK_DETAILS"), OSError("offline")],
                             now=NOW + timedelta(seconds=CACHE_MAX_AGE))
        self.assertEqual(state["status"], "stale")
        self.assertTrue(all(r["metar"]["stale"] for r in state["airports"]))
        self.assertNotIn("MUST_NOT", json.dumps(state))
        self.assertIn("dated", state["error"])

    def test_station_mismatch_duplicate_and_one_failed_endpoint_are_partial(self):
        cases = [[[metar("KATL"), metar("KATL")], [taf("KATL"), taf("KVPS")]],
                 [[metar("KATL"), metar("WRNG")], OSError("offline")]]
        for responses in cases:
            with self.subTest(responses=responses):
                self.feed = TripWeatherFeed(self.path, lambda: self.config)
                state, _ = self.poll(responses=responses)
                self.assertEqual(state["status"], "partial")
                self.assertIsNone(state["airports"][1]["metar"])

    def test_disable_while_request_inflight_does_not_save_or_poll_nws(self):
        def request(url):
            self.config["trip_weather_enabled"] = False
            return [metar("KATL"), metar("KVPS")] if "/metar?" in url else [taf("KATL"), taf("KVPS")]
        with patch("flightwall.trip_weather.request_json", side_effect=request) as fetch:
            state = self.feed.poll(snapshot(), NOW)
        self.assertEqual(state["status"], "off")
        self.assertEqual(state["airports"], [])
        self.child.poll.assert_not_called()
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(self.path.joinpath("trip-weather.json").exists())

    def test_reloaded_cache_is_checked_and_corrupt_section_does_not_escape(self):
        self.poll()
        self.feed = TripWeatherFeed(self.path, lambda: self.config)
        with patch("flightwall.trip_weather.request_json", side_effect=OSError("offline")):
            state = self.feed.poll(snapshot(), NOW)
        self.assertEqual(state["status"], "partial")  # Fresh cache retained, failed refresh disclosed.
        self.feed._cache["KATL"]["metar"]["conditions"]["weather_codes"] = ["UNTRUSTED CACHE SECRET"]
        state = self.feed.status(NOW)
        self.assertIsNone(state["airports"][0]["metar"])
        self.assertNotIn("SECRET", json.dumps(state))

    def test_nonconus_does_not_send_nws_point(self):
        state, _ = self.poll(snap=snapshot(flight("LHR", "CDG")), responses=[[], []])
        self.assertEqual([r["iata"] for r in state["airports"]], ["LHR", "CDG"])
        self.assertTrue(all(r["forecast"] is None for r in state["airports"]))
        self.child_factory.assert_not_called()

    def test_raw_or_unrecognized_cache_fields_are_not_reloaded_or_resaved(self):
        self.poll()
        document = json.loads(self.path.joinpath("trip-weather.json").read_text())
        document["stations"]["KATL"]["metar"]["rawOb"] = "MUST_NOT_RETAIN_RAW"
        document["stations"]["KVPS"]["taf"]["periods"][0]["rawTAF"] = "MUST_NOT_RETAIN_RAW"
        self.path.joinpath("trip-weather.json").write_text(json.dumps(document))
        self.feed = TripWeatherFeed(self.path, lambda: self.config)
        state, _ = self.poll(responses=[OSError("offline"), OSError("offline")])
        self.assertIsNone(state["airports"][0]["metar"])
        self.assertIsNone(state["airports"][1]["taf"])
        self.assertNotIn("MUST_NOT", self.path.joinpath("trip-weather.json").read_text())


class RequestTests(unittest.TestCase):
    def test_endpoint_allowlist_and_station_count_no_network_on_invalid_url(self):
        for url in ("http://aviationweather.gov/api/data/metar?ids=KATL&format=json",
                    "https://evil.invalid/api/data/metar?ids=KATL&format=json",
                    "https://aviationweather.gov/api/data/metar?ids=KATL&format=json&raw=true",
                    "https://aviationweather.gov/api/data/taf?ids=KATL,KVPS,KBNA,KPNS,KECP&format=json",
                    "https://aviationweather.gov/api/data/taf?ids=KATL/secret&format=json",
                    "https://aviationweather.gov/api/data/metar?ids=KATL&ids=KVPS&format=json"):
            with self.subTest(url=url), patch("flightwall.trip_weather.urllib.request.build_opener") as opener:
                with self.assertRaises(ValueError):
                    request_json(url)
                opener.assert_not_called()

    def test_response_size_redirect_no_content_and_custom_agent(self):
        url = "https://aviationweather.gov/api/data/metar?ids=KATL&format=json"
        for content, response_url, expected in ((b"", url, []), (b"[]", url, []),
                                                (b"x" * (MAX_RESPONSE + 1), url, None),
                                                (b"[]", "https://other.invalid/", None),
                                                (b"{}", url, None)):
            with self.subTest(length=len(content), response_url=response_url):
                response = MagicMock()
                response.__enter__.return_value = response
                response.geturl.return_value = response_url
                response.read.return_value = content
                opener = MagicMock()
                opener.open.return_value = response
                with patch("flightwall.trip_weather.urllib.request.build_opener", return_value=opener):
                    if expected is None:
                        with self.assertRaises(ValueError):
                            request_json(url)
                    else:
                        self.assertEqual(request_json(url), expected)
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 8)
                self.assertIn("FlightWall/", opener.open.call_args.args[0].get_header("User-agent"))
                if response_url == url:
                    self.assertEqual(response.read.call_args.args, (MAX_RESPONSE + 1,))
                else:
                    response.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
