from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flightwall.trip_weather import (TripWeatherFeed, _conditions, _report_status, forecast_category,
                                     forecast_window, parse_taf, preview_airports, trip_preview_eligible,
                                     trip_weather_eligible)
from test_trip_weather import flight, metar, snapshot, taf
from test_flight_context_views import CONFIG, airport, capture


NOW = datetime(2026, 11, 5, 17, tzinfo=timezone.utc)
SETTINGS = CONFIG | {"presence_ttl_minutes": 90, "show_deadheads": True}


def scheduled_state():
    commute = flight("VPS", "ATL", start=NOW + timedelta(hours=20), end=NOW + timedelta(hours=21), deadhead=True)
    duty = flight("ATL", "BNA", start=NOW + timedelta(hours=22), end=NOW + timedelta(hours=23))
    later = flight("BNA", "ATL", start=NOW + timedelta(days=2), end=NOW + timedelta(days=2, hours=1))
    return {"snapshot": snapshot(commute, duty, later), "backend_available": True,
            "presence": {"chris": {"state": "home", "stale": False, "last_seen": NOW.isoformat()}}}


def test_preview_requires_day_before_and_fresh_explicit_home_in_every_projection():
    state = scheduled_state()
    assert trip_preview_eligible(state, SETTINGS, NOW)
    assert trip_preview_eligible(state["snapshot"] | {"presence": state["presence"]}, SETTINGS, NOW)
    for record in ({"state": "away", "stale": False, "last_seen": NOW.isoformat()},
                   {"state": "unknown", "stale": False, "last_seen": NOW.isoformat()},
                   {"state": "home", "stale": True, "last_seen": NOW.isoformat()},
                   {"state": "home", "stale": False},
                   {"state": "home", "stale": False, "last_seen": (NOW - timedelta(minutes=90)).isoformat()}):
        assert not trip_preview_eligible(state | {"presence": {"chris": record}}, SETTINGS, NOW)
    assert not trip_preview_eligible(state | {"backend_available": False}, SETTINGS, NOW)
    assert not trip_preview_eligible(state, SETTINGS | {"presence_enabled": False}, NOW)
    assert not trip_preview_eligible(state, SETTINGS | {"demo": True}, NOW)
    for moment in (NOW - timedelta(days=1), NOW + timedelta(days=1)):
        current = state | {"presence": {"chris": {"state": "home", "stale": False, "last_seen": moment.isoformat()}}}
        assert not trip_preview_eligible(current, SETTINGS, moment)


def test_first_date_includes_only_explicit_timed_commute_and_endpoint_windows():
    state = scheduled_state()
    targets = preview_airports(state["snapshot"], SETTINGS, NOW)
    assert [row["iata"] for row in targets] == ["VPS", "ATL", "BNA"]
    assert [window["kind"] for window in targets[1]["windows"]] == ["arrival", "departure"]
    assert len(targets[0]["windows"]) == 1
    assert targets[0]["windows"][0]["start"] == (NOW + timedelta(hours=19)).isoformat()
    assert targets[0]["windows"][0]["end"] == (NOW + timedelta(hours=21)).isoformat()


def test_fresh_home_after_midnight_enables_only_matched_overnight_weather():
    from flightwall.household_policy import build_rotation
    now = datetime(2027, 1, 13, 8, tzinfo=timezone.utc)
    inbound = flight("MEM", "VPS", start=now - timedelta(hours=5), end=now - timedelta(hours=3))
    outbound = flight("VPS", "ATL", start=now + timedelta(hours=10), end=now + timedelta(hours=11))
    connection = flight("ATL", "BNA", start=now + timedelta(hours=12), end=now + timedelta(hours=13))
    unrelated = flight("ECP", "ORD", start=now + timedelta(hours=15), end=now + timedelta(hours=16))
    schedule = snapshot(inbound, outbound, connection, unrelated) | {"last_sync": now.isoformat()}
    state = {"snapshot": schedule, "trip_weather": {"status": "partial", "airports": [airport(now)]},
             "presence": {"chris": {"state": "home", "stale": False, "last_seen": now.isoformat()}}}
    assert not trip_preview_eligible(state, SETTINGS, now)
    assert trip_weather_eligible(state, SETTINGS, now)
    entries = [row for row in build_rotation(state, SETTINGS, now) if row["view"] == "trip_weather"]
    assert [row["item"]["iata"] for row in entries] == ["VPS", "ATL", "BNA"]
    assert all(row["item"]["vps_overnight"] and row["item"]["windows"] for row in entries)
    assert not trip_weather_eligible(state | {"snapshot": schedule | {"error": "offline"}}, SETTINGS, now)
    for not_home in ("unknown", "away"):
        changed = state | {"presence": {"chris": state["presence"]["chris"] | {"state": not_home}}}
        assert not trip_weather_eligible(changed, SETTINGS, now)


def test_rotation_includes_all_preview_airports_and_pages_visits_only_while_home():
    from flightwall.household_policy import build_rotation
    state = scheduled_state()
    airports = preview_airports(state["snapshot"], SETTINGS, NOW)
    state["trip_weather"] = {"status": "ready", "airports": airports}
    config = SETTINGS | {"trip_weather_enabled": True}
    entries = build_rotation(state, config, NOW)
    assert [row["key"] for row in entries if row["view"] == "trip_weather"] == [
        "trip_weather:VPS:0", "trip_weather:ATL:0", "trip_weather:BNA:0"]
    away = state | {"presence": {"chris": {"state": "away", "stale": False, "last_seen": NOW.isoformat()}}}
    assert "trip_weather" not in {row["view"] for row in build_rotation(away, config, NOW)}
    unsupported = snapshot({"title": "commute tomorrow", "start": "2026-11-06", "end": "2026-11-07"})
    assert not preview_airports(unsupported, SETTINGS, NOW)


def test_preview_uses_calendar_day_across_fall_dst_and_keeps_overnight_arrival():
    now = datetime(2026, 10, 31, 17, tzinfo=timezone.utc)
    start = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)  # Sunday 23:30 CST.
    targets = preview_airports(snapshot(flight(start=start, end=start + timedelta(hours=2))), SETTINGS, now)
    assert len(targets) == 2
    assert targets[1]["windows"][0]["scheduled_at"] == (start + timedelta(hours=2)).isoformat()


def forecast(now=NOW):
    raw = taf(issueTime=(now - timedelta(hours=1)).isoformat(), validTimeFrom=now.timestamp(),
              validTimeTo=(now + timedelta(hours=30)).timestamp(), fcsts=[
        {"timeFrom": now.timestamp(), "timeTo": (now + timedelta(hours=30)).timestamp(),
         "fcstChange": None, "visib": "6+", "clouds": [{"cover": "SCT", "base": 5000}]},
        {"timeFrom": (now + timedelta(hours=20, minutes=30)).timestamp(),
         "timeTo": (now + timedelta(hours=30)).timestamp(), "fcstChange": "FM", "visib": 2,
         "clouds": [{"cover": "OVC", "base": 800}]},
        {"timeFrom": (now + timedelta(hours=20)).timestamp(), "timeTo": (now + timedelta(hours=21)).timestamp(),
         "fcstChange": "PROB", "probability": 30, "visib": 0.5,
         "clouds": [{"cover": "OVC", "base": 300}]}])
    return _report_status("taf", parse_taf(raw, "KATL", now), "KATL", now)


def test_forecast_window_retains_every_change_and_probability_without_extrapolation():
    window = preview_airports(scheduled_state()["snapshot"], SETTINGS, NOW)[0]["windows"][0]
    projected = forecast_window(forecast(), window, NOW)
    assert projected["coverage"] == "full"
    assert {row["category"] for row in projected["segments"]} == {"VFR", "IFR"}
    assert any(row["start"] == (NOW + timedelta(hours=20, minutes=30)).isoformat() for row in projected["segments"])
    assert projected["alternatives"] == [{"change": "PROB", "probability": 30, "category": "LIFR"}]
    for field, value in (("valid_end", NOW.isoformat()), ("stale", True), ("checked_at", (NOW - timedelta(hours=2)).isoformat())):
        assert forecast_window(forecast() | {field: value}, window, NOW)["coverage"] == "unavailable"
    outside = window | {key: (datetime.fromisoformat(value) + timedelta(days=2)).isoformat()
                        for key, value in window.items() if key != "kind"}
    assert forecast_window(forecast(), outside, NOW)["coverage"] == "unavailable"


def test_categories_follow_ceiling_visibility_boundaries_and_unknowns():
    assert forecast_category(_conditions({"visib": "6+", "clouds": [{"cover": "SCT", "base": 3000}]})) == "VFR"
    for ceiling, category in ((3000, "MVFR"), (1000, "MVFR"), (999, "IFR"), (500, "IFR"), (499, "LIFR")):
        assert forecast_category(_conditions({"visib": "6+", "clouds": [{"cover": "BKN", "base": ceiling}]})) == category
    assert forecast_category(_conditions({"visib": 2, "clouds": []})) is None
    assert forecast_category(_conditions({"visib": "3+", "clouds": [{"cover": "CLR"}]})) is None
    assert forecast_category(_conditions({"visib": 0.5, "clouds": []})) == "LIFR"


def test_graphical_windows_render_and_disappear_when_presence_expires():
    source = scheduled_state()
    state = source["snapshot"] | {"view": "trip_weather", "now": NOW.isoformat(), "presence": source["presence"]}
    card = airport(NOW) | preview_airports(source["snapshot"], SETTINGS, NOW)[1] | {"taf": forecast()}
    state["trip_weather"] = {"airports": [card]}
    labels, image = capture(state, SETTINGS)
    assert "FORECAST AT SCHEDULED ARRIVAL / DEPARTURE" in labels
    assert any("ARRIVAL / " in label for label in labels) and any("DEPARTURE / " in label for label in labels)
    assert any("POSSIBLE 30%" in label for label in labels)
    assert (image.size, image.mode) == ((512, 256), "RGB")
    hidden, _ = capture(state | {"presence": {"chris": {"state": "away", "stale": False, "last_seen": NOW.isoformat()}}}, SETTINGS)
    assert "FORECAST AT SCHEDULED ARRIVAL / DEPARTURE" not in hidden


def test_nws_day_fallback_is_labeled_separately_from_unknown_timed_taf():
    source = scheduled_state()
    state = source["snapshot"] | {"view": "trip_weather", "now": NOW.isoformat(), "presence": source["presence"]}
    card = airport(NOW) | preview_airports(source["snapshot"], SETTINGS, NOW)[1]
    card["taf"] = None
    state["trip_weather"] = {"airports": [card]}
    labels, _ = capture(state, SETTINGS)
    assert any(label.startswith("NWS DAY / ") for label in labels)
    assert "TAF NOT YET AVAILABLE FOR THIS WINDOW" in labels
    assert any(label.startswith("NWS DAY ISSUED ") for label in labels)
    card["forecast"]["checked_at"] = (NOW - timedelta(hours=2)).isoformat()
    stale, _ = capture(state, SETTINGS)
    assert not any(label.startswith("NWS DAY / ") for label in stale)


def test_preview_feed_adds_explicit_deadhead_targets_without_caching_schedule(tmp_path):
    config = SETTINGS | {"trip_weather_enabled": True}
    feed = TripWeatherFeed(tmp_path, lambda: config)
    source = snapshot(scheduled_state()["snapshot"]["flights"][0])
    def reply(url):
        stations = ("KVPS", "KATL")
        if "/metar?" in url:
            return [metar(station=station, obsTime=NOW.timestamp()) for station in stations]
        return []
    with patch("flightwall.trip_weather.request_json", side_effect=reply), patch.object(feed, "_forecast", return_value=None):
        result = feed.poll(source, NOW, preview=True)
    assert [row["iata"] for row in result["airports"]] == ["VPS", "ATL"]
    assert all(row["windows"] for row in result["airports"])
    cached = feed.path.read_text()
    assert "scheduled_at" not in cached and "synthetic-never-persist" not in cached
