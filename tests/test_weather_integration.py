"""Opt-in forecast wiring, private locations, and independent poll scheduling."""
from datetime import datetime, timezone
import json
import threading
from unittest.mock import patch

import pytest

from flightwall.backend import Runtime
from flightwall.config import ConfigStore, DEFAULTS, PUBLIC_FIELDS, REMOTE_SETTINGS
from flightwall.household_policy import build_rotation
from flightwall.onboarding import OnboardingRouter, SetupError, WIZARD_SETTINGS
from flightwall.remote import RemoteServer, SETTING_TYPES, _tools


NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)


def test_forecast_coordinates_are_local_only_and_absent_from_public_settings(tmp_path):
    config = ConfigStore(tmp_path)
    assert config.get()["weather_enabled"] is False
    assert config.get()["weather_latitude"] is None
    assert config.get()["weather_longitude"] is None
    config.update({"weather_latitude": 35.25, "weather_longitude": -97.75})
    assert config.get()["weather_latitude"] == 35.25
    for key in ("weather_latitude", "weather_longitude"):
        assert key not in PUBLIC_FIELDS | REMOTE_SETTINGS | WIZARD_SETTINGS
        assert key not in SETTING_TYPES
        assert key not in config.public()
        before = config.path.read_bytes()
        with pytest.raises(ValueError, match="local CLI"):
            config.update({key: 0}, remote=True)
        assert config.path.read_bytes() == before
    assert config.update({"weather_enabled": True}, remote=True)["weather_enabled"] is True


@pytest.mark.parametrize("key,value", [
    ("weather_latitude", 90.1), ("weather_longitude", -180.1),
    ("weather_latitude", True), ("weather_longitude", "12"),
    ("weather_latitude", float("nan")), ("weather_longitude", float("inf")),
    ("nearby_radius_miles", 0), ("nearby_radius_miles", 250.1),
    ("nearby_radius_miles", False), ("nearby_radius_miles", "5"),
    ("nearby_radius_miles", float("nan")), ("nearby_radius_miles", float("inf")),
])
def test_new_location_and_radius_values_fail_without_mutating_config(tmp_path, key, value):
    config = ConfigStore(tmp_path)
    before = config.path.read_bytes()
    with pytest.raises(ValueError):
        config.update({key: value})
    assert config.path.read_bytes() == before


def test_statute_mile_override_does_not_rewrite_legacy_nautical_radius(tmp_path):
    config = ConfigStore(tmp_path)
    config.update({"nearby_radius_nm": 42})
    assert config.get()["nearby_radius_miles"] is None
    for value in (0.1, 5, 250, None):
        result = config.update({"nearby_radius_miles": value}, remote=True)
        assert result["nearby_radius_miles"] == value
        assert ConfigStore(tmp_path).get()["nearby_radius_nm"] == 42
    config.update({"weather_latitude": -90, "weather_longitude": 180})
    config.update({"weather_latitude": None, "weather_longitude": None})


def test_remote_schema_and_setup_accept_toggle_radius_but_reject_weather_coordinates(tmp_path):
    config = ConfigStore(tmp_path)
    callback = lambda settings: config.update(settings, remote=True)
    remote = RemoteServer(tmp_path, config.get, {"update_settings": callback})
    result = remote._invoke("update_settings", {"settings": {"weather_enabled": True, "nearby_radius_miles": 5}})
    assert result["weather_enabled"] is True and result["nearby_radius_miles"] == 5
    with pytest.raises(ValueError, match="Unsupported"):
        remote._invoke("update_settings", {"settings": {"weather_latitude": 35.25}})
    schema = next(tool for tool in _tools() if tool["name"] == "update_settings")
    properties = schema["inputSchema"]["properties"]["settings"]["properties"]
    assert properties["nearby_radius_miles"]["type"] == ["number", "null"]
    assert properties["nearby_radius_miles"]["minimum"] == 0.1
    assert properties["nearby_radius_miles"]["maximum"] == 250
    assert "weather_latitude" not in properties and "weather_longitude" not in properties
    router = OnboardingRouter(tmp_path, config.get, {"setup_save": config.update})
    assert router._save_settings({"settings": {"weather_enabled": False, "nearby_radius_miles": None}}) == {"saved": True}
    with pytest.raises(SetupError, match="supported setup settings"):
        router._save_settings({"settings": {"weather_longitude": -97.75}})
    settings = router._status({"csrf": "synthetic"})["settings"]
    assert settings["weather_enabled"] is False
    assert "weather_latitude" not in settings and "weather_longitude" not in settings


def test_demo_and_disabled_weather_never_fetch_in_runtime_status(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    with patch("flightwall.weather.request_json") as network:
        assert runtime.weather.poll(NOW)["status"] == "demo"
        assert runtime.display_status(NOW)["weather"]["status"] == "demo"
        runtime.config.update({"demo": False})
        assert runtime.weather.poll(NOW)["status"] == "disabled"
        assert runtime.status(NOW)["weather"]["status"] == "disabled"
        runtime.config.update({"weather_enabled": True})
        assert runtime.weather.poll(NOW)["status"] == "unconfigured"
        assert runtime.status(NOW)["weather"]["status"] == "unconfigured"
        network.assert_not_called()


@pytest.mark.parametrize("weather_status", ["unconfigured", "ready", "partial", "stale", "unavailable"])
def test_one_forecast_slot_is_independent_of_presence_and_preserves_flight_focus(weather_status):
    config = {**DEFAULTS, "weather_enabled": True, "guest_mode": True}
    status = {"snapshot": {"demo": False, "flights": []}, "weather": {"status": weather_status},
              "presence": {"chris": {"state": "away", "stale": False}}, "kids": {"state": "unknown"}}
    assert [row["view"] for row in build_rotation(status, config, NOW)].count("weather") == 1
    status["snapshot"]["flights"] = [{"id": "synthetic", "number": "123", "origin": "ATL", "destination": "MEM",
                                      "start": "2026-10-31T17:30:00Z", "end": "2026-10-31T19:00:00Z"}]
    rows = build_rotation(status, config, NOW)
    positions = [i for i, row in enumerate(rows) if row["view"] == "weather"]
    assert len(positions) == 1
    previous = rows[positions[0] - 1]
    assert previous["view"] in {"details", "map"} and previous["duration_seconds"] == 60
    for disabled in ({**config, "weather_enabled": False},):
        assert all(row["view"] != "weather" for row in build_rotation(status, disabled, NOW))
    status["snapshot"]["demo"] = True
    assert all(row["view"] != "weather" for row in build_rotation(status, config, NOW))


def test_slow_weather_poll_does_not_block_calendar_worker_or_status(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    runtime.config.update({"demo": False, "weather_enabled": True,
                           "weather_latitude": 35.25, "weather_longitude": -97.75})
    entered, release, calendar_started = threading.Event(), threading.Event(), threading.Event()

    def slow_poll():
        entered.set()
        release.wait(5)

    def calendar_worker():
        calendar_started.set()
        runtime.stop_event.wait(5)

    try:
        with patch("flightwall.backend.RemoteServer") as remote, patch.object(runtime, "_run", calendar_worker), \
                patch.object(runtime.weather, "poll", slow_poll), patch("flightwall.weather.request_json") as network:
            runtime.start()
            assert entered.wait(2) and calendar_started.wait(2)
            assert runtime._weather_worker is not runtime.worker and runtime._weather_worker.daemon
            status = runtime.status(NOW)
            assert status["weather"]["status"] == "unavailable"
            encoded = json.dumps(status)
            assert "weather_latitude" not in encoded and "weather_longitude" not in encoded
            assert "35.25" not in encoded and "-97.75" not in encoded
            network.assert_not_called()
            release.set()
            runtime.stop()
            remote.return_value.stop.assert_called_once()
            assert not runtime._weather_worker.is_alive()
    finally:
        release.set()
        runtime.stop_event.set()
        if runtime._weather_worker:
            runtime._weather_worker.join(timeout=2)


def test_unexpected_weather_status_error_stays_sanitized_and_keeps_other_views(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    runtime.config.update({"demo": False, "weather_enabled": True})
    with patch.object(runtime.weather, "status", side_effect=ValueError("private coordinate sentinel")):
        status = runtime.status(NOW)
    assert status["weather"]["status"] == "unavailable"
    assert "sentinel" not in json.dumps(status)
    assert {row["view"] for row in build_rotation(status, runtime.config.get(), NOW)} >= {"home", "weather"}


def test_nearby_relative_positions_do_not_reveal_the_home_pin_in_maintenance(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    runtime.config.update({"demo": False, "nearby_enabled": True, "home_latitude": 0,
                           "home_longitude": 0, "nearby_radius_miles": 5})
    response = {"now": NOW.timestamp() * 1000, "ac": [{"hex": "abcdef", "lat": 0.01, "lon": 0,
                                                       "seen_pos": 0, "alt_baro": 12000}]}
    with patch("flightwall.feeds.utcnow", return_value=NOW), \
            patch("flightwall.feeds.request_json", return_value=response):
        runtime.feeds.poll(runtime.config.get(), None)
    tv = runtime.display_status(NOW)
    maintenance = runtime.status(NOW)
    assert len(tv["nearby"]) == len(maintenance["nearby"]) == 1
    absolute = {"latitude", "longitude", "lat", "lon"}
    assert absolute <= set(tv["nearby"][0])
    assert not absolute.intersection(maintenance["nearby"][0])
    for key in ("east_miles", "north_miles", "distance_miles", "observed_at", "hex"):
        assert maintenance["nearby"][0][key] == tv["nearby"][0][key]
    assert not {"home_latitude", "home_longitude"}.intersection(tv["config"])
    assert absolute <= set(runtime.feeds.nearby[0])  # Redaction never mutates the TV/feed cache.


def test_runtime_hides_nearby_response_if_pin_or_radius_changed_during_fetch(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    runtime.config.update({"demo": False, "nearby_enabled": True, "home_latitude": 0,
                           "home_longitude": 0, "nearby_radius_miles": 5})
    response = {"now": NOW.timestamp() * 1000, "ac": [{"hex": "abcdef", "lat": 0.01, "lon": 0,
                                                       "seen_pos": 0, "alt_baro": 12000}]}
    original = runtime.config.get()
    for changed in ({"home_latitude": 1}, {"nearby_radius_miles": 3}):
        runtime.config.update({"home_latitude": 0, "nearby_radius_miles": 5})
        runtime.feeds._last_nearby = -float("inf")

        def old_response(_url):
            runtime.config.update(changed)
            return response

        with patch("flightwall.feeds.utcnow", return_value=NOW), \
                patch("flightwall.feeds.request_json", side_effect=old_response):
            runtime.feeds.poll(original, None)
        assert runtime.display_status(NOW)["nearby"] == []
        assert runtime.status(NOW)["nearby"] == []
