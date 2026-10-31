"""Signed, staged application releases. Configuration and credentials stay outside releases.

The updater deliberately has no command execution facility. It trusts one locally pinned
Ed25519 public key, downloads a signed HTTPS release, and changes ``current`` only when
the stable launcher explicitly calls :func:`activate_pending` between GUI sessions.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

try:
    import fcntl
except ImportError:  # The release-building helper also imports this module on Windows.
    fcntl = None


MAX_MANIFEST_BYTES = 32 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 1536 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
MAX_FILE_BYTES = 512 * 1024 * 1024
MANIFEST_FIELDS = frozenset({"version", "url", "sha256", "size", "signature"})
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\Z")


class UpdateError(ValueError):
    """A release failed a validation or lifecycle check."""


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise UpdateError("Release versions must use three numeric components, such as 0.1.1.")
    return tuple(int(part) for part in value.split("."))


def canonical_manifest(manifest: Mapping[str, Any]) -> bytes:
    """Bytes signed by the release tool; the signature itself is excluded."""
    fields = {key: value for key, value in manifest.items() if key != "signature"}
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def validate_https_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise UpdateError("Update URLs must be valid HTTPS URLs.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("Update URLs must be valid HTTPS URLs.") from exc
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or port == 0):
        raise UpdateError("Update URLs require HTTPS, without embedded credentials or fragments.")
    return url


class HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    """Public downloads may redirect at most five times, always to HTTPS."""
    max_repeats = 2
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_https_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # No authentication headers are used by this updater, including redirects.
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


def _download(url: str, target, limit: int, expected_size: int | None = None) -> int:
    validate_https_url(url)
    request = Request(url, headers={"User-Agent": "FlightWall-Updater/1", "Accept": "application/octet-stream"})
    opener = build_opener(HTTPSOnlyRedirectHandler())
    count = 0
    with opener.open(request, timeout=30) as response:
        validate_https_url(response.geturl())
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise UpdateError("The update server supplied an invalid content length.") from exc
            if declared_size < 0 or declared_size > limit:
                raise UpdateError("The update download exceeds its permitted size.")
            if expected_size is not None and declared_size != expected_size:
                raise UpdateError("The update download size does not match the signed manifest.")
        while True:
            block = response.read(min(1024 * 1024, limit - count + 1))
            if not block:
                break
            count += len(block)
            if count > limit:
                raise UpdateError("The update download exceeds its permitted size.")
            target.write(block)
    if expected_size is not None and count != expected_size:
        raise UpdateError("The update download size does not match the signed manifest.")
    return count


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("Update metadata contains duplicate fields.")
        result[key] = value
    return result


def _json_read(path: Path, default=None):
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise UpdateError("Update metadata is too large.")
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except FileNotFoundError:
        return {} if default is None else default
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Update metadata is not valid JSON.") from exc


def _atomic_json(path: Path, value: Mapping[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _locked(install_dir: Path):
    install_dir.mkdir(parents=True, exist_ok=True)
    with (install_dir / ".update.lock").open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_public_key(value: str, data_dir: Path) -> Ed25519PublicKey:
    """Accept a raw base64 public key, or a locally provisioned PEM file path."""
    if not isinstance(value, str) or not value:
        raise UpdateError("A locally pinned update public key is required.")
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) == 32:
            return Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError):
        pass
    if "://" in value or "\n" in value or len(value) > 4096:
        raise UpdateError("The update public key must be base64 or a local PEM file path.")
    path = Path(value)
    if not path.is_absolute():
        path = data_dir / path
    try:
        if path.stat().st_size > 16384:
            raise UpdateError("The update public key file is too large.")
        key = serialization.load_pem_public_key(path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateError("The locally pinned update public key could not be read.") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise UpdateError("The update public key must be an Ed25519 key.")
    return key


def verify_manifest(manifest: Any, public_key: Ed25519PublicKey) -> dict:
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise UpdateError("The signed update manifest has unexpected or missing fields.")
    version_tuple(manifest["version"])
    validate_https_url(manifest["url"])
    if not isinstance(manifest["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
        raise UpdateError("The signed release hash is invalid.")
    if type(manifest["size"]) is not int or not 0 < manifest["size"] <= MAX_ARCHIVE_BYTES:
        raise UpdateError("The signed release size is outside the permitted range.")
    try:
        signature = base64.b64decode(manifest["signature"], validate=True)
        public_key.verify(signature, canonical_manifest(manifest))
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise UpdateError("The update manifest signature is invalid.") from exc
    return manifest


def _state_path(data_dir: Path) -> Path:
    return data_dir / "updates" / "state.json"


def _read_state(data_dir: Path) -> dict:
    state = _json_read(_state_path(data_dir))
    if not isinstance(state, dict):
        raise UpdateError("The local update state is invalid.")
    if "awaiting_health" in state and type(state["awaiting_health"]) is not bool:
        raise UpdateError("The local update health state is invalid.")
    for name in ("minimum_version", "previous_version", "activated_version"):
        if state.get(name) is not None:
            version_tuple(state[name])
    failed = state.get("failed_versions", [])
    if not isinstance(failed, list) or len(failed) > 100:
        raise UpdateError("The local rollback history is invalid.")
    for value in failed:
        version_tuple(value)
    return state


def _release_dir(install_dir: Path, version: str) -> Path:
    version_tuple(version)
    root = install_dir.resolve() / "releases"
    if root.is_symlink():
        raise UpdateError("The releases directory cannot be a symlink.")
    path = root / version
    if path.is_symlink() or (path.exists() and path.resolve() != path):
        raise UpdateError("The release path must be a regular local directory.")
    return path


def _release_version(path: Path) -> str:
    try:
        version_file = path / "VERSION"
        if version_file.is_symlink() or version_file.stat().st_size > 64:
            raise UpdateError("The release VERSION file is invalid.")
        version = version_file.read_text(encoding="ascii").strip()
        version_tuple(version)
        return version
    except (OSError, UnicodeError) as exc:
        raise UpdateError("The release VERSION file is missing or invalid.") from exc


def _validate_release(path: Path, version: str):
    if not path.is_dir() or path.is_symlink() or _release_version(path) != version:
        raise UpdateError("The release directory does not match its signed version.")
    executable = path / "FlightWall"
    if executable.is_symlink() or not executable.is_file() or executable.stat().st_size == 0:
        raise UpdateError("The release does not contain its FlightWall executable.")


def _current_version(install_dir: Path) -> str:
    current = install_dir / "current"
    if not current.is_symlink():
        raise UpdateError("The installed current release must be a symlink.")
    try:
        target = current.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UpdateError("The installed current release is missing.") from exc
    version = _release_version(target)
    if target != _release_dir(install_dir, version):
        raise UpdateError("The current release points outside the releases directory.")
    _validate_release(target, version)
    return version


def safe_extract(archive: Path, destination: Path, version: str):
    """Extract regular ZIP files only, with explicit traversal and resource limits."""
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if not members or len(members) > MAX_ARCHIVE_FILES:
                raise UpdateError("The release archive contains too many files.")
            total = 0
            seen = set()
            for member in members:
                name = member.filename
                original = member.orig_filename
                if (not name or "\\" in name or ":" in name or name.startswith("/")
                        or any(ord(c) < 32 for c in original) or "\x00" in original):
                    raise UpdateError("The release archive contains an unsafe path.")
                parts = PurePosixPath(name).parts
                if not parts or any(part in (".", "..") for part in parts):
                    raise UpdateError("The release archive contains an unsafe path.")
                normalized = "/".join(parts)
                if normalized in seen:
                    raise UpdateError("The release archive contains duplicate paths.")
                seen.add(normalized)
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or member.flag_bits & 1:
                    raise UpdateError("The release archive contains a link, special file, or encrypted file.")
                if member.is_dir() != (kind == stat.S_IFDIR) and kind != 0:
                    raise UpdateError("The release archive contains inconsistent file metadata.")
                total += member.file_size
                if (member.file_size > MAX_FILE_BYTES or total > MAX_UNPACKED_BYTES
                        or (member.file_size > 1024 * 1024 and member.file_size > max(1, member.compress_size) * 500)):
                    raise UpdateError("The release archive exceeds safe extraction limits.")
                target = destination.joinpath(*parts)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise UpdateError("The release archive escapes its release directory.")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with bundle.open(member) as source, target.open("xb") as output:
                    while block := source.read(1024 * 1024):
                        written += len(block)
                        if written > member.file_size:
                            raise UpdateError("A release file exceeds its declared size.")
                        output.write(block)
                if written != member.file_size:
                    raise UpdateError("A release file does not match its declared size.")
                target.chmod(0o755 if mode & 0o111 else 0o644)
        _validate_release(destination, version)
        (destination / "FlightWall").chmod(0o755)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _switch_current(install_dir: Path, version: str):
    release = _release_dir(install_dir, version)
    _validate_release(release, version)
    temporary = install_dir / (".current-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(Path("releases") / version, target_is_directory=True)
        os.replace(temporary, install_dir / "current")
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _cleanup_interrupted_staging(install_dir: Path):
    """Called only under the shared update lock, so no live download is removed."""
    releases = install_dir / "releases"
    if not releases.is_dir() or releases.is_symlink():
        return
    for entry in releases.iterdir():
        if not entry.name.startswith((".download-", ".staging-", ".orphan-")):
            continue
        if entry.name.startswith(".orphan-") and entry.is_dir() and not entry.is_symlink():
            # An interrupted replacement of an incomplete stage can leave its
            # extracted directory here. Restore its metadata for a verified retry.
            try:
                metadata = _json_read(entry / ".release.json")
                version = metadata.get("version")
                target = _release_dir(install_dir, version)
                _validate_release(entry, version)
                if not target.exists():
                    os.replace(entry, target)
                    continue
            except (UpdateError, AttributeError, OSError):
                pass
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)


def _rollback_locked(install_dir: Path, data_dir: Path, state: dict) -> dict:
    if not state.get("awaiting_health"):
        return {"status": "unchanged", "message": "The current release has no unconfirmed update."}
    failed = state.get("activated_version")
    previous = state.get("previous_version")
    if not failed or not previous:
        raise UpdateError("No previous healthy release is available for rollback.")
    _switch_current(install_dir, previous)
    failures = state.get("failed_versions", [])
    if failed not in failures:
        failures.append(failed)
    state.update(awaiting_health=False, failed_versions=failures[-100:], activated_version=previous)
    _atomic_json(_state_path(data_dir), state)
    pending_path = install_dir / "pending.json"
    if pending_path.exists():
        pending = _json_read(pending_path)
        if isinstance(pending, dict) and pending.get("version") == failed:
            pending_path.unlink()
    return {"status": "rolled_back", "version": previous, "failed_version": failed}


def activate_pending(install_dir, data_dir) -> dict:
    """Call from the stable launcher before GUI start, after the service is stopped."""
    install_dir, data_dir = Path(install_dir), Path(data_dir)
    try:
        with _locked(install_dir):
            state = _read_state(data_dir)
            if state.get("awaiting_health"):
                return _rollback_locked(install_dir, data_dir, state)
            pending_path = install_dir / "pending.json"
            pending = _json_read(pending_path)
            if not pending:
                return {"status": "unchanged", "version": _current_version(install_dir)}
            if not isinstance(pending, dict) or set(pending) != {"version", "sha256"}:
                raise UpdateError("The staged update metadata is invalid.")
            version = pending["version"]
            version_tuple(version)
            previous = _current_version(install_dir)
            if (version_tuple(version) <= version_tuple(previous) or version in state.get("failed_versions", [])
                    or version_tuple(version) < version_tuple(state.get("minimum_version", previous))):
                raise UpdateError("The staged update is not a newer, eligible release.")
            release = _release_dir(install_dir, version)
            _validate_release(release, version)
            if _json_read(release / ".release.json") != pending:
                raise UpdateError("The staged release metadata does not match its pending activation.")
            # Persist the rollback target before changing the symlink. A power loss is
            # conservatively treated as an unconfirmed activation on the next launch.
            state.update(previous_version=previous, activated_version=version, awaiting_health=True)
            _atomic_json(_state_path(data_dir), state)
            _switch_current(install_dir, version)
            pending_path.unlink()
            return {"status": "activated", "version": version, "previous_version": previous}
    except (UpdateError, OSError) as exc:
        return _error_status(exc)


def mark_healthy(install_dir, data_dir, version: str | None = None) -> dict:
    """App calls after its initial frame and core services start successfully."""
    install_dir, data_dir = Path(install_dir), Path(data_dir)
    try:
        with _locked(install_dir):
            state = _read_state(data_dir)
            current = _current_version(install_dir)
            if version is not None and version != current:
                raise UpdateError("A different release cannot confirm this activation.")
            if not state.get("awaiting_health"):
                return {"status": "unchanged", "version": current}
            if state.get("activated_version") != current:
                raise UpdateError("The active release does not match the health check.")
            state["awaiting_health"] = False
            _atomic_json(_state_path(data_dir), state)
            return {"status": "healthy", "version": current}
    except (UpdateError, OSError) as exc:
        return _error_status(exc)


def rollback(install_dir, data_dir) -> dict:
    """Undo only an activation that has not yet passed its startup health check."""
    install_dir, data_dir = Path(install_dir), Path(data_dir)
    try:
        with _locked(install_dir):
            return _rollback_locked(install_dir, data_dir, _read_state(data_dir))
    except (UpdateError, OSError) as exc:
        return _error_status(exc)


def _error_status(exc: Exception) -> dict:
    # Network exceptions may contain calendar credentials, proxy URLs, or signed URL
    # query strings. Never surface their arbitrary message to logs or remote status.
    if isinstance(exc, UpdateError):
        return {"status": "error", "message": str(exc)}
    return {"status": "error", "message": "The update could not be completed; check connectivity, free space, and file permissions."}


def stage_local_release(install_dir, data_dir, version: str) -> dict:
    """Stage an explicitly downloaded local installer, never a network update.

    The installer has already copied this release and checked its executable.
    This local command preserves the same next-launch activation and rollback
    rules as signed updates. Its content digest identifies the manual copy; it
    is not a signature and cannot authorize an automatic unsigned download.
    """
    install_dir, data_dir = Path(install_dir), Path(data_dir)
    try:
        version_tuple(version)
        with _locked(install_dir):
            state = _read_state(data_dir)
            if state.get("awaiting_health"):
                raise UpdateError("The previous launch is unconfirmed. Open and close Flight Wall to recover it before installing another release.")
            current = _current_version(install_dir)
            release = _release_dir(install_dir, version)
            _validate_release(release, version)
            pending = _json_read(install_dir / "pending.json")
            if pending:
                if (not isinstance(pending, dict) or set(pending) != {"version", "sha256"}
                        or not isinstance(pending["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", pending["sha256"])):
                    raise UpdateError("The staged update metadata is invalid.")
                version_tuple(pending["version"])
            floor = max([current, state.get("minimum_version", current)]
                        + ([pending["version"]] if pending else []), key=version_tuple)
            if version_tuple(version) < version_tuple(floor):
                raise UpdateError("A newer release has already been accepted; manual downgrade is blocked.")
            if version in state.get("failed_versions", []):
                raise UpdateError("This release previously failed its startup check. Install a higher version.")
            if version == current:
                return {"status": "unchanged", "version": version, "current_version": current,
                        "message": "This release is already selected; configuration is preserved."}

            # Hash relative names, link targets, sizes and file hashes in a stable
            # order. PyInstaller uses file symlinks inside _internal; their final
            # targets must remain regular files inside this same release.
            digest = hashlib.sha256()
            total = 0
            count = 0
            for path in sorted(release.rglob("*"), key=lambda item: item.relative_to(release).as_posix()):
                relative = path.relative_to(release).as_posix()
                if relative == ".release.json":
                    continue
                if any(ord(character) < 32 for character in relative):
                    raise UpdateError("The local release contains an unsafe filename.")
                if path.is_dir():
                    if path.is_symlink():
                        raise UpdateError("The local release contains a directory symlink.")
                    continue
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise UpdateError("The local release contains a broken or unsafe link.") from exc
                if not target.is_relative_to(release) or not target.is_file():
                    raise UpdateError("The local release contains an external link or special file.")
                size = target.stat().st_size
                count += 1
                total += size
                if count > MAX_ARCHIVE_FILES or size > MAX_FILE_BYTES or total > MAX_UNPACKED_BYTES:
                    raise UpdateError("The local release exceeds permitted size limits.")
                with target.open("rb") as source:
                    file_hash = hashlib.file_digest(source, "sha256").digest()
                link = os.readlink(path) if path.is_symlink() else ""
                for value in (relative.encode("utf-8"), link.encode("utf-8"), str(size).encode("ascii"), file_hash):
                    digest.update(len(value).to_bytes(8, "big"))
                    digest.update(value)
            metadata = {"version": version, "sha256": digest.hexdigest()}
            existing = _json_read(release / ".release.json")
            if existing:
                if pending == existing and pending.get("version") == version:
                    # A signed pending stage uses its archive hash. Preserve it.
                    return {"status": "staged", "version": version, "current_version": current,
                            "message": "This release is already staged for the next Ports launch."}
                if existing != metadata:
                    raise UpdateError("Different contents for this release were already accepted. Install a higher version.")
            _atomic_json(release / ".release.json", metadata)
            state["minimum_version"] = version
            _atomic_json(_state_path(data_dir), state)
            _atomic_json(install_dir / "pending.json", metadata)
            return {"status": "staged", "version": version, "current_version": current,
                    "message": "The manually installed release will activate at the next safe Ports launch."}
    except (UpdateError, OSError, UnicodeError) as exc:
        return _error_status(exc)


class UpdateManager:
    def __init__(self, install_dir, data_dir, config: Mapping[str, Any]):
        self.install_dir = Path(install_dir)
        self.data_dir = Path(data_dir)
        self.config = config

    def _configured(self) -> bool:
        return bool(self.config.get("updates_enabled") and self.config.get("update_manifest_url")
                    and self.config.get("update_public_key"))

    def _manifest(self) -> dict:
        output = io.BytesIO()
        _download(self.config["update_manifest_url"], output, MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(output.getvalue().decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("The update server did not return a valid manifest.") from exc
        key = load_public_key(self.config["update_public_key"], self.data_dir)
        return verify_manifest(manifest, key)

    def _assess(self, manifest: dict, state: dict) -> dict:
        current = _current_version(self.install_dir)
        version = manifest["version"]
        pending = _json_read(self.install_dir / "pending.json")
        if pending and (not isinstance(pending, dict) or set(pending) != {"version", "sha256"}):
            raise UpdateError("The staged update metadata is invalid.")
        candidates = [current, state.get("minimum_version", current)]
        if pending:
            version_tuple(pending["version"])
            candidates.append(pending["version"])
        floor = max(candidates, key=version_tuple)
        if version_tuple(version) < version_tuple(floor):
            raise UpdateError("The signed release is older than the highest accepted version; downgrade blocked.")
        if version in state.get("failed_versions", []):
            raise UpdateError("This release previously failed its startup health check; publish a higher version.")
        if pending and pending.get("version") == version and pending.get("sha256") == manifest["sha256"]:
            return {"status": "staged", "version": version, "current_version": current,
                    "message": "The update will activate on the next safe application launch."}
        if version_tuple(version) == version_tuple(current):
            return {"status": "up_to_date", "version": current}
        if version_tuple(version) == version_tuple(floor):
            # Recover a power loss between installing the extracted directory and
            # writing pending.json. The archive is downloaded and checked again.
            release = _release_dir(self.install_dir, version)
            metadata = _json_read(release / ".release.json") if release.exists() else {}
            if metadata != {"version": version, "sha256": manifest["sha256"]}:
                raise UpdateError("This release version has already been accepted; publish a higher version.")
        return {"status": "available", "version": version, "current_version": current, "size": manifest["size"]}

    def check(self) -> dict:
        if not self._configured():
            return {"status": "disabled", "message": "Signed automatic updates are not configured."}
        try:
            manifest = self._manifest()
            with _locked(self.install_dir):
                return self._assess(manifest, _read_state(self.data_dir))
        except Exception as exc:
            return _error_status(exc)

    def stage(self) -> dict:
        if not self._configured():
            return {"status": "disabled", "message": "Signed automatic updates are not configured."}
        temporary_archive = None
        staging = None
        abandoned = None
        try:
            # Fetch and verify afresh; check() results are never a substitute for verification.
            manifest = self._manifest()
            with _locked(self.install_dir):
                _cleanup_interrupted_staging(self.install_dir)
                state = _read_state(self.data_dir)
                assessment = self._assess(manifest, state)
                if assessment["status"] != "available":
                    return assessment
                version = manifest["version"]
                releases = self.install_dir / "releases"
                releases.mkdir(parents=True, exist_ok=True)
                target = _release_dir(self.install_dir, version)
                if target.exists():
                    metadata = _json_read(target / ".release.json")
                    if metadata != {"version": version, "sha256": manifest["sha256"]}:
                        raise UpdateError("A different directory for this release already exists; it will not be overwritten.")
                fd, filename = tempfile.mkstemp(prefix=".download-", suffix=".zip", dir=releases)
                temporary_archive = Path(filename)
                with os.fdopen(fd, "wb") as output:
                    _download(manifest["url"], output, manifest["size"], expected_size=manifest["size"])
                    output.flush()
                    os.fsync(output.fileno())
                with temporary_archive.open("rb") as incoming:
                    digest = hashlib.file_digest(incoming, "sha256").hexdigest()
                if digest != manifest["sha256"]:
                    raise UpdateError("The downloaded release does not match the signed SHA-256 hash.")
                staging = releases / (".staging-" + uuid.uuid4().hex)
                safe_extract(temporary_archive, staging, version)
                _atomic_json(staging / ".release.json", {"version": version, "sha256": digest})
                if target.exists():
                    abandoned = releases / (".orphan-" + uuid.uuid4().hex)
                    os.replace(target, abandoned)
                os.replace(staging, target)
                staging = None
                # Never lower this floor, even when a startup failure rolls back current.
                state["minimum_version"] = version
                _atomic_json(_state_path(self.data_dir), state)
                _atomic_json(self.install_dir / "pending.json", {"version": version, "sha256": digest})
                return {"status": "staged", "version": version, "current_version": assessment["current_version"],
                        "message": "The signed update is ready for the next safe application launch."}
        except Exception as exc:
            return _error_status(exc)
        finally:
            if temporary_archive is not None:
                temporary_archive.unlink(missing_ok=True)
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if abandoned is not None:
                if not target.exists() and abandoned.exists():
                    try:
                        os.replace(abandoned, target)
                    except OSError:
                        pass  # Recover it under the update lock at the next retry.
                else:
                    shutil.rmtree(abandoned, ignore_errors=True)
