#!/usr/bin/env python3
"""Fail-closed native release gate. Run in Debian 12 with --network none.

Every installation and credential is a disposable synthetic fixture. This is
not a Batocera hardware test and never uses the workstation's household DATA.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import types
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


BASELINE_VERSION = "0.3.0"
BASELINE_SHA256 = "086bf6267e3c96b55de1cfda70ec3adff21c65aff19bb5a525936a70b969de08"
CURRENT_BASELINE_VERSION = "0.4.12"
CURRENT_BASELINE_SHA256 = "aa45ae1e4c114a2ce96a9f37934a19221220a5097912dd161ac398bff1eab039"
MAX_GLIBC = (2, 36)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root):
    root = Path(root).resolve(strict=True)
    result = {}
    for path in sorted(root.rglob("*")):
        target = path.resolve(strict=True)
        require(target.is_relative_to(root), "Bundle contains an external link")
        if path.is_dir():
            require(not path.is_symlink(), "Bundle contains a directory link")
            continue
        require(path.is_file(), "Bundle contains a special file")
        result[path.relative_to(root).as_posix()] = digest(path)
    require(bool(result), "Bundle is empty")
    return result


def verify_baseline(path):
    require(digest(path) == BASELINE_SHA256, "Original 0.3.0 installer SHA-256 does not match the published baseline")


def verify_current_baseline(path):
    require(digest(path) == CURRENT_BASELINE_SHA256,
            "Published 0.4.12 installer SHA-256 does not match the current baseline")


def extract_installer(path, destination, version):
    """Bounded data-only tar extraction, retaining safe internal library links."""
    prefix = "flightwall-batocera-" + version
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        require(len(members) <= 12000, "Installer contains too many entries")
        require(sum(item.size for item in members) <= 800_000_000, "Installer expands beyond the validation limit")
        seen = set()
        for item in members:
            name = PurePosixPath(item.name)
            require(not name.is_absolute() and ".." not in name.parts and "\\" not in item.name
                    and name.parts and name.parts[0] == prefix, "Installer entry escapes its package directory")
            require(item.name not in seen, "Installer contains duplicate entries")
            seen.add(item.name)
            require(item.isfile() or item.isdir() or item.issym() or item.islnk(), "Installer contains a special file")
        archive.extractall(destination, members=members, filter="data")
    package = Path(destination) / prefix
    require((package / "VERSION").read_text().strip() == version, "Installer VERSION mismatch")
    inventory(package / "releases" / version)
    return package


def required_glibc(symbols):
    required = set()
    for line in symbols.splitlines():
        if "*UND*" not in line:
            continue
        for tag in re.findall(r"\bGLIBC_[A-Za-z0-9_.]+", line):
            require(re.fullmatch(r"GLIBC_[0-9]+(?:\.[0-9]+)+", tag) is not None,
                    "ELF requires an unsupported private/new glibc ABI")
            required.add(tuple(map(int, tag[6:].split("."))))
    return required


def audit_elf(release):
    versions, count = set(), 0
    for path in sorted(Path(release).rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            header = stream.read(20)
        if not header.startswith(b"\x7fELF"):
            continue
        require(len(header) == 20 and header[:6] == b"\x7fELF\x02\x01"
                and int.from_bytes(header[18:20], "little") == 62, "Bundle contains a non-x86_64 ELF object")
        result = subprocess.run(["objdump", "-T", str(path)], capture_output=True, text=True, timeout=30)
        require(result.returncode == 0, "An ELF object's imported symbols could not be audited")
        versions.update(required_glibc(result.stdout))
        count += 1
    require(count > 10 and versions, "Frozen native object audit did not find the expected bundle")
    maximum = max(versions)
    require(maximum <= MAX_GLIBC, "Frozen bundle requires glibc newer than 2.36")
    return {"elf_objects": count, "maximum_required_glibc": ".".join(map(str, maximum))}


def normalized_code(code):
    return code.replace(co_filename="<tested-source>", co_consts=tuple(
        normalized_code(value) if isinstance(value, types.CodeType) else value for value in code.co_consts))


def audit_frozen_source(release, source):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(Path(release) / "FlightWall"))
    names = [name for name, entry in archive.toc.items() if entry[-1] == "z"]
    require(len(names) == 1, "Frozen executable must contain one Python module archive")
    modules = archive.open_embedded_archive(names[0])
    require("flightwall_entry" in archive.toc, "Frozen executable is missing its application entry point")
    entry = marshal.loads(archive.extract("flightwall_entry"))
    expected_entry = compile((Path(source) / "flightwall_entry.py").read_bytes(), "flightwall_entry.py", "exec", dont_inherit=True)
    require(normalized_code(entry) == normalized_code(expected_entry), "Frozen entry point differs from tested source")
    checked, source_only = [], []
    for path in sorted((Path(source) / "flightwall").rglob("*.py")):
        relative = path.relative_to(source).with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        name = ".".join(parts)
        # `python -m flightwall` uses this alternative source entry point. The
        # frozen application starts with the separately verified flightwall_entry
        # above, so PyInstaller need not include __main__. If it does, audit it.
        if name == "flightwall.__main__" and name not in modules.toc:
            source_only.append(name)
            continue
        require(name in modules.toc, "Frozen executable is missing an application module: " + name)
        actual = modules.extract(name)
        expected = compile(path.read_bytes(), str(path), "exec", dont_inherit=True, optimize=0)
        require(isinstance(actual, types.CodeType) and normalized_code(actual) == normalized_code(expected),
                "Frozen application code differs from the tested source: " + name)
        checked.append(name)
    assets = inventory(Path(source) / "assets")
    require(inventory(Path(release) / "_internal" / "assets") == assets, "Frozen assets differ from tested source")
    return {"application_modules": checked, "source_only_entrypoints": source_only,
            "asset_files": len(assets)}


def execute(label, args, *, env=None, success=True, timeout=120):
    result = subprocess.run([str(value) for value in args], env=env, capture_output=True, text=True, timeout=timeout)
    if success:
        require(result.returncode == 0, label + " failed (exit " + str(result.returncode) + ")")
    # Never print command output: private synthetic fixtures exercise real CLI
    # paths, and this gate should remain safe if its diagnostics are reused.
    return result


def json_output(result):
    value = json.loads(result.stdout)
    require(isinstance(value, dict), "Native command did not return a JSON object")
    return value


def json_objects(text):
    decoder, result, offset = json.JSONDecoder(), [], 0
    while offset < len(text):
        index = text.find("{", offset)
        if index < 0:
            break
        try:
            value, length = decoder.raw_decode(text[index:])
        except ValueError:
            offset = index + 1
        else:
            if isinstance(value, dict):
                result.append(value)
            offset = index + length
    return result


def private_file(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    path.chmod(0o600)


class Installation:
    def __init__(self, root):
        self.root = Path(root)
        self.install, self.data = self.root / "install", self.root / "private-data"
        self.services, self.ports = self.root / "services", self.root / "ports"
        self.service_file = self.services / "flightwall"
        self.env = {**os.environ, "FLIGHTWALL_INSTALL_DIR": str(self.install), "FLIGHTWALL_DATA_DIR": str(self.data),
                    "FLIGHTWALL_PORTS_DIR": str(self.ports), "FLIGHTWALL_SERVICES_DIR": str(self.services),
                    "FLIGHTWALL_SERVICE_FILE": str(self.service_file), "SDL_VIDEODRIVER": "offscreen",
                    "SDL_AUDIODRIVER": "dummy", "LIBGL_ALWAYS_SOFTWARE": "1", "PYGAME_HIDE_SUPPORT_PROMPT": "1"}
        for name in ("FLIGHTWALL_PACKAGE_VERSION", "PYTHONPATH", "PYTHONHOME", "FLIGHTWALL_DEBUG"):
            self.env.pop(name, None)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base = "http://127.0.0.1:" + str(self.port)
        self.opener = build_opener(ProxyHandler({}))

    def install_package(self, package):
        execute("Isolated installer", ["sh", Path(package) / "install.sh"], env=self.env)

    def app(self, *args, version=None, **kwargs):
        binary = (self.install / "releases" / version if version else self.install / "current") / "FlightWall"
        return execute("Native " + str(args[0]), [binary, "--install-dir", self.install, "--data-dir", self.data, *args], env=self.env, **kwargs)

    def configure_port(self):
        self.app("config", "--set", "control_port", str(self.port))

    def service(self, action, *, success=True):
        return execute("Isolated service " + action, ["sh", self.service_file, action], env=self.env, success=success, timeout=30)

    def request(self, path, payload=None, headers=None):
        raw = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + path, data=raw, headers={"Content-Type": "application/json", **(headers or {})})
        with self.opener.open(request, timeout=3) as response:
            data = response.read(2_000_001)
            require(len(data) <= 2_000_000, "Native HTTP response exceeded validation bounds")
            return data, response.headers

    def wait_health(self, version):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                value = json.loads(self.request("/health")[0])
                if value.get("version") == version:
                    return
            except (OSError, ValueError, URLError):
                pass
            time.sleep(.1)
        raise ValueError("Native background service failed to report the expected version")

    def launch(self, *, opengl=True, success=True):
        log = self.data / "logs" / "launcher.log"
        offset = log.stat().st_size if log.exists() else 0
        result = execute("Stable Ports launcher", ["sh", self.install / "launch.sh", "--windowed", "--frames", "4"],
                         env=self.env, success=success)
        if success:
            with log.open("rb") as stream:
                stream.seek(offset)
                records = json_objects(stream.read().decode("utf-8", errors="replace"))
            require(any(row.get("frames") == 4 and (
                row.get("opengl") is opengl and row.get("shader") is False if "opengl" in row
                else row.get("shader") is opengl  # Pre-0.4.12 executables reported only the shader flag.
            ) for row in records),
                    "Frozen GUI did not render four frames with the required graphics path")
        return result

    def selected(self):
        return (self.install / "current").resolve(strict=True).name

    def state(self):
        return json.loads((self.data / "updates" / "state.json").read_text())

    def stop(self):
        if self.service_file.exists():
            self.service("stop", success=False)


def preserve_fixtures(case):
    """Credentials are deliberately invalid and all integrations stay disabled."""
    case.app("config", "--set", "brightness", "0.47", "--set", "timezone", "America/Chicago",
             "--set", "update_public_key", base64.b64encode(b"Q" * 32).decode())
    fixtures = {
        "google-client.json": {"installed": {"client_id": "synthetic.invalid", "client_secret": "synthetic-only"}},
        "google-token.json": {"fixture": "synthetic-legacy-auth"},
        "accounts/chris/google-client.json": {"installed": {"client_id": "synthetic-chris.invalid"}},
        "accounts/chris/google-token.json": {"fixture": "synthetic-chris-auth"},
        "accounts/lisa/google-client.json": {"installed": {"client_id": "synthetic-lisa.invalid"}},
        "accounts/lisa/google-token.json": {"fixture": "synthetic-lisa-auth"},
        "accounts/chris/notification-token.txt": b"synthetic-notification-chris\n",
        "accounts/lisa/notification-token.txt": b"synthetic-notification-lisa\n",
        "presence/chris.token": b"synthetic-presence-chris\n", "presence/lisa.token": b"synthetic-presence-lisa\n",
        "control-token.txt": b"C" * 64 + b"\n", "alexa-token.txt": b"V" * 64 + b"\n",
        "calendar-cache.json": {"flights": [], "last_sync": None, "source": "synthetic cache", "error": None},
        "content.json": [{"id": "a" * 24, "owner": "lisa", "kind": "inspiration", "title": "Synthetic private artwork",
                          "body": "Preserve this chosen test card", "image_id": "a" * 24}],
        "artwork/" + "a" * 24 + ".png": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1kAAAAASUVORK5CYII="),
        "owner-notes.txt": b"synthetic mutable data outside releases\n",
    }
    for relative, value in fixtures.items():
        private_file(case.data / relative, value)
    day = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    case.app("personal", "add", "--kind", "appointment", "--title", "Synthetic upgrade acceptance",
             "--date", day, "--time", "10:00")
    names = [*fixtures, "config.json", "personal.json"]
    case.ports.mkdir(parents=True, exist_ok=True)
    (case.ports / "unrelated-game.txt").write_bytes(b"synthetic game sentinel")
    return {name: digest(case.data / name) for name in names}


def assert_preserved(case, fixtures, recovery_hash):
    require(all(digest(case.data / name) == expected for name, expected in fixtures.items()),
            "Native installation/launch changed a protected private fixture")
    require((case.install / "update-helper").resolve().name == BASELINE_VERSION,
            "The original pinned recovery helper was replaced")
    require(digest(case.install / "update-helper" / "FlightWall") == recovery_hash, "The pinned recovery executable changed")
    require((case.ports / "unrelated-game.txt").read_bytes() == b"synthetic game sentinel", "An unrelated game changed")
    require(all((case.data / name).stat().st_mode & 0o777 == 0o600 for name in fixtures), "Private fixture modes changed")


def preserve_current_fixtures(case):
    """Create 0.4-owned state through that frozen service, never guessed schemas."""
    auth = {"Authorization": "Bearer " + "C" * 64}
    day = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    requests = (("/api/v1/timers", {"label": "Synthetic upgrade timer", "duration_seconds": 3600,
                                  "source": "manual", "request_id": "native-upgrade-timer"}),
                ("/api/v1/shopping-list/add", {"item": "Synthetic upgrade shopping item", "source": "manual",
                                             "request_id": "native-upgrade-shopping"}),
                ("/api/v1/flight-watches", {"airline": "DL", "flight_number": "123", "date": day,
                                           "source": "manual", "request_id": "native-upgrade-watch"}))
    for route, payload in requests:
        case.request(route, payload, auth)
    return {name: digest(case.data / name)
            for name in ("timers.json", "shopping/flightwall-list.json", "flight-watches.json")}


def verify_setup(case, source):
    """Exercise the same browser origin, session and CSRF contract as setup UI."""
    for route, filename in (("/setup", "index.html"), ("/setup/app.js", "app.js"), ("/setup/style.css", "style.css")):
        require(case.request(route)[0] == (Path(source) / "assets" / "onboarding" / filename).read_bytes(),
                "Native service serves stale/missing setup assets")
    token = (case.data / "control-token.txt").read_text().strip()
    _, headers = case.request("/setup/login", {"token": token}, {"Origin": case.base})
    cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
    status = json.loads(case.request("/setup/status", headers={"Cookie": cookie})[0])
    auth = {"Cookie": cookie, "X-Setup-CSRF": status["csrf"], "Origin": case.base}
    case.request("/setup/settings", {"settings": {"rotation_seconds": 30}}, auth)
    require(json.loads((case.data / "config.json").read_text())["rotation_seconds"] == 30, "Native setup save did not persist")
    case.request("/setup/finish", {}, auth)
    require((case.data / "setup-complete.json").is_file(), "Native setup Finish did not persist")


def fresh_install(package, root, source, version):
    case = Installation(root)
    try:
        case.install_package(package)
        require(case.selected() == version, "Fresh install selected the wrong release")
        require(case.service("status", success=False).returncode != 0, "Installer enabled a background service")
        diagnosis = json_output(case.app("diagnose"))
        require(diagnosis.get("frozen") is True and diagnosis.get("version") == version and diagnosis.get("demo") is True,
                "Fresh executable is not the expected frozen offline DEMO")
        case.configure_port()
        image = case.root / "demo.png"
        result = json_output(case.app("render-preview", "--output", str(image)))
        require(result.get("demo") is True and (result.get("width"), result.get("height")) == (512, 256),
                "Frozen software renderer did not produce the logical DEMO canvas")
        require(image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", image.read_bytes()[16:24]) == (512, 256),
                "Frozen preview PNG dimensions are wrong")
        case.launch(opengl=True)
        software = dict(case.env, SDL_VIDEODRIVER="dummy")
        result = json_output(execute("Frozen software fallback", [case.install / "current" / "FlightWall",
                    "--install-dir", case.install, "--data-dir", case.data, "gui", "--windowed", "--frames", "4"], env=software))
        require(result.get("frames") == 4 and result.get("opengl") is False and result.get("shader") is False,
                "Frozen software fallback failed")
        case.service("start")
        case.wait_health(version)
        verify_setup(case, source)
        case.launch(opengl=True)
        case.wait_health(version)
        return {"frozen_demo": True, "logical_png": [512, 256], "opengl_presentation": True,
                "software_fallback": True, "setup_assets_login_save_finish": True, "service_gui_service": True}
    finally:
        case.stop()


def upgrade_install(old_package, package, root, version, *, rollback=False, current_package=None):
    case = Installation(root)
    try:
        case.install_package(old_package)
        require(case.app("--version").stdout.strip() == BASELINE_VERSION, "Baseline is not the real 0.3.0 executable")
        case.configure_port()
        fixtures = preserve_fixtures(case)
        recovery_hash = digest(case.install / "update-helper" / "FlightWall")
        case.service("start")
        case.wait_health(BASELINE_VERSION)
        previous = BASELINE_VERSION
        if current_package is not None:
            # Reproduce the actual installed history instead of making 0.4.12
            # the recovery helper: original 0.3.0 -> healthy 0.4.12 -> candidate.
            case.install_package(current_package)
            require(case.selected() == BASELINE_VERSION, "Current baseline activated before a safe launch")
            case.wait_health(BASELINE_VERSION)
            case.launch(opengl=True)
            previous = CURRENT_BASELINE_VERSION
            require(case.selected() == previous and case.app("--version").stdout.strip() == previous,
                    "Current baseline is not the real published 0.4.12 executable")
            case.wait_health(previous)
            require(case.state().get("awaiting_health") is False, "Current baseline did not pass startup health")
            assert_preserved(case, fixtures, recovery_hash)
            fixtures.update(preserve_current_fixtures(case))
        case.install_package(package)
        require(case.selected() == previous, "Installer activated the update before a safe Ports launch")
        require(json.loads((case.install / "pending.json").read_text())["version"] == version, "New executable was not staged")
        case.wait_health(previous)
        assert_preserved(case, fixtures, recovery_hash)
        if rollback:
            # Corrupt only this disposable staged copy after a real installation.
            # The old pinned helper must activate it, notice GUI failure, and
            # recover without executing anything from the failed application.
            (case.install / "releases" / version / "FlightWall").write_bytes(b"flightwall-validation-intentional-missing-command\n")
            result = case.launch(success=False)
            require(result.returncode != 0, "Broken staged executable unexpectedly launched")
            require(case.selected() == previous, "Failed startup did not roll back")
            require(case.app("--version").stdout.strip() == previous, "Rollback did not restore the previous real executable")
            case.wait_health(previous)
            state = case.state()
            require(state.get("awaiting_health") is False and version in state.get("failed_versions", []), "Rollback state was not retained")
        else:
            case.launch(opengl=True)
            require(case.selected() == version and case.app("--version").stdout.strip() == version,
                    "Genuine executable upgrade did not activate")
            case.wait_health(version)
            state = case.state()
            require(state.get("awaiting_health") is False, "First rendered frame did not mark the upgrade healthy")
        require(state.get("minimum_version") == version, "Update version floor was lowered")
        require(state.get("previous_version") == previous, "Update recovery history selected the wrong previous release")
        assert_preserved(case, fixtures, recovery_hash)
        return {"from": previous, "to": version, "real_executables": True, "next_launch_only": True,
                "private_fixtures_preserved": len(fixtures), "pinned_recovery_preserved": True,
                "pinned_recovery_version": BASELINE_VERSION,
                "background_service_restored": True, "rollback": rollback, "version_floor_preserved": True}
    finally:
        case.stop()


def verify_report(release, report_path, *, require_current=False):
    report = json.loads(Path(report_path).read_text())
    require(report.get("passed") is True and report.get("baseline_sha256") == BASELINE_SHA256,
            "A successful pinned-baseline native validation report is required")
    require(report.get("bundle_files") == inventory(release), "Frozen bundle changed after native validation")
    require((Path(release) / "VERSION").read_text().strip() == report["version"], "Report version does not match bundle")
    if require_current:
        require(report.get("current_baseline_sha256") == CURRENT_BASELINE_SHA256,
                "The published current baseline must be pinned in its validation report")
        for name, is_rollback in (("upgrade", False), ("rollback", True)):
            result = report.get(name, {})
            require(result.get("from") == CURRENT_BASELINE_VERSION and result.get("to") == report["version"]
                    and result.get("rollback") is is_rollback
                    and result.get("pinned_recovery_version") == BASELINE_VERSION
                    and all(result.get(flag) is True for flag in ("real_executables", "next_launch_only",
                        "pinned_recovery_preserved", "background_service_restored", "version_floor_preserved"))
                    and type(result.get("private_fixtures_preserved")) is int
                    and result["private_fixtures_preserved"] > 0,
                    "Current-version upgrade and rollback acceptance is incomplete")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--current-baseline", type=Path,
                        help="Separately test published 0.4.12 -> candidate with the original 0.3.0 recovery helper")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-report", type=Path)
    parser.add_argument("--verify-current-report", type=Path)
    args = parser.parse_args()
    require(not (args.verify_report and args.verify_current_report), "Choose only one report verification mode")
    if args.verify_report or args.verify_current_report:
        verify_report(args.release, args.verify_report or args.verify_current_report,
                      require_current=bool(args.verify_current_report))
        print("Native validation report matches the exact frozen bundle.")
        return
    require(sys.platform == "linux" and platform.machine() == "x86_64", "Native release validation requires Linux x86_64")
    require(args.installer and args.baseline and args.output, "Installer, original baseline and output report are required")
    require(os.environ.get("FLIGHTWALL_NATIVE_ISOLATED") == "1", "Run this validator in the documented network-isolated container")
    require({path.name for path in Path('/sys/class/net').iterdir()} <= {"lo"},
            "Native fixtures must run with only a loopback network interface")
    version = (args.release / "VERSION").read_text().strip()
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) and tuple(map(int, version.split("."))) > (0, 3, 0),
            "Candidate must be a newer numeric version than 0.3.0")
    verify_baseline(args.baseline)
    if args.current_baseline:
        require(tuple(map(int, version.split("."))) > tuple(map(int, CURRENT_BASELINE_VERSION.split("."))),
                "Candidate must be newer than the current 0.4.12 baseline")
        verify_current_baseline(args.current_baseline)
    report = {"version": version, "baseline_sha256": BASELINE_SHA256, "bundle_files": inventory(args.release)}
    print("Auditing frozen source, assets, architecture and glibc requirements.", flush=True)
    report.update(audit_elf(args.release))
    report.update(audit_frozen_source(args.release, args.source))
    with tempfile.TemporaryDirectory(prefix="flightwall-native-") as temporary:
        root = Path(temporary)
        package = extract_installer(args.installer, root / "candidate", version)
        baseline = extract_installer(args.baseline, root / "baseline", BASELINE_VERSION)
        require(inventory(package / "releases" / version) == report["bundle_files"], "Packaged installer differs from audited frozen bundle")
        current = None
        if args.current_baseline:
            current = extract_installer(args.current_baseline, root / "current-baseline", CURRENT_BASELINE_VERSION)
            report["current_baseline_sha256"] = CURRENT_BASELINE_SHA256
            print("Checking genuine 0.4.12 executable upgrade with original 0.3.0 recovery and private data.", flush=True)
        else:
            print("Checking fresh frozen installation, graphics and local setup.", flush=True)
            report["fresh_install"] = fresh_install(package, root / "fresh", args.source, version)
            print("Checking genuine 0.3.0 executable upgrade and private-data preservation.", flush=True)
        report["upgrade"] = upgrade_install(baseline, package, root / "upgrade", version, current_package=current)
        print("Checking failed-start rollback through the original pinned recovery helper.", flush=True)
        report["rollback"] = upgrade_install(baseline, package, root / "rollback", version,
                                             rollback=True, current_package=current)
    report.update(passed=True, checked_at=datetime.now(timezone.utc).isoformat(),
                  limitations=["Synthetic offline Linux checks; not physical Batocera/TV, Google, phone, paid-provider or live Alexa acceptance."])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("Native release gate passed. Exact bundle inventory recorded.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print("Native release gate failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
