"""Run the real stable shell launcher against isolated release/service fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from flightwall.updater import _atomic_json, _state_path


FAKE_APP = '''#!{python}
import argparse
import os
from pathlib import Path
import sys
from flightwall.updater import activate_pending, mark_healthy, rollback
parser = argparse.ArgumentParser()
parser.add_argument('--install-dir', type=Path)
parser.add_argument('--data-dir', type=Path)
parser.add_argument('command')
args = parser.parse_args()
def event(text):
    with (args.data_dir / 'events').open('a') as handle:
        handle.write(text + '\\n')
def active():
    return (args.install_dir / 'current' / 'VERSION').read_text().strip()
if args.command == 'activate-pending':
    result = activate_pending(args.install_dir, args.data_dir)
    event('activate ' + active())
    raise SystemExit(1 if result['status'] == 'error' else 0)
if args.command == 'rollback-update':
    result = rollback(args.install_dir, args.data_dir)
    event('rollback ' + active())
    raise SystemExit(1 if result['status'] == 'error' else 0)
if args.command == 'gui':
    event('gui ' + active())
    if os.environ.get('FAIL_VERSION') == {version!r}:
        raise SystemExit(7)
    result = mark_healthy(args.install_dir, args.data_dir, version={version!r})
    event('healthy ' + active())
    raise SystemExit(1 if result['status'] == 'error' else 0)
if args.command == 'service':
    event('service ' + active())
    raise SystemExit(0)
if args.command == 'focus':
    event('focus ' + active())
    raise SystemExit(0 if os.environ.get('FOCUS_SUCCEEDS') == '1' else 1)
raise SystemExit(2)
'''

FAKE_SERVICE = '''#!/bin/sh
set -eu
DATA_DIR=$FLIGHTWALL_DATA_DIR
INSTALL_DIR=$FLIGHTWALL_INSTALL_DIR
case "$1" in
status) test -f "$DATA_DIR/service-running" ;;
stop)
    printf 'stop %s\\n' "$(cat "$INSTALL_DIR/current/VERSION")" >> "$DATA_DIR/events"
    if [ "${SERVICE_STOP_FAIL:-0}" = 1 ]; then exit 1; fi
    rm -f "$DATA_DIR/service-running"
    ;;
start)
    printf 'start %s\\n' "$(cat "$INSTALL_DIR/current/VERSION")" >> "$DATA_DIR/events"
    touch "$DATA_DIR/service-running"
    ;;
*) exit 2 ;;
esac
'''


@unittest.skipUnless(os.name == "posix", "Batocera launcher requires POSIX shell and symlinks")
class LaunchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        self.data = self.root / "data"
        self.data.mkdir()
        for version in ("0.1.0", "0.1.1"):
            release = self.install / "releases" / version
            release.mkdir(parents=True)
            (release / "VERSION").write_text(version + "\n")
            executable = release / "FlightWall"
            executable.write_text(FAKE_APP.format(python=sys.executable, version=version))
            executable.chmod(0o755)
        (self.install / "current").symlink_to("releases/0.1.0")
        (self.install / "update-helper").symlink_to("releases/0.1.0")
        pending = {"version": "0.1.1", "sha256": "a" * 64}
        _atomic_json(self.install / "pending.json", pending)
        _atomic_json(self.install / "releases" / "0.1.1" / ".release.json", pending)
        _atomic_json(_state_path(self.data), {"minimum_version": "0.1.1"})
        self.service = self.root / "service"
        self.service.write_text(FAKE_SERVICE)
        self.service.chmod(0o755)
        self.env = dict(os.environ, FLIGHTWALL_INSTALL_DIR=str(self.install), FLIGHTWALL_DATA_DIR=str(self.data),
                        FLIGHTWALL_SERVICE_FILE=str(self.service), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        self.launcher = Path(__file__).resolve().parents[1] / "batocera" / "launch.sh"

    def tearDown(self):
        self.temporary.cleanup()

    def launch(self):
        return subprocess.run(["sh", str(self.launcher)], env=self.env, capture_output=True, text=True, timeout=15)

    def events(self):
        return (self.data / "events").read_text().splitlines()

    def test_service_stops_before_activation_and_restarts_matching_new_gui(self):
        (self.data / "service-running").touch()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["stop 0.1.0", "activate 0.1.1", "gui 0.1.1", "healthy 0.1.1", "start 0.1.1"])
        self.assertFalse((self.data / "gui-launch.lock").exists())
        state = json.loads(_state_path(self.data).read_text())
        self.assertFalse(state["awaiting_health"])

    def test_failed_gui_rolls_back_then_restarts_service_on_previous_release(self):
        (self.data / "service-running").touch()
        self.env["FAIL_VERSION"] = "0.1.1"
        result = self.launch()
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.events(), ["stop 0.1.0", "activate 0.1.1", "gui 0.1.1", "rollback 0.1.0", "start 0.1.0"])
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")

    def test_broken_new_executable_cannot_break_pinned_recovery_helper(self):
        executable = self.install / "releases" / "0.1.1" / "FlightWall"
        executable.write_text("this-command-does-not-exist-flightwall\n")
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), ["activate 0.1.1", "rollback 0.1.0"])
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")

    def test_no_background_service_is_started_unless_previously_running(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["activate 0.1.1", "gui 0.1.1", "healthy 0.1.1"])
        self.assertFalse((self.data / "service-running").exists())

    def test_failed_service_stop_prevents_activation(self):
        (self.data / "service-running").touch()
        self.env["SERVICE_STOP_FAIL"] = "1"
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), ["stop 0.1.0"])
        self.assertTrue((self.install / "pending.json").exists())
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")

    def test_service_runner_never_activates_pending_release(self):
        result = subprocess.run(["sh", str(self.launcher.with_name("service-runner.sh"))], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["service 0.1.0"])
        self.assertTrue((self.install / "pending.json").exists())
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")
        self.assertNotIn("awaiting_health", json.loads(_state_path(self.data).read_text()))

    def test_missing_unconfirmed_current_binary_recovers_before_executable_check(self):
        (self.install / "current").unlink()
        (self.install / "current").symlink_to("releases/0.1.1")
        (self.install / "current" / "FlightWall").unlink()
        _atomic_json(_state_path(self.data), {"minimum_version": "0.1.1", "awaiting_health": True,
                                            "previous_version": "0.1.0", "activated_version": "0.1.1"})
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["activate 0.1.0", "gui 0.1.0", "healthy 0.1.0"])
        self.assertFalse((self.install / "pending.json").exists())

    def test_second_gui_cannot_stop_service_or_activate_release(self):
        lock = self.data / "gui-launch.lock"
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))
        (self.data / "service-running").touch()
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), ["focus 0.1.0"])
        self.assertTrue((self.install / "pending.json").exists())
        self.assertTrue((self.data / "service-running").exists())
        self.assertEqual((lock / "pid").read_text(), str(os.getpid()))

    def test_second_launch_focuses_existing_gui_without_activation_or_service_handoff(self):
        lock = self.data / "gui-launch.lock"
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))
        (self.data / "service-running").touch()
        self.env["FOCUS_SUCCEEDS"] = "1"
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["focus 0.1.0"])
        self.assertTrue((self.install / "pending.json").exists())
        self.assertTrue((self.data / "service-running").exists())
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")
        self.assertEqual((self.install / "update-helper").resolve().name, "0.1.0")
        self.assertEqual((lock / "pid").read_text(), str(os.getpid()))


if __name__ == "__main__":
    unittest.main()
