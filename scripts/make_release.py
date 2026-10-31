#!/usr/bin/env python3
"""Create signed Flight Wall release files on Linux, macOS, or Windows.

Run this on the maintainer's computer, never on the display as an automatic action.
It packages an already frozen Linux x86_64 application; it cannot cross-compile one.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flightwall.updater import (  # noqa: E402
    MAX_ARCHIVE_BYTES, MAX_ARCHIVE_FILES, MAX_UNPACKED_BYTES,
    UpdateError, canonical_manifest, safe_extract, validate_https_url, version_tuple,
)


def keygen(directory: Path):
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    names = [directory / "flightwall-signing-private.pem", directory / "flightwall-signing-public.pem",
             directory / "flightwall-public-key.txt"]
    if any(path.exists() for path in names):
        raise UpdateError("A signing key file already exists in that directory; choose a new empty key directory.")
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())
    public = private.public_key()
    public_bytes = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    raw_public = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fd = os.open(names[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(private_bytes)
    names[1].write_bytes(public_bytes)
    encoded = base64.b64encode(raw_public).decode("ascii")
    names[2].write_text(encoded + "\n", encoding="ascii")
    print("Private signing key (keep private; never publish):", names[0])
    print("Public key file (safe to copy to the display):", names[1])
    print("Public key configuration value:", encoded)


def _source_files(source: Path, private_key: Path):
    forbidden_names = {"token.json", "credentials.json", "client_secret.json", "config.json", "state.json",
                       ".env", "flightwall-signing-private.pem", "flightwall-token.json",
                       "google-client.json", "google-token.json", "gmail-client.json", "gmail-token.json",
                       "household-calendars.json", "personal.json", "content.json", "layovers.json",
                       "timers.json", "flight-watches.json", "remote-oauth.json", "weather.json",
                       "consents.json", "setup-consents.json", "setup-complete.json",
                       "openai-usage.json", "flightaware-usage.json",
                       "sms.json", "flightwall-list.json", "list.json", "lisa.json",
                       "personal.lock", "timers.lock", "flight-watches.lock",
                       "household-calendars.lock", "openai-usage.lock"}
    # These are private runtime/workstation directories, even when accidentally
    # nested under _internal. Code such as shopping.py and public assets/stores
    # remain valid; a directory named shopping contains mutable household data.
    forbidden_dirs = {".git", ".ssh", ".aws", ".codex", ".agents", ".local-install",
                      "userdata", "secrets", "accounts", "artwork", "presence",
                      "shopping", "phone-invites", "updates", "logs"}
    files = []
    total = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.lower() in forbidden_dirs for part in relative.parts):
            raise UpdateError("The source folder contains a credentials or repository directory; package only the frozen application.")
        if path.is_dir():
            if path.is_symlink():
                raise UpdateError("Directory symlinks cannot be packaged in a release.")
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(source) or resolved == private_key:
            raise UpdateError("A source file points outside the application or at the private signing key.")
        if not path.is_file():
            raise UpdateError("The application contains a special file that cannot be packaged.")
        name = path.name.lower()
        if (name in forbidden_names or name.endswith(("-private.pem", ".key", ".token", "token.txt", "-cache.json"))
                or name.startswith("client_secret_") or name.startswith(".env.")):
            raise UpdateError("The source contains a potential credentials file; remove it before packaging.")
        with path.open("rb") as handle:
            prefix = handle.read(128).lstrip()
        if prefix.startswith((b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN RSA PRIVATE KEY-----",
                              b"-----BEGIN EC PRIVATE KEY-----", b"-----BEGIN OPENSSH PRIVATE KEY-----")):
            raise UpdateError("The source contains a private key; it must never be packaged.")
        total += path.stat().st_size
        files.append((path, relative))
        if total > MAX_UNPACKED_BYTES or len(files) > MAX_ARCHIVE_FILES:
            raise UpdateError("The frozen application exceeds the updater's release limits.")
    return files


def build(source: Path, version: str, url: str, private_key: Path, output: Path):
    version_tuple(version)
    validate_https_url(url)
    source, private_key = source.expanduser().resolve(), private_key.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_dir():
        raise UpdateError("The source must be the frozen FlightWall application directory.")
    if output.is_relative_to(source) or private_key.is_relative_to(source):
        raise UpdateError("Keep the signing key and output directory outside the frozen application directory.")
    executable = source / "FlightWall"
    if not executable.is_file():
        raise UpdateError("The source folder must contain the Linux FlightWall executable at its root.")
    with executable.open("rb") as handle:
        header = handle.read(20)
    if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise UpdateError("The executable must be an already built Linux x86_64 ELF executable.")
    if (source / "VERSION").read_text(encoding="ascii").strip() != version:
        raise UpdateError("The frozen application's VERSION file must match --version.")
    key = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise UpdateError("The signing key must be an Ed25519 private key.")
    files = _source_files(source, private_key)
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / f"FlightWall-{version}-linux-x86_64.zip"
    manifest_path = output / "manifest.json"
    if archive_path.exists() or manifest_path.exists():
        raise UpdateError("Release output files already exist; use a fresh output directory.")
    try:
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, relative in files:
                # Dereference local shared-library symlinks into regular archive files.
                # The updater never extracts symlinks.
                entry = zipfile.ZipInfo(relative.as_posix(), date_time=(2024, 1, 1, 0, 0, 0))
                entry.create_system = 3
                executable_bit = relative.as_posix() == "FlightWall" or bool(path.stat().st_mode & 0o111)
                entry.external_attr = (stat.S_IFREG | (0o755 if executable_bit else 0o644)) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as incoming, archive.open(entry, "w", force_zip64=True) as outgoing:
                    while block := incoming.read(1024 * 1024):
                        outgoing.write(block)
        size = archive_path.stat().st_size
        if not 0 < size <= MAX_ARCHIVE_BYTES:
            raise UpdateError("The release archive exceeds the updater's download limit.")
        # Verify the exact archive using the same extraction policy as the display.
        with tempfile.TemporaryDirectory(prefix="flightwall-release-check-") as temporary:
            safe_extract(archive_path, Path(temporary) / "release", version)
        with archive_path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        manifest = {"version": version, "url": url, "sha256": digest, "size": size}
        manifest["signature"] = base64.b64encode(key.sign(canonical_manifest(manifest))).decode("ascii")
        key.public_key().verify(base64.b64decode(manifest["signature"]), canonical_manifest(manifest))
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    print("Verified release archive:", archive_path)
    print("Signed update manifest:", manifest_path)
    print("Upload these two files to your own HTTPS release host. Nothing was uploaded by this command.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("keygen", help="Generate a local Ed25519 release signing key.")
    generate.add_argument("--directory", type=Path, required=True)
    release = sub.add_parser("build", help="Package and sign an already frozen Linux x86_64 application.")
    release.add_argument("--source", type=Path, required=True)
    release.add_argument("--version", required=True)
    release.add_argument("--url", required=True, help="Final public HTTPS URL of the release ZIP.")
    release.add_argument("--private-key", type=Path, required=True)
    release.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            keygen(args.directory)
        else:
            build(args.source, args.version, args.url, args.private_key, args.output)
        return 0
    except (UpdateError, OSError, ValueError, TypeError) as exc:
        print("Release preparation failed:", str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
