"""Verify Flight Wall window ownership before local X11 activation or recovery."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import time


PROC_ROOT = Path("/proc")
WMCTRL_PATHS = ("/usr/bin/wmctrl", "/bin/wmctrl")
XPROP_PATHS = ("/usr/bin/xprop", "/bin/xprop")
PID_PATTERN = r"[1-9][0-9]{0,9}"
WINDOW_PATTERN = r"0x[0-9a-fA-F]{1,16}"
MENU_EXECUTABLES = ("/usr/bin/emulationstation", "/usr/bin/emulationstation/emulationstation")


def _process(pid):
    with (PROC_ROOT / str(pid) / "stat").open("rb") as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError("Invalid process metadata")
    fields = raw.rsplit(b")", 1)[1].split()
    return int(fields[1]), int(fields[19])  # Parent PID and Linux start time.


def _arguments(pid):
    with (PROC_ROOT / str(pid) / "cmdline").open("rb") as source:
        raw = source.read(8193)
    if not raw or len(raw) > 8192:
        return []
    return [os.fsdecode(value) for value in raw.rstrip(b"\0").split(b"\0")]


def _read_lock(data):
    lock = data / "gui-launch.lock"
    if lock.is_symlink():
        raise ValueError("Invalid launcher lock")
    with (lock / "pid").open() as source:
        owner = source.read(64).strip()
    if not re.fullmatch(PID_PATTERN, owner):
        raise ValueError("Invalid launcher PID")
    try:
        with (lock / "started").open() as source:
            started = source.read(64).strip()
        if not re.fullmatch(r"[0-9]{1,20}", started):
            raise ValueError("Invalid launcher start time")
    except FileNotFoundError:
        started = None  # The previous stable launcher only stored the PID.
    return int(owner), int(started) if started is not None else None


def _launcher_identity(install, data):
    owner, stamp = _read_lock(data)
    _, started = _process(owner)
    if stamp is not None and started != stamp:
        return None
    arguments = _arguments(owner)
    if (len(arguments) < 2 or Path(arguments[0]).name not in {"sh", "dash", "bash", "busybox"}
            or Path(arguments[1]).resolve(strict=True) != install / "launch.sh"):
        return None
    return owner, started


def _release_executable(executable, install):
    relative = executable.relative_to(install / "releases")
    return (len(relative.parts) == 2 and relative.name == "FlightWall"
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", relative.parts[0]) is not None)


def _gui_process(pid, install, data, executable=None):
    actual = (PROC_ROOT / str(pid) / "exe").resolve(strict=True)
    if actual != executable and (executable is not None or not _release_executable(actual, install)):
        return False
    arguments = _arguments(pid)
    return (len(arguments) >= 6 and arguments[1] == "--install-dir"
            and arguments[3] == "--data-dir" and arguments[5] == "gui"
            and Path(arguments[2]).resolve(strict=True) == install
            and Path(arguments[4]).resolve(strict=True) == data)


def _gui_identity(pid, owner, install, data, executable):
    if not _gui_process(pid, install, data, executable):
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


def _tool(paths):
    return next((path for path in paths if os.access(path, os.X_OK)), None)


class _Desktop:
    def __init__(self, install_dir, data_dir):
        self.install = Path(install_dir).resolve(strict=True)
        self.data = Path(data_dir).resolve(strict=True)
        self.executable = (self.install / "current" / "FlightWall").resolve(strict=True)
        if not _release_executable(self.executable, self.install):
            raise ValueError("Invalid active Flight Wall executable")
        self.wmctrl, self.xprop = _tool(WMCTRL_PATHS), _tool(XPROP_PATHS)
        if not self.wmctrl or not self.xprop:
            raise ValueError("X11 focus tools are unavailable")
        self.environment = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":0.0")

    def run(self, tool, *args):
        result = subprocess.run([tool, *args], capture_output=True, timeout=2,
                                check=False, env=self.environment)
        return result.stdout if result.returncode == 0 and len(result.stdout) <= 131072 else None

    def windows(self):
        rows = []
        for line in (self.run(self.wmctrl, "-lp") or b"").decode("utf-8", errors="replace").splitlines():
            fields = line.split(None, 4)
            if len(fields) == 5 and re.fullmatch(WINDOW_PATTERN, fields[0]) and re.fullmatch(PID_PATTERN, fields[2]):
                rows.append((fields[0], int(fields[2]), fields[4]))
        return rows

    def active(self):
        output = self.run(self.xprop, "-root", "_NET_ACTIVE_WINDOW") or b""
        match = re.fullmatch(rb"_NET_ACTIVE_WINDOW\(WINDOW\): window id # (0x[0-9a-fA-F]{1,16})\s*", output)
        return int(match[1], 16) if match else None

    def gui(self, rows, owner):
        matches = []
        for window, pid, title in rows:
            if title != "Flight Wall":
                continue
            try:
                identity = _gui_identity(pid, owner[0], self.install, self.data, self.executable)
                if identity is not None and identity[-1] == owner:
                    matches.append((window, pid, identity))
            except (OSError, ValueError, IndexError):
                continue
        return matches[0] if len(matches) == 1 else None

    def matches(self, target, owner):
        _, pid, identity = target
        return (_launcher_identity(self.install, self.data) == owner
                and _gui_identity(pid, owner[0], self.install, self.data, self.executable) == identity)

    def menu(self, rows, active):
        if not active:
            return False
        for window, pid, _ in rows:
            if int(window, 16) == active:
                try:
                    executable = (PROC_ROOT / str(pid) / "exe").resolve(strict=True)
                    return executable in tuple(Path(path) for path in MENU_EXECUTABLES)
                except OSError:
                    return False
        return False

    def raise_gui(self, target, owner, *, attempts=4, menu_only=False):
        window = target[0]
        for _ in range(attempts):
            if not self.matches(target, owner):
                return False
            if menu_only:
                active = self.active()
                if active == int(window, 16):
                    return True
                if not self.menu(self.windows(), active):
                    return False
            if self.run(self.wmctrl, "-ia", window) is None:
                return False
            time.sleep(0.2)
            if self.active() == int(window, 16) and self.matches(target, owner):
                return True
        return False


FOCUS_ERRORS = (OSError, ValueError, IndexError, UnicodeError, subprocess.SubprocessError)


def focus_window(install_dir, data_dir):
    """Return success only when X11 confirms the verified window is active."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        desktop = _Desktop(install_dir, data_dir)
        owner = _launcher_identity(desktop.install, desktop.data)
        target = desktop.gui(desktop.windows(), owner) if owner else None
        return bool(target and desktop.raise_gui(target, owner))
    except FOCUS_ERRORS:
        return False


def guard_foreground(install_dir, data_dir, stopped):
    """While this Ports GUI runs, reclaim its window when the menu covers it."""
    if not sys.platform.startswith("linux"):
        return
    try:
        desktop = _Desktop(install_dir, data_dir)
        owner = _launcher_identity(desktop.install, desktop.data)
        pid = os.getpid()
        if not owner or not _gui_identity(pid, owner[0], desktop.install, desktop.data, desktop.executable):
            return
    except FOCUS_ERRORS:
        return
    target = None
    while not stopped.wait(1):
        try:
            active = desktop.active()
            if not active or target and active == int(target[0], 16):
                continue
            rows = desktop.windows()
            target = desktop.gui(rows, owner)
            if target and target[1] == pid and desktop.menu(rows, active) and not stopped.is_set():
                desktop.raise_gui(target, owner, attempts=1, menu_only=True)
        except FOCUS_ERRORS:
            continue


def _stale_lock(install, data):
    """Reclaim only a proven orphan lock with no matching GUI process."""
    lock = data / "gui-launch.lock"
    owner, stamp = _read_lock(data)
    try:
        _, current_stamp = _process(owner)
    except FileNotFoundError:
        pass
    else:
        if stamp is None or stamp == current_stamp:
            return False
    reclaim = lock / "reclaim"
    reclaim.mkdir(mode=0o700)
    try:
        if _read_lock(data) != (owner, stamp) or (install / ".install-lock").exists():
            return False
        if {path.name for path in lock.iterdir()} - {"pid", "started", "reclaim"}:
            return False
        for proc in PROC_ROOT.iterdir():
            if not proc.name.isdecimal():
                continue
            try:
                if _gui_process(int(proc.name), install, data):
                    return False
            except (FileNotFoundError, ProcessLookupError, ValueError):
                continue
        (lock / "pid").unlink()
        (lock / "started").unlink(missing_ok=True)
    finally:
        reclaim.rmdir()
    lock.rmdir()
    return True


def reopen_from_ports(install_dir, data_dir):
    """Resolve a duplicate selection, or return permission to retry acquisition."""
    if not sys.platform.startswith("linux"):
        return "unavailable"
    install, data = Path(install_dir), Path(data_dir)
    try:
        install, data = install.resolve(strict=True), data.resolve(strict=True)
        if (install / ".install-lock").exists():
            return "unavailable"
        lock = data / "gui-launch.lock"
        if not lock.exists():
            return "released"
        if _stale_lock(install, data):
            return "released"
        desktop = _Desktop(install, data)
        owner = _launcher_identity(install, data)
        if owner is None:
            return "unavailable"
        for _ in range(5):
            if not lock.exists():
                return "released"
            target = desktop.gui(desktop.windows(), owner)
            if target:
                break
            time.sleep(0.4)
        if not target:
            return "unavailable"
        if desktop.raise_gui(target, owner, attempts=8, menu_only=True):
            return "focused"
        # Request an ordinary window close only when the verified menu still
        # covers the same GUI after repeated activation attempts.
        rows = desktop.windows()
        if ((install / ".install-lock").exists() or not desktop.menu(rows, desktop.active())
                or desktop.gui(rows, owner) != target or not desktop.matches(target, owner)):
            return "unavailable"
        if desktop.run(desktop.wmctrl, "-ic", target[0]) is None:
            return "unavailable"
        for _ in range(20):
            if not lock.exists():
                return "released"
            time.sleep(0.2)
    except FOCUS_ERRORS:
        pass
    return "unavailable"
