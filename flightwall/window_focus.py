"""Activate the existing, locally verified X11 display without launching a GUI."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


PROC_ROOT = Path("/proc")
WMCTRL_PATHS = ("/usr/bin/wmctrl", "/bin/wmctrl")


def _process(pid):
    raw = (PROC_ROOT / str(pid) / "stat").read_bytes()
    if len(raw) > 8192:
        raise ValueError("Invalid process metadata")
    fields = raw.rsplit(b")", 1)[1].split()
    return int(fields[1]), int(fields[19])  # Parent PID and Linux start time.


def _gui_identity(pid, owner, install, data, executable):
    directory = PROC_ROOT / str(pid)
    if (directory / "exe").resolve(strict=True) != executable:
        return None
    raw = (directory / "cmdline").read_bytes()
    if not raw or len(raw) > 8192:
        return None
    arguments = [os.fsdecode(value) for value in raw.rstrip(b"\0").split(b"\0")]
    if (len(arguments) < 6 or arguments[1] != "--install-dir"
            or arguments[3] != "--data-dir" or arguments[5] != "gui"
            or Path(arguments[2]).resolve(strict=True) != install
            or Path(arguments[4]).resolve(strict=True) != data):
        return None
    ancestry = []
    current = pid
    for _ in range(16):
        parent, started = _process(current)
        ancestry.append((current, started))
        if current == owner:
            return tuple(ancestry)
        if parent <= 1 or parent == current:
            return None
        current = parent
    return None


def focus_window(install_dir, data_dir):
    """Return true only after the one verified running GUI accepts activation."""
    if not sys.platform.startswith("linux"):
        return False
    wmctrl = next((path for path in WMCTRL_PATHS if os.access(path, os.X_OK)), None)
    if wmctrl is None:
        return False
    environment = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":0.0")
    try:
        install, data = Path(install_dir).resolve(strict=True), Path(data_dir).resolve(strict=True)
        executable = (install / "current" / "FlightWall").resolve(strict=True)
        relative = executable.relative_to(install / "releases")
        if (len(relative.parts) != 2 or relative.name != "FlightWall"
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", relative.parts[0])):
            return False
        owner_text = (data / "gui-launch.lock" / "pid").read_text().strip()
        if not re.fullmatch(r"[1-9][0-9]{0,9}", owner_text):
            return False
        owner = int(owner_text)
        listing = subprocess.run([wmctrl, "-lp"], capture_output=True, timeout=2, check=False, env=environment)
        if listing.returncode or len(listing.stdout) > 131072:
            return False
        matches = []
        for line in listing.stdout.decode("utf-8").splitlines():
            fields = line.split(None, 4)
            if (len(fields) != 5 or fields[4] != "Flight Wall"
                    or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", fields[0])
                    or not re.fullmatch(r"[1-9][0-9]{0,9}", fields[2])):
                continue
            pid = int(fields[2])
            try:
                identity = _gui_identity(pid, owner, install, data, executable)
            except (OSError, ValueError, IndexError):
                continue  # An unrelated window or exited PID is not our display.
            if identity is not None:
                matches.append((fields[0], pid, identity))
        if len(matches) != 1:
            return False
        window, pid, identity = matches[0]
        if _gui_identity(pid, owner, install, data, executable) != identity:
            return False
        return subprocess.run([wmctrl, "-ia", window], capture_output=True, timeout=2, check=False,
                              env=environment).returncode == 0
    except (OSError, ValueError, IndexError, UnicodeError, subprocess.SubprocessError):
        return False
