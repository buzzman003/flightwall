"""Local X11 window/process fixtures never activate a real desktop window."""
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import Mock, patch

import pytest

from flightwall import window_focus
from flightwall.main import main


def process_stat(pid, parent, start):
    fields = ["S", str(parent), *(["0"] * 17), str(start)]
    return f"{pid} (FlightWall display) " + " ".join(fields)


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    install, data, proc = (tmp_path / name for name in ("install", "data", "proc"))
    executable = install / "releases" / "0.4.14" / "FlightWall"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic frozen executable")
    lock = data / "gui-launch.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("100")
    for pid, parent in ((100, 1), (101, 100)):
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "stat").write_text(process_stat(pid, parent, 1234))
    command = [str(executable), "--install-dir", str(install), "--data-dir", str(data), "gui"]
    (proc / "101" / "cmdline").write_bytes("\0".join(command).encode() + b"\0")
    # Represent Linux /proc/exe and current symlinks on hosts without symlink rights.
    resolved = {install / "current" / "FlightWall": executable, proc / "101" / "exe": executable}
    original_resolve = Path.resolve
    def resolve(path, *args, **kwargs):
        return resolved[path] if path in resolved else original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(window_focus, "PROC_ROOT", proc)
    monkeypatch.setattr(window_focus.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0.0")
    monkeypatch.setattr(window_focus.os, "access", lambda path, mode: path == "/usr/bin/wmctrl")
    run = Mock(side_effect=[CompletedProcess([], 0, b"0x00200001 0 101 host Flight Wall\n"),
                            CompletedProcess([], 0, b"")])
    monkeypatch.setattr(window_focus.subprocess, "run", run)
    return install, data, proc, resolved, run


def test_focus_activates_only_the_verified_gui_without_creating_settings(desktop):
    install, data, _, _, run = desktop
    assert window_focus.focus_window(install, data) is True
    assert [call.args[0] for call in run.call_args_list] == [
        ["/usr/bin/wmctrl", "-lp"], ["/usr/bin/wmctrl", "-ia", "0x00200001"]]
    for call in run.call_args_list:
        assert call.kwargs["capture_output"] is True and call.kwargs["timeout"] == 2
        assert call.kwargs["check"] is False and call.kwargs["env"]["DISPLAY"] == ":0.0"
        assert "shell" not in call.kwargs
    assert (data / "gui-launch.lock" / "pid").read_text() == "100"
    assert not (data / "config.json").exists()


@pytest.mark.parametrize("change", ["executable", "service", "data", "parent", "title"])
def test_focus_rejects_an_unrelated_window_or_process(desktop, change):
    install, data, proc, resolved, run = desktop
    if change == "executable":
        resolved[proc / "101" / "exe"] = install / "unrelated-program"
    elif change == "service":
        path = proc / "101" / "cmdline"
        path.write_bytes(path.read_bytes().replace(b"gui\0", b"service\0"))
    elif change == "data":
        path = proc / "101" / "cmdline"
        path.write_bytes(path.read_bytes().replace(str(data).encode(), str(install).encode()))
    elif change == "parent":
        (proc / "101" / "stat").write_text(process_stat(101, 1, 1234))
    else:
        run.side_effect = [CompletedProcess([], 0, b"0x00200001 0 101 host Other window\n")]
    assert window_focus.focus_window(install, data) is False
    run.assert_called_once()


def test_an_exited_unrelated_window_does_not_mask_the_verified_gui(desktop):
    install, data, _, _, run = desktop
    run.side_effect = [CompletedProcess([], 0, b"0x00300002 0 202 host Flight Wall\n"
                                             b"0x00200001 0 101 host Flight Wall\n"),
                       CompletedProcess([], 0, b"")]
    assert window_focus.focus_window(install, data) is True
    assert run.call_args.args[0][-1] == "0x00200001"


def test_changed_pid_identity_is_not_activated(desktop):
    install, data, _, _, run = desktop
    with patch.object(window_focus, "_gui_identity", side_effect=[((101, 1234),), ((101, 5678),)]):
        assert window_focus.focus_window(install, data) is False
    run.assert_called_once()


@pytest.mark.parametrize("display", [None, ":9"])
def test_focus_uses_a_subprocess_display_default_without_changing_environment(desktop, monkeypatch, display):
    install, data, _, _, run = desktop
    if display is None:
        monkeypatch.delenv("DISPLAY")
    else:
        monkeypatch.setenv("DISPLAY", display)
    assert window_focus.focus_window(install, data) is True
    assert run.call_args.kwargs["env"]["DISPLAY"] == (display or ":0.0")
    assert window_focus.os.environ.get("DISPLAY") == display


@pytest.mark.parametrize("failure", ["tool", "timeout", "activation", "multiple"])
def test_missing_desktop_or_failed_activation_returns_failure(desktop, monkeypatch, failure):
    install, data, _, _, run = desktop
    if failure == "tool":
        monkeypatch.setattr(window_focus.os, "access", lambda *_: False)
    elif failure == "timeout":
        run.side_effect = TimeoutExpired("wmctrl", 2)
    elif failure == "activation":
        run.side_effect = [CompletedProcess([], 0, b"0x00200001 0 101 host Flight Wall\n"),
                           CompletedProcess([], 1, b"")]
    else:
        run.side_effect = [CompletedProcess([], 0, b"0x00200001 0 101 host Flight Wall\n"
                                                 b"0x00200002 0 101 host Flight Wall\n")]
    assert window_focus.focus_window(install, data) is False


@pytest.mark.parametrize("focused", [False, True])
def test_focus_cli_returns_a_status_without_loading_or_saving_configuration(tmp_path, focused, capsys):
    with patch.object(window_focus, "focus_window", return_value=focused) as focus, \
            patch("flightwall.main.ConfigStore") as config:
        assert main(["--install-dir", str(tmp_path), "--data-dir", str(tmp_path), "focus"]) == (0 if focused else 1)
        focus.assert_called_once_with(str(tmp_path), str(tmp_path))
        config.assert_not_called()
    assert ('"focused": true' if focused else '"focused": false') in capsys.readouterr().out
