"""Synthetic end-to-end capabilities; no real provider or household data."""
import http.client
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from flightwall.backend import Runtime
from flightwall.remote import RemoteServer
from flightwall.config import validate


@pytest.fixture
def household(tmp_path):
    runtime = Runtime(tmp_path / "data", tmp_path / "install")
    runtime.config.update({"alexa_enabled": True, "timezone": "America/Chicago"})
    callbacks = {
        "get_status": runtime.status, "get_display_status": runtime.display_status,
        "get_timers": runtime.get_timers, "start_timer": runtime.start_timer,
        "cancel_timer": runtime.cancel_timer, "get_shopping_list": runtime.get_shopping_list,
        "add_shopping_item": lambda p: runtime._shopping_edit("add", p),
        "remove_shopping_item": lambda p: runtime._shopping_edit("remove", p),
        "clear_shopping_list": runtime.clear_shopping_list,
        "show_shopping_list": runtime.show_shopping_list,
        "create_flight_watch": runtime.create_flight_watch,
    }
    server = RemoteServer(runtime.data_dir, runtime.config.get, callbacks, port=0).start()
    try:
        yield runtime, server
    finally:
        server.stop()


def request(server, path, token=None, payload=None):
    connection = http.client.HTTPConnection(*server.address, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request("GET" if payload is None else "POST", path,
                           None if payload is None else json.dumps(payload), headers)
        reply = connection.getresponse()
        return reply.status, json.loads(reply.read())
    finally:
        connection.close()


def test_voice_shopping_ticker_persists_and_clear_requires_confirmation(household):
    runtime, server = household
    key = server.alexa_token_path.read_text().strip()
    payload = {"item": "half and half", "request_id": "synthetic-1", "source": "alexa"}
    code, result = request(server, "/api/v1/shopping-list/add", key, payload)
    assert code == 200 and result["changed"]
    assert result["items"][0]["name"] == "half and half"
    assert request(server, "/api/v1/shopping-list/add", key, payload)[0] == 200
    assert len(runtime.display_status()["shopping_list"]["items"]) == 1
    assert request(server, "/api/v1/shopping-list/clear", key, {"confirmed": False})[0] == 400
    assert len(runtime.get_shopping_list()["items"]) == 1
    assert request(server, "/api/v1/shopping-list/show", key, {"visible": False})[1]["visible"] is False
    restored = Runtime(runtime.data_dir, runtime.install_dir)
    assert restored.get_shopping_list()["items"][0]["name"] == "half and half"
    assert restored.get_shopping_list()["visible"] is False
    assert request(server, "/api/v1/shopping-list/clear", key, {"confirmed": True})[1]["items"] == []


def test_voice_timers_reach_display_survive_restart_and_keep_admin_isolation(household):
    runtime, server = household
    key = server.alexa_token_path.read_text().strip()
    payload = {"label": "pasta", "duration_seconds": 360, "request_id": "synthetic-timer", "source": "alexa"}
    code, result = request(server, "/api/v1/timers", key, payload)
    assert code == 200 and result["created"]
    assert request(server, "/api/v1/timers", key, payload)[1]["created"] is False
    assert runtime.display_status()["timers"][0]["label"] == "pasta"
    assert Runtime(runtime.data_dir, runtime.install_dir).get_timers()["timers"][0]["id"] == result["timer"]["id"]
    for path in ["/api/v1/display", "/api/v1/status", "/mcp"]:
        assert request(server, path, key)[0] == 401
    phone = runtime.presence.rotate("lisa")
    for path in ["/api/v1/timers", "/api/v1/shopping-list"]:
        assert request(server, path, phone)[0] == 401
        assert request(server, path)[0] == 401
    assert request(server, "/api/v1/timers/cancel", key, {"id": result["timer"]["id"]})[0] == 200
    assert runtime.get_timers()["timers"] == []


def test_only_accepted_fresh_lisa_reports_reach_disabled_alert_engine(household):
    runtime, _ = household
    runtime.config.update({"presence_enabled": True, "home_latitude": 10.0, "home_longitude": 10.0})
    payload = {"_type": "location", "lat": 10.0, "lon": 10.0, "acc": 5,
               "tst": int(datetime.now(timezone.utc).timestamp())}
    with patch.object(runtime.shopping_alerts, "consume_location") as consume:
        runtime.update_presence("chris", payload)
        assert not consume.called
        runtime.update_presence("lisa", payload)
        consume.assert_called_once_with("lisa", payload)
        runtime.update_presence("lisa", payload)
        assert consume.call_count == 1
        before = runtime.presence.path.read_bytes()
        modified = runtime.presence.path.stat().st_mtime_ns
        for discarded in ({**payload, "tst": payload["tst"] - 1},
                          {**payload, "tst": payload["tst"] - 20000},
                          {"_type": "status", "android": {"loc": 1}},
                          {"_type": "transition", "event": "leave", "desc": "Other region", "tst": payload["tst"]}):
            assert runtime.update_presence("lisa", discarded)["updated"] is False
            assert runtime.presence.path.read_bytes() == before
            assert runtime.presence.path.stat().st_mtime_ns == modified
            assert consume.call_count == 1
    assert runtime.config.get()["shopping_enabled"] is False
    assert "lat" not in json.loads(runtime.presence.path.read_text())["people"]["lisa"]


def test_new_settings_validate_and_sensitive_shopping_setup_stays_local(household):
    runtime, _ = household
    assert validate("display_scale_mode", "fit") == "fit"
    assert validate("shopping_stores", "[]") == "[]"
    with pytest.raises(ValueError):
        validate("display_scale_mode", "stretch")
    with pytest.raises(ValueError):
        runtime.config.update({"shopping_enabled": True}, remote=True)
    assert "shopping_stores" not in runtime.config.public()


def test_public_watch_is_separate_from_roster_and_truthful_when_unconfigured(household):
    runtime, server = household
    key = server.alexa_token_path.read_text().strip()
    before = runtime.snapshot()
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    payload = {"airline": "DL", "flight_number": "123", "date": today,
               "source": "alexa", "request_id": "public-watch-fixture"}
    code, result = request(server, "/api/v1/flight-watches", key, payload)
    assert code == 200 and result["created"]
    assert result["tracking_available"] is False
    assert result["watch"]["ident"] == "DL123"
    assert runtime.snapshot() == before
    assert runtime.display_status()["flight_watches"][0]["status"] == "unconfigured"
    assert request(server, "/api/v1/flight-watches", key, {**payload, "calendar_id": "forbidden"})[0] == 400
