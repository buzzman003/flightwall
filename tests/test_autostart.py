"""Drive the embedded boot helper without a real menu, GUI, or network."""
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def boot(tmp_path):
    script = Path(__file__).resolve().parents[1] / "batocera" / "flightwall_autostart"
    source = script.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    state = tmp_path / "boot"
    state.mkdir()
    port = tmp_path / "ports" / "Flight Wall.sh"
    data = tmp_path / "data"
    namespace = {"__name__": "autostart_fixture"}
    with patch.object(sys, "argv", ["-", str(state), str(port), str(data)]):
        exec(compile(source, str(script), "exec"), namespace)
    namespace["gui_open"] = Mock()
    namespace["game_running"] = Mock(return_value=False)
    namespace["time"] = Mock(wraps=namespace["time"])
    namespace["time"].sleep = Mock()
    namespace["request"] = Mock()
    return namespace


def index(port):
    return 200, json.dumps([{"path": str(port)}]).encode()


def test_boot_launches_exact_port_through_menu_once(boot):
    boot["request"].side_effect = [
        (200, b"[true]"), index(boot["PORT"]), (200, b"[true]"), (200, b""),
        (200, b"[true]"), index(boot["PORT"]), (200, b"[true]"),
    ]
    boot["gui_open"].side_effect = [False, True, False]
    assert boot["launch"]() == 0
    assert boot["launch"]() == 0
    posts = [item for item in boot["request"].call_args_list if item.args[0] == "POST"]
    assert len(posts) == 1
    assert posts[0].args == ("POST", "/launch")
    assert posts[0].kwargs == {"body": bytes(boot["PORT"])}
    assert (boot["STATE"] / "launch-requested").is_file()


def test_uncertain_post_checks_for_gui_without_retrying(boot):
    boot["request"].side_effect = [
        (200, b"[true]"), index(boot["PORT"]), (200, b"[true]"), TimeoutError(),
    ]
    boot["gui_open"].side_effect = [False, True]
    assert boot["launch"]() == 0
    assert sum(item.args[0] == "POST" for item in boot["request"].call_args_list) == 1
    assert (boot["STATE"] / "launch-requested").is_file()


def test_persistently_busy_frontend_fails_without_posting(boot):
    boot["request"].return_value = (201, b"[false]")
    with pytest.raises(ValueError, match="did not become idle"):
        boot["launch"]()
    assert boot["request"].call_count == 20
    assert not (boot["STATE"] / "launch-requested").exists()


def test_transiently_busy_frontend_recovers_before_submitting_one_launch(boot):
    boot["request"].side_effect = [(201, b"[false]"), (200, b"[true]"), index(boot["PORT"]),
                                   (201, b"[false]"), (200, b"[true]"), (200, b"")]
    boot["gui_open"].side_effect = [False, True]
    assert boot["launch"]() == 0
    assert sum(item.args[0] == "POST" for item in boot["request"].call_args_list) == 1
    assert (boot["STATE"] / "verified").is_file()


def test_user_game_wins_during_busy_wait(boot):
    boot["game_running"].side_effect = [False, True]
    boot["request"].return_value = (201, b"[false]")
    assert boot["launch"]() == 0
    assert all(item.args[0] == "GET" for item in boot["request"].call_args_list)
    assert not (boot["STATE"] / "launch-requested").exists()
    assert (boot["STATE"] / "skipped-game").is_file()
    assert not (boot["STATE"] / "verified").exists()


def test_wrong_port_cannot_trigger_autostart(boot):
    boot["request"].side_effect = [(200, b"[true]"), index(boot["PORT"].with_name("Other Flight Wall.sh"))]
    with pytest.raises(ValueError, match="not indexed"):
        boot["launch"]()
    assert all(item.args[0] == "GET" for item in boot["request"].call_args_list)
    assert not (boot["STATE"] / "launch-requested").exists()


def test_stop_marker_prevents_requests(boot):
    (boot["STATE"] / "stopped").touch()
    assert boot["launch"]() == 0
    boot["request"].assert_not_called()
