"""Current-flight focus and independent flight feeds use synthetic data only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
from unittest.mock import Mock, patch

import pytest

from flightwall.backend import Runtime
from flightwall.config import DEFAULTS
from flightwall.flight_alerts import normalize_operating_status
from flightwall.household_policy import build_rotation, household_context, recent_vps_arrival

NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)


def flight(**changes):
    return {"id": "own-current", "number": "DL123", "origin": "ATL", "destination": "MEM",
            "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat(),
            "deadhead": False, **changes}


def provider(selected=None, **changes):
    selected = selected or flight()
    raw = {"fa_flight_id": "DAL123-synthetic", "scheduled_out": selected["start"], "scheduled_in": selected["end"],
           "actual_out": (NOW - timedelta(minutes=55)).isoformat(),
           "actual_off": (NOW - timedelta(minutes=45)).isoformat(), **changes}
    return normalize_operating_status(raw, selected, NOW)


def position(selected=None, **changes):
    return {"flight_id": (selected or flight())["id"], "provider_flight_id": "DAL123-synthetic",
            "latitude": 34.1, "longitude": -88.2, "observed_at": NOW.isoformat(), "source": "FlightAware", **changes}


def state(**changes):
    current = flight()
    future = flight(id="next-day", number="DL124", start=(NOW + timedelta(days=1)).isoformat(),
                    end=(NOW + timedelta(days=1, hours=2)).isoformat())
    return {"snapshot": {"demo": False, "flights": [current, future]}, "selected_flight": current,
            "operating_flight": current, "operating_status": provider(), "operating_live": position(),
            "presence": {"chris": {"state": "unknown", "stale": True}}, "kids": {"state": "unknown"},
            "personal_items": [], "on_duty": True, **changes}


def config(**changes):
    return DEFAULTS | {"demo": False, "flightaware_enabled": True, "flight_focus_only": True, **changes}


def runtime(tmp_path, selected=None):
    selected = selected or flight()
    service = Runtime(tmp_path, tmp_path / "install")
    service.config.update({"demo": False, "flightaware_enabled": True, "airline": "DL", "timezone": "America/Chicago"})
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    snapshot = {"demo": False, "flights": [selected], "last_sync": NOW.isoformat()}
    service.snapshot = lambda: deepcopy(snapshot)
    return service, snapshot


def test_strict_current_focus_yields_future_preview_but_keeps_short_dinner_and_global_overlays():
    dinner = {"id": "meal", "kind": "dinner", "display_start": (NOW - timedelta(hours=1)).isoformat(),
              "display_end": (NOW + timedelta(hours=1)).isoformat()}
    status = state(personal_items=[dinner], timers=[{"id": "timer"}], shopping_list={"items": ["olives"]})
    before = deepcopy(status)
    rows = build_rotation(status, config(), NOW)
    views = [row["view"] for row in rows]
    assert {"details", "operating_map", "flight_status", "personal"} <= set(views)
    assert not {"schedule", "trip_summary", "trip_map"}.intersection(views)
    assert all(row["duration_seconds"] == 60 for row in rows if row["view"] in {"details", "operating_map"})
    assert all(row["duration_seconds"] == 15 for row in rows if row["view"] == "personal")
    assert all(row["selected_flight"]["id"] == "own-current" for row in rows if row["view"] in {"details", "operating_map"})
    assert len({row["key"] for row in rows}) == len(rows)
    assert status == before  # Timers/ticker are separate, unchanged global state.


def test_default_policy_retains_schedule_and_future_trip_preview():
    rows = build_rotation(state(), config(flight_focus_only=False), NOW)
    assert {"schedule", "trip_summary", "trip_map"} <= {row["view"] for row in rows}


def test_outside_focus_keeps_the_normal_preview_instead_of_faking_current_flight_context():
    next_flight = flight(start=(NOW + timedelta(hours=5)).isoformat(), end=(NOW + timedelta(hours=7)).isoformat())
    status = state(snapshot={"demo": False, "flights": [next_flight]}, selected_flight=next_flight,
                   operating_flight=None, operating_status={"status": "outside_window"}, operating_live=None)
    assert not household_context(status, config(), NOW)["flight_priority"]
    views = {row["view"] for row in build_rotation(status, config(), NOW)}
    assert {"trip_summary", "trip_map"} <= views
    assert not {"details", "map", "operating_map"}.intersection(views)


@pytest.mark.parametrize("changes", [
    {"flight_id": "next-day"}, {"provider_flight_id": "different"}, {"source": "calendar"},
    {"observed_at": (NOW - timedelta(seconds=301)).isoformat()},
    {"observed_at": (NOW + timedelta(seconds=1)).isoformat()}, {"latitude": True},
])
def test_invalid_or_stale_position_uses_the_ordinary_map_and_keeps_status_warning_reachable(changes):
    rows = build_rotation(state(operating_live=position(**changes)), config(), NOW)
    views = {row["view"] for row in rows}
    assert {"details", "map", "flight_status"} <= views and "operating_map" not in views


def test_strict_mode_holds_a_confirmed_delayed_leg_when_status_stales_but_stays_bounded():
    current = flight(start=(NOW - timedelta(hours=4)).isoformat(), end=(NOW - timedelta(hours=2)).isoformat())
    known = provider(current, actual_out=(NOW - timedelta(hours=3, minutes=55)).isoformat(),
                     actual_off=(NOW - timedelta(hours=3, minutes=45)).isoformat())
    known["fetched_at"] = (NOW - timedelta(minutes=10)).isoformat()
    status = state(snapshot={"demo": False, "flights": [current]}, selected_flight=None,
                   operating_flight=current, operating_status=known, operating_live=None)
    assert household_context(status, config(), NOW)["flight_priority"]
    assert not household_context(status, config(flight_focus_only=False), NOW)["flight_priority"]
    assert not household_context(status, config(), NOW + timedelta(hours=4, seconds=1))["flight_priority"]
    rows = build_rotation(status, config(), NOW)
    assert {"details", "map", "flight_status"} <= {row["view"] for row in rows}
    assert all(row["selected_flight"]["id"] == current["id"] for row in rows if row["view"] in {"details", "map"})


def test_trip_weather_is_per_airport_and_radar_advisories_are_short_context_cards():
    airports = [{"iata": "ATL", "icao": "KATL", "roles": ["origin"]},
                {"iata": "MEM", "icao": "KMEM", "roles": ["destination"]}]
    status = state(trip_weather={"status": "partial", "airports": airports}, national_radar={"status": "ready"},
                   airport_alerts={"status": "ready", "fetched_at": NOW.isoformat(), "issued_at": NOW.isoformat(),
                                   "events": [{"airport": "ATL", "kind": "ground_stop"}]})
    cfg = config(trip_weather_enabled=True, national_radar_enabled=True, airport_alerts_enabled=True)
    rows = build_rotation(status, cfg, NOW)
    trip = [row for row in rows if row["view"] == "trip_weather"]
    assert [row["key"] for row in trip] == ["trip_weather:ATL", "trip_weather:MEM"]
    assert [row["item"]["iata"] for row in trip] == ["ATL", "MEM"]
    assert all(row["duration_seconds"] == 15 for row in rows if row["view"] in {"trip_weather", "national_radar", "airport_alerts"})
    assert {"national_radar", "airport_alerts"} <= {row["view"] for row in rows}
    assert "airport_alerts" not in {row["view"] for row in build_rotation(status, cfg, NOW + timedelta(seconds=600))}
    status["airport_alerts"]["events"] = []
    assert "airport_alerts" not in {row["view"] for row in build_rotation(status, cfg, NOW)}


def test_runtime_keeps_an_operating_marker_separate_from_future_roster_selection(tmp_path):
    current = flight(end=(NOW - timedelta(minutes=1)).isoformat())
    service, snapshot = runtime(tmp_path, current)
    snapshot["flights"].append(flight(id="next-day", start=(NOW + timedelta(hours=2)).isoformat(),
                                      end=(NOW + timedelta(hours=4)).isoformat()))
    service.feeds._set_operating_status(service.config.get(), current, provider(current))
    service.feeds.live = position(current)
    with patch("flightwall.feeds.request_json") as network:
        status = service.display_status(NOW)
        network.assert_not_called()
    assert status["selected_flight"]["id"] == status["operating_live"]["flight_id"] == "own-current"
    assert status["operating_flight"]["id"] == "own-current"
    status["operating_live"]["latitude"] = 0
    assert service.feeds.live["latitude"] == 34.1
    service.feeds.live["provider_flight_id"] = "another-provider"
    assert service.display_status(NOW)["operating_live"] is None


def test_actual_vps_arrival_resumes_local_map_despite_away_without_changing_presence(tmp_path):
    inbound = flight(destination="VPS", end=(NOW + timedelta(minutes=5)).isoformat())
    service, snapshot = runtime(tmp_path, inbound)
    service.config.update({"nearby_enabled": True, "home_latitude": 35.0, "home_longitude": -90.0,
                           "nearby_radius_miles": 5})
    landed = provider(inbound, actual_on=(NOW - timedelta(minutes=1)).isoformat())
    service.feeds._set_operating_status(service.config.get(), inbound, landed)
    nearby = {"hex": "abc123", "observed_at": NOW.isoformat(), "distance_miles": 1,
              "latitude": 35.01, "longitude": -90.01, "east_miles": .5, "north_miles": .5}
    service.feeds._nearby_completed = ((35.0, -90.0, 5), [nearby])
    away = {"chris": {"state": "away", "stale": False}, "lisa": {"state": "unknown", "stale": True}}
    with patch.object(service.presence, "snapshot", return_value=deepcopy(away)):
        status = service.display_status(NOW)
        assert status["presence"] == away
        assert status["household_context"]["recent_vps_arrival"]
        assert not status["household_context"]["flight_priority"]
        assert len(status["nearby"]) == 1
        assert "nearby" in {row["view"] for row in build_rotation(status, service.config.get(), NOW)}
        # A cached confirmed fact may outlive provider freshness; the fixed
        # actual-arrival cutoff is never extended by a new status read.
        aged = service.display_status(NOW + timedelta(minutes=30))
        assert aged["recent_vps_arrival"] == status["recent_vps_arrival"]
        assert recent_vps_arrival(aged, NOW + timedelta(minutes=30))
        expired = service.display_status(NOW + timedelta(minutes=59))
        assert expired["recent_vps_arrival"] is None and expired["nearby"] == []
    assert not list(tmp_path.glob("*arrival*"))


@pytest.mark.parametrize("changes", [
    {"estimated_in": NOW.isoformat(), "actual_on": None}, {"diverted": True},
    {"cancelled": True}, {"actual_on": (NOW + timedelta(minutes=1)).isoformat()},
])
def test_estimates_diversions_termination_or_future_clocks_cannot_grant_arrival_exception(tmp_path, changes):
    inbound = flight(destination="VPS")
    service, snapshot = runtime(tmp_path, inbound)
    status = provider(inbound, **({"actual_on": (NOW - timedelta(minutes=1)).isoformat()} | changes))
    service.feeds._set_operating_status(service.config.get(), inbound, status)
    assert service.display_status(NOW)["recent_vps_arrival"] is None


def test_first_stale_arrival_and_calendar_switch_cannot_create_or_retain_exception(tmp_path):
    inbound = flight(destination="VPS")
    service, snapshot = runtime(tmp_path, inbound)
    known = provider(inbound, actual_on=(NOW - timedelta(minutes=1)).isoformat())
    known["fetched_at"] = (NOW - timedelta(minutes=10)).isoformat()
    service.feeds._set_operating_status(service.config.get(), inbound, known)
    assert service.display_status(NOW)["recent_vps_arrival"] is None
    known["fetched_at"] = NOW.isoformat()
    service.feeds._set_operating_status(service.config.get(), inbound, known)
    assert service.display_status(NOW)["recent_vps_arrival"] is not None
    service.config.update({"calendar_id": "synthetic-other-calendar", "flightaware_enabled": False})
    assert service.display_status(NOW)["recent_vps_arrival"] is None


@pytest.mark.parametrize("arrived, expected_suppression", [(True, False), (False, True)])
def test_background_loop_only_releases_nearby_polling_for_an_actual_recent_vps_arrival(tmp_path, arrived, expected_suppression):
    inbound = flight(destination="VPS", end=(NOW + timedelta(minutes=5)).isoformat())
    service, snapshot = runtime(tmp_path, inbound)
    service.feeds._set_operating_status(service.config.get(), inbound,
                                        provider(inbound, actual_on=(NOW - timedelta(minutes=1)).isoformat() if arrived else None))
    service._mail_worker, service._content_worker = Mock(), Mock()
    service._mail_worker.is_alive.return_value = service._content_worker.is_alive.return_value = True
    away = {"chris": {"state": "away", "stale": False}}
    with patch("flightwall.backend.datetime", wraps=datetime) as clock, patch.object(service, "sync"), \
            patch.object(service.presence, "snapshot", return_value=away), \
            patch.object(service.flight_watches, "list_watches", return_value=[]), \
            patch.object(service.feeds, "poll", side_effect=lambda *args, **kwargs: service.stop_event.set()) as poll:
        clock.now.return_value = NOW
        service._run()
    assert poll.call_count == 1 and poll.call_args.kwargs["on_duty"] is expected_suppression
    assert away["chris"]["state"] == "away"


def test_workers_are_separate_and_slow_flight_feeds_do_not_block_home_status_or_calendar(tmp_path):
    service, snapshot = runtime(tmp_path)
    release = threading.Event()
    entered = {key: threading.Event() for key in ("home", "trip", "environment", "calendar")}
    def slow(key):
        def poll(*args):
            entered[key].set()
            release.wait(5)
        return poll
    try:
        with patch("flightwall.backend.RemoteServer"), patch.object(service, "_run", side_effect=slow("calendar")), \
                patch.object(service.weather, "poll", side_effect=slow("home")), \
                patch.object(service.trip_weather, "poll", side_effect=slow("trip")), \
                patch.object(service.environment, "poll", side_effect=slow("environment")), \
                patch("flightwall.trip_weather.request_json") as awc, \
                patch("flightwall.flight_environment.request_bytes") as environment:
            service.start()
            assert all(event.wait(2) for event in entered.values())
            workers = [service.worker, service._weather_worker, service._trip_weather_worker, service._environment_worker]
            assert len({worker.ident for worker in workers}) == 4 and all(worker.daemon for worker in workers)
            status = service.status(NOW)
            assert {"weather", "trip_weather", "national_radar", "airport_alerts"} <= set(status)
            encoded = json.dumps(status)
            assert not any(key in encoded for key in ("image_path", "image_data", "weather_latitude", "home_longitude"))
            awc.assert_not_called()
            environment.assert_not_called()
            release.set()
            service.stop()
            assert not any(worker.is_alive() for worker in workers)
    finally:
        release.set()
        service.stop()


def test_new_feed_status_errors_are_sanitized_and_do_not_break_other_cards(tmp_path):
    service, snapshot = runtime(tmp_path)
    with patch.object(service.trip_weather, "status", side_effect=ValueError("PRIVATE-COORDINATE")), \
            patch.object(service.environment, "status", side_effect=ValueError("PRIVATE-PATH")):
        result = service.display_status(NOW)
    assert result["trip_weather"]["status"] == "unavailable"
    assert result["national_radar"]["status"] == result["airport_alerts"]["status"] == "unavailable"
    assert "PRIVATE" not in json.dumps(result)
    assert result["selected_flight"]["id"] == "own-current"


def test_snapshot_context_filters_old_airport_status_without_network_or_image_payloads(tmp_path):
    service, snapshot = runtime(tmp_path)
    weather = {"status": "ready", "airports": [{"iata": "ATL", "icao": "KATL"}, {"iata": "LAX", "icao": "KLAX"}]}
    environment = {"radar": {"status": "ready", "image_available": True, "image_sha256": "a" * 64},
                   "airport_alerts": {"status": "ready", "events": [{"airport": "ATL"}, {"airport": "LAX"}]}}
    original = deepcopy((weather, environment))
    with patch.object(service.trip_weather, "status", return_value=weather), \
            patch.object(service.environment, "status", return_value=environment), \
            patch("flightwall.trip_weather.request_json") as network:
        status = service.display_status(NOW)
        assert [row["iata"] for row in status["trip_weather"]["airports"]] == ["ATL"]
        assert [row["airport"] for row in status["airport_alerts"]["events"]] == ["ATL"]
        snapshot["flights"] = []
        empty = service.display_status(NOW)
        assert empty["trip_weather"]["airports"] == empty["airport_alerts"]["events"] == []
        assert empty["national_radar"]["status"] == "outside_window"
        assert not empty["national_radar"]["image_available"] and empty["national_radar"]["image_sha256"] is None
        network.assert_not_called()
    assert (weather, environment) == original


def test_global_radar_context_is_independent_of_operating_weather_station_selection(tmp_path):
    deadhead = flight(deadhead=True, start=(NOW + timedelta(hours=2)).isoformat(),
                      end=(NOW + timedelta(hours=4)).isoformat())
    service, snapshot = runtime(tmp_path, deadhead)
    service.config.update({"show_deadheads": True, "national_radar_enabled": True})
    environment = {"radar": {"status": "ready", "image_available": True, "image_sha256": "a" * 64},
                   "airport_alerts": {"status": "outside_window", "events": []}}
    with patch.object(service.trip_weather, "status", return_value={"status": "idle", "airports": []}), \
            patch.object(service.environment, "status", return_value=environment), \
            patch("flightwall.flight_environment.request_bytes") as network:
        status = service.display_status(NOW)
        assert status["trip_weather"]["airports"] == []
        assert status["national_radar"]["status"] == "ready" and status["national_radar"]["image_available"]
        assert "national_radar" in {row["view"] for row in build_rotation(status, service.config.get(), NOW)}
        service.config.update({"show_deadheads": False})
        hidden = service.display_status(NOW)
        assert hidden["national_radar"]["status"] == "outside_window"
        assert not hidden["national_radar"]["image_available"]
        network.assert_not_called()
