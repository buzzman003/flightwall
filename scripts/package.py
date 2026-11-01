#!/usr/bin/env python3
"""Make the downloadable offline installer and source bundle."""
from __future__ import annotations

import hashlib
from importlib import metadata
from pathlib import Path
import shutil
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from flightwall import __version__


def main():
    version = __version__
    release = ROOT / "releases" / version
    if not (release / "FlightWall").is_file() or (release / "VERSION").read_text().strip() != version:
        raise SystemExit("Build the matching native release first using scripts/build-linux.sh")
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    # Keep the previously delivered guide/checksums when packaging a new version.
    old_sums = output / "SHA256SUMS.txt"
    if old_sums.exists() and f"flightwall-batocera-{version}.tar.gz" not in old_sums.read_text():
        history = output / "previous-guides"
        history.mkdir(exist_ok=True)
        for name in ("SHA256SUMS.txt", "Flight-Wall-Complete-Setup.txt"):
            previous = output / name
            if previous.exists():
                shutil.copy2(previous, history / name)
    prefix = f"flightwall-batocera-{version}"
    installer = output / f"{prefix}.tar.gz"
    with tarfile.open(installer, "w:gz", compresslevel=6) as archive:
        archive.add(release, arcname=f"{prefix}/releases/{version}")
        archive.add(ROOT / "scripts" / "install.sh", arcname=f"{prefix}/install.sh")
        archive.add(ROOT / "scripts" / "diagnose.sh", arcname=f"{prefix}/scripts/diagnose.sh")
        archive.add(ROOT / "scripts" / "register-ports-media.py", arcname=f"{prefix}/scripts/register-ports-media.py")
        def source_filter(entry):
            return None if "__pycache__" in Path(entry.name).parts or entry.name.endswith(".pyc") else entry
        for name in ("batocera", "docs", "README.txt", "LICENSE.txt", "THIRD-PARTY.txt", "VERSION", "integrations"):
            archive.add(ROOT / name, arcname=f"{prefix}/{name}", filter=source_filter)
    source = output / f"flightwall-source-{version}.zip"
    allowed = ["flightwall", "tests", "scripts", "assets", "batocera", "docs", ".github", "integrations"]
    allowed_files = ["README.md", "README.txt", "LICENSE.txt", "THIRD-PARTY.txt", "CONTRACT.md", "AGENTS.md", ".gitignore", "pytest.ini", "requirements.txt", "requirements-build.txt", "Dockerfile.batocera", "flightwall_entry.py", ".dockerignore", "VERSION"]
    source_paths = []
    for name in allowed:
        source_paths.extend(path for path in (ROOT / name).rglob("*") if path.is_file())
    source_paths.extend(ROOT / name for name in allowed_files if (ROOT / name).is_file())
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source_paths):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            # Explicit source inventory: never traverse a data directory, venv,
            # build output, credentials or signing key directory.
            archive.write(path, f"flightwall-source-{version}/" + str(path.relative_to(ROOT)))
    ordered_docs = ["INSTALL.txt", "HOUSEHOLD-SETUP.txt", "PRESENCE.txt", "MESSAGES.txt", "ALEXA-SETUP.txt", "CALENDAR.txt", "REMOTE.txt", "UPDATE-CHANNEL.txt", "UPDATES.txt", "FEEDS.txt", "CLI.txt", "VALIDATION.txt"]
    guide = output / "Flight-Wall-Complete-Setup.txt"
    with guide.open("w", encoding="utf-8") as stream:
        stream.write("FLIGHT WALL: COMPLETE SETUP AND REFERENCE\n\n")
        stream.write("Start with INSTALL below. The remaining sections give extra detail.\n")
        for name in ordered_docs:
            path = ROOT / "docs" / name
            if path.exists():
                stream.write("\n\n" + "=" * 76 + "\n" + name + "\n" + "=" * 76 + "\n\n")
                stream.write(path.read_text())
    alexa_zip = output / f"Flight-Wall-Alexa-Lambda-{version}.zip"
    with zipfile.ZipFile(alexa_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / "integrations" / "alexa" / "lambda_function.py", "lambda_function.py")
        # zoneinfo uses this packaged fallback if the Lambda runtime omits the
        # system IANA database. No pip installation or Lambda layer is needed.
        import tzdata
        timezone_package = Path(tzdata.__file__).parent
        for path in sorted(timezone_package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                archive.write(path, "tzdata/" + str(path.relative_to(timezone_package)))
        timezone_dist = metadata.distribution("tzdata")
        for file in timezone_dist.files or []:
            if Path(str(file)).name.upper() in {"LICENSE", "LICENSE_APACHE"}:
                archive.write(timezone_dist.locate_file(file), "licenses/tzdata-" + Path(str(file)).name)
    alexa_model = output / "Flight-Wall-Alexa-Interaction-Model-en-US.json"
    shutil.copy2(ROOT / "integrations" / "alexa" / "interaction-model-en-US.json", alexa_model)
    alexa_guide = output / "Flight-Wall-Alexa-Setup.txt"
    shutil.copy2(ROOT / "docs" / "ALEXA-SETUP.txt", alexa_guide)
    household_guide = output / "Flight-Wall-Household-Setup.txt"
    shutil.copy2(ROOT / "docs" / "HOUSEHOLD-SETUP.txt", household_guide)
    sums = output / "SHA256SUMS.txt"
    with sums.open("w") as stream:
        for path in (installer, source, guide, household_guide, alexa_zip, alexa_model, alexa_guide):
            with path.open("rb") as incoming:
                digest = hashlib.file_digest(incoming, "sha256").hexdigest()
            stream.write(f"{digest}  {path.name}\n")
            print(f"{path.name}: {path.stat().st_size:,} bytes")
    print(f"Checksums: {sums}")


if __name__ == "__main__":
    main()
