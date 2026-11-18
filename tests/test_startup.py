"""Cold local-service readiness must not strand the TV at a black window."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import threading
from types import SimpleNamespace
import urllib.error
from unittest.mock import Mock, patch

import pytest

from flightwall import __version__
from flightwall.config import DEFAULTS
from flightwall.display import (backend_connection_refused, entry_render_state,
                                fresh_household_status, transient_backend_error,
                                validate_startup_status, wait_for_startup_status)
from flightwall.main import main
from flightwall.render import demo_snapshot


def status():
    return {"version": __version__, "config": deepcopy(DEFAULTS), "snapshot": demo_snapshot()}


class LoadingScreen:
    gl = None

    def __init__(self, *_args):
        self.frames = []

    def draw(self, frame, _config):
        self.frames.append(frame.copy())


def fake_pygame():
    ticks = [0.0]

    class Clock:
        def tick(self, rate):
            ticks[0] += 1 / rate

    pygame = SimpleNamespace(QUIT=1, KEYDOWN=2, K_ESCAPE=3,
                             event=SimpleNamespace(get=Mock(return_value=[])),
                             time=SimpleNamespace(Clock=Clock))
    return pygame, ticks


def test_startup_timeout_recovers_without_showing_cached_household_data():
    expected, screen = status(), LoadingScreen()
    connection = Mock()
    connection.status.side_effect = [urllib.error.URLError(TimeoutError()), expected]
    pygame, ticks = fake_pygame()
    with patch("flightwall.display.time.monotonic", side_effect=lambda: ticks[0]):
        result = wait_for_startup_status(connection, pygame, screen, DEFAULTS)
    assert result is expected
    assert connection.status.call_count == 2
    assert screen.frames
    assert all(frame.size == (512, 256) for frame in screen.frames)


def test_startup_timeout_has_an_overall_deadline():
    screen, connection = LoadingScreen(), Mock()
    connection.status.side_effect = TimeoutError()
    pygame, ticks = fake_pygame()
    with patch("flightwall.display.time.monotonic", side_effect=lambda: ticks[0]):
        with pytest.raises(TimeoutError, match="did not become ready"):
            wait_for_startup_status(connection, pygame, screen, DEFAULTS, timeout=.15)
    assert 1 <= connection.status.call_count <= 2
    assert .15 <= ticks[0] <= .2


def test_escape_remains_responsive_during_an_inflight_startup_request():
    release, started = threading.Event(), threading.Event()

    def blocked_status():
        started.set()
        release.wait(2)
        return status()

    connection, screen = Mock(), LoadingScreen()
    connection.status.side_effect = blocked_status
    pygame, _ = fake_pygame()
    pygame.event.get.return_value = [SimpleNamespace(type=pygame.KEYDOWN, key=pygame.K_ESCAPE)]
    try:
        assert wait_for_startup_status(connection, pygame, screen, DEFAULTS) is None
        assert started.is_set()
        assert not release.is_set()
        assert not screen.frames
    finally:
        release.set()


@pytest.mark.parametrize("error", [urllib.error.HTTPError("http://127.0.0.1", code, "private error", {}, None)
                                  for code in (401, 403, 500)])
def test_startup_http_failures_are_not_retried(error):
    connection, screen = Mock(), LoadingScreen()
    connection.status.side_effect = error
    pygame, _ = fake_pygame()
    with pytest.raises(urllib.error.HTTPError):
        wait_for_startup_status(connection, pygame, screen, DEFAULTS)
    assert connection.status.call_count == 1
    assert not screen.frames
    assert not transient_backend_error(error)
    assert not backend_connection_refused(error)


@pytest.mark.parametrize("payload", [None, [], {}, {"config": {}, "snapshot": {"flights": []}},
                                     {"config": DEFAULTS, "snapshot": {"flights": "private data"}},
                                     {"config": {**DEFAULTS, "timezone": "invalid/private"},
                                      "snapshot": {"flights": []}}])
def test_invalid_startup_data_fails_without_echoing_payload(payload):
    with pytest.raises(ValueError, match="invalid startup") as error:
        validate_startup_status(payload)
    assert "private" not in str(error.value)


def test_existing_initial_status_is_reused_without_another_request_or_input_consumption():
    connection, screen = Mock(), LoadingScreen()
    pygame, _ = fake_pygame()
    expected = status()
    assert wait_for_startup_status(connection, pygame, screen, DEFAULTS, initial_status=expected) is expected
    connection.status.assert_not_called()
    pygame.event.get.assert_not_called()
    assert not screen.frames


def gui_cli(tmp_path, response):
    with patch("flightwall.display.BackendConnection") as connection, \
            patch("flightwall.backend.Runtime") as runtime, \
            patch("flightwall.display.run_gui", return_value={"frames": 1}) as gui:
        if isinstance(response, BaseException):
            connection.return_value.status.side_effect = response
        else:
            connection.return_value.status.return_value = response
        code = main(["--data-dir", str(tmp_path / "data"), "--install-dir", str(tmp_path / "install"),
                     "gui", "--windowed", "--frames", "1"])
    return code, connection, runtime, gui


def test_slow_existing_backend_does_not_start_a_duplicate_runtime(tmp_path):
    code, _, runtime, gui = gui_cli(tmp_path, urllib.error.URLError(TimeoutError()))
    assert code == 0
    runtime.assert_not_called()
    assert gui.call_args.kwargs["initial_status"] is None


def test_refused_connection_starts_and_stops_one_owned_runtime(tmp_path):
    code, _, runtime, gui = gui_cli(tmp_path, urllib.error.URLError(ConnectionRefusedError()))
    assert code == 0
    runtime.assert_called_once()
    runtime.return_value.start.assert_called_once()
    runtime.return_value.start.return_value.stop.assert_called_once()
    assert gui.call_args.kwargs["initial_status"] is None


@pytest.mark.parametrize("response", [urllib.error.HTTPError("http://127.0.0.1", 401, "private data", {}, None),
                                     {"config": {}, "snapshot": {"flights": []}},
                                     {**status(), "version": "0.0.1"}])
def test_permanent_existing_backend_errors_do_not_start_runtime_or_display(tmp_path, response, capsys):
    code, _, runtime, gui = gui_cli(tmp_path, response)
    assert code == 1
    runtime.assert_not_called()
    gui.assert_not_called()
    assert "private data" not in capsys.readouterr().err


def test_existing_backend_status_is_passed_to_gui_once(tmp_path):
    expected = status()
    code, connection, runtime, gui = gui_cli(tmp_path, expected)
    assert code == 0
    runtime.assert_not_called()
    connection.return_value.status.assert_called_once()
    assert gui.call_args.kwargs["initial_status"] is expected


@pytest.mark.skipif(importlib.util.find_spec("pygame") is None, reason="pygame is optional for renderer tests")
def test_gui_health_is_marked_only_after_recovered_first_card(monkeypatch):
    import pygame
    from flightwall.display import run_gui

    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    screen, connection, healthy = LoadingScreen(), Mock(), []
    connection.status.side_effect = [TimeoutError(), status()]
    with patch("flightwall.display.DisplayScreen", return_value=screen), \
            patch.object(pygame.event, "get", return_value=[]):
        result = run_gui(connection, windowed=True, max_frames=1,
                         on_healthy=lambda: healthy.append(len(screen.frames)))
    assert result["frames"] == 1
    assert len(screen.frames) > 1
    assert healthy == [len(screen.frames)]
    assert not pygame.display.get_init()


def test_outer_nearby_rows_propagate_and_disappear_on_backend_outage():
    now = datetime.now(timezone.utc)
    current = {**status(), "nearby": [], "nearby_outer": [{"flight": "EXAMPLE"}]}
    state = entry_render_state(current, DEFAULTS, {"view": "nearby", "key": "nearby"}, now)
    assert state["nearby_outer"] == current["nearby_outer"]
    stale = fresh_household_status(current, DEFAULTS, now, backend_available=False)
    assert stale["nearby_outer"] == []
    assert entry_render_state(stale, DEFAULTS, {"view": "nearby", "key": "nearby"}, now)["nearby_outer"] == []
