"""Short admin-only display leases cannot become voice/MCP spending controls."""
import http.client
import json
from unittest.mock import patch

import pytest

from flightwall.backend import Runtime
from flightwall.remote import RemoteServer, SCOPE, SETTING_TYPES, _tools


def test_runtime_display_lease_is_end_exclusive_refreshable_and_other_clears(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    with patch("flightwall.backend.time.monotonic", return_value=100) as clock:
        assert runtime.nearby_view_is_active() is False
        runtime.note_display_activity("nearby")
        assert runtime.nearby_view_activity() == "nearby"
        clock.return_value = 105.999
        assert runtime.nearby_view_is_active() is True
        clock.return_value = 106
        assert runtime.nearby_view_is_active() is False
        runtime.note_display_activity("nearby-paused")
        clock.return_value = 111.999
        assert runtime.nearby_view_is_active() is True
        assert runtime.nearby_view_activity() == "nearby-paused"
        runtime.note_display_activity("other")
        assert runtime.nearby_view_is_active() is False
        clock.return_value = 112
        assert runtime.nearby_view_is_active() is False


def test_runtime_backwards_clock_and_invalid_views_cannot_activate_or_extend_lease(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    with patch("flightwall.backend.time.monotonic", return_value=100) as clock:
        runtime.note_display_activity("nearby")
        clock.return_value = 99.999
        assert runtime.nearby_view_is_active() is False
        clock.return_value = 105
        for value in (None, True, 1, {}, ["nearby"], "NEARBY", "timers", "nearby "):
            with pytest.raises(ValueError):
                runtime.note_display_activity(value)
        clock.return_value = 106
        assert runtime.nearby_view_is_active() is False


@pytest.fixture
def server(tmp_path):
    calls = []
    config = {"public_url": "https://wall.example.invalid", "control_port": 0, "alexa_enabled": True}
    callbacks = {"display_activity": calls.append}
    remote = RemoteServer(tmp_path, lambda: config, callbacks).start()
    try:
        yield remote, calls
    finally:
        remote.stop()


def request(remote, path="/api/v1/display-activity", body=None, token=None, headers=None):
    payload = json.dumps(body) if body is not None else "null"
    headers = {"Content-Type": "application/json", **(headers or {})}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    connection = http.client.HTTPConnection(*remote.address, timeout=5)
    try:
        connection.request("POST", path, payload, headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None
    finally:
        connection.close()


def test_activity_accepts_only_exact_admin_body_and_calls_direct_callback(server):
    remote, calls = server
    admin = remote.api_key_path.read_text().strip()
    assert request(remote, body={"view": "nearby"}, token=admin) == (200, {"ok": True})
    assert request(remote, body={"view": "nearby-paused"}, token=admin) == (200, {"ok": True})
    assert request(remote, body={"view": "other"}, token=admin) == (200, {"ok": True})
    assert calls == ["nearby", "nearby-paused", "other"]
    assert {"display_activity", "nearby_visible", "nearby_fast"}.isdisjoint(SETTING_TYPES)


@pytest.mark.parametrize("body", [
    None, [], {}, {"view": "nearby", "ttl": 999}, {"view": "other", "command": "run"},
    {"view": "home"}, {"view": "NEARBY"}, {"view": True}, {"view": None}, {"view": ["nearby"]},
])
def test_activity_rejects_extra_fields_unknown_views_and_invalid_types(server, body):
    remote, calls = server
    admin = remote.api_key_path.read_text().strip()
    code, result = request(remote, body=body, token=admin)
    assert code == 400 and calls == []
    assert result == {"error": "Invalid request"}


def test_activity_rejects_unauthenticated_alexa_phone_and_delegated_mcp_bearers(server):
    remote, calls = server
    alexa = remote.alexa_token_path.read_text().strip()
    phone = remote.presence.rotate("chris")
    with remote._lock:
        delegated = remote._mint("synthetic-mcp-client", SCOPE)["access_token"]
    for token in (None, "invalid-synthetic-token", alexa, phone, delegated):
        code, result = request(remote, body={"view": "nearby-paused"}, token=token)
        assert code == 401 and result == {"error": "Authentication required"}
    assert calls == []


def test_activity_missing_callback_is_503_and_cross_origin_is_denied(server):
    remote, calls = server
    admin = remote.api_key_path.read_text().strip()
    assert request(remote, body={"view": "nearby"}, token=admin,
                   headers={"Origin": "https://other.example.invalid"})[0] == 403
    remote.callbacks.pop("display_activity")
    assert request(remote, body={"view": "nearby"}, token=admin) == (
        503, {"error": "Display activity is unavailable"})
    assert calls == []


def test_display_activity_is_not_advertised_or_callable_through_mcp(server):
    remote, calls = server
    admin = remote.api_key_path.read_text().strip()
    assert "display_activity" not in {tool["name"] for tool in _tools()}
    for name in ("display_activity", "display-activity", "nearby_visible", "nearby_fast"):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": {"view": "nearby-paused"}}}
        code, value = request(remote, "/mcp", body, admin)
        assert code == 200 and value["result"]["isError"] is True
    assert calls == []


def test_admin_activity_updates_actual_runtime_lease_and_clears_without_persistence(server, tmp_path):
    remote, _ = server
    runtime = Runtime(tmp_path / "runtime", tmp_path / "install")
    remote.callbacks["display_activity"] = runtime.note_display_activity
    admin = remote.api_key_path.read_text().strip()
    with patch("flightwall.backend.time.monotonic", return_value=100) as clock:
        assert request(remote, body={"view": "nearby-paused"}, token=admin)[0] == 200
        assert runtime.nearby_view_activity() == "nearby-paused"
        assert runtime.nearby_view_is_active()
        clock.return_value = 106
        assert runtime.nearby_view_is_active() is False
        assert request(remote, body={"view": "nearby"}, token=admin)[0] == 200
        assert runtime.nearby_view_activity() == "nearby"
        assert request(remote, body={"view": "other"}, token=admin)[0] == 200
        assert runtime.nearby_view_is_active() is False
    assert not (tmp_path / "runtime" / "display-activity.json").exists()
