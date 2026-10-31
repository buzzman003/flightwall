"""Manual package upgrades share the existing safe launch lifecycle."""
import json
import os
from pathlib import Path

import pytest

from flightwall import updater


@pytest.fixture
def installation(tmp_path):
    install = tmp_path / "install"
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text('{"calendar_id":"private","update_public_key":"pinned"}')
    for version in ("0.1.0", "0.2.0", "0.3.0"):
        release = install / "releases" / version
        release.mkdir(parents=True)
        (release / "VERSION").write_text(version + "\n")
        (release / "FlightWall").write_bytes(b"local executable " + version.encode())
        (release / "_internal").mkdir()
        (release / "_internal" / "asset").write_bytes(b"bundled data")
    try:
        (install / "current").symlink_to("releases/0.1.0", target_is_directory=True)
        (install / "update-helper").symlink_to("releases/0.1.0", target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable; Linux must run the native install lifecycle checks")
        raise
    return install, data


def test_manual_stage_activates_only_at_next_launch_and_preserves_data(installation):
    install, data = installation
    config = (data / "config.json").read_bytes()
    result = updater.stage_local_release(install, data, "0.2.0")
    assert result["status"] == "staged"
    assert (install / "current").resolve().name == "0.1.0"
    assert (install / "update-helper").resolve().name == "0.1.0"
    assert updater.activate_pending(install, data)["status"] == "activated"
    assert updater.mark_healthy(install, data, "0.2.0")["status"] == "healthy"
    assert updater.activate_pending(install, data)["status"] == "unchanged"
    assert (data / "config.json").read_bytes() == config


def test_failed_manual_upgrade_rolls_back_without_lowering_floor(installation):
    install, data = installation
    updater.stage_local_release(install, data, "0.2.0")
    updater.activate_pending(install, data)
    assert updater.rollback(install, data)["status"] == "rolled_back"
    assert (install / "current").resolve().name == "0.1.0"
    assert updater.stage_local_release(install, data, "0.2.0")["status"] == "error"
    assert updater.stage_local_release(install, data, "0.1.0")["status"] == "error"
    assert updater.stage_local_release(install, data, "0.3.0")["status"] == "staged"


def test_manual_stage_does_not_replace_a_newer_pending_release(installation):
    install, data = installation
    updater.stage_local_release(install, data, "0.3.0")
    before = (install / "pending.json").read_bytes()
    assert updater.stage_local_release(install, data, "0.2.0")["status"] == "error"
    assert (install / "pending.json").read_bytes() == before


def test_same_manual_stage_is_idempotent(installation):
    install, data = installation
    assert updater.stage_local_release(install, data, "0.2.0")["status"] == "staged"
    before = (install / "pending.json").read_bytes()
    assert updater.stage_local_release(install, data, "0.2.0")["status"] == "staged"
    assert (install / "pending.json").read_bytes() == before


def test_fresh_or_existing_selected_release_is_unchanged(installation):
    install, data = installation
    assert updater.stage_local_release(install, data, "0.1.0")["status"] == "unchanged"
    assert not (install / "pending.json").exists()


def test_existing_different_metadata_is_not_rewritten(installation):
    install, data = installation
    metadata = install / "releases/0.2.0/.release.json"
    metadata.write_text(json.dumps({"version": "0.2.0", "sha256": "f" * 64}))
    assert updater.stage_local_release(install, data, "0.2.0")["status"] == "error"
    assert not (install / "pending.json").exists()


def test_internal_file_symlinks_are_allowed_but_external_links_rejected(installation):
    install, data = installation
    release = install / "releases/0.2.0"
    (release / "asset-link").symlink_to("_internal/asset")
    assert updater.stage_local_release(install, data, "0.2.0")["status"] == "staged"
    other = install / "releases/0.3.0"
    (other / "external").symlink_to(data / "config.json")
    assert updater.stage_local_release(install, data, "0.3.0")["status"] == "error"


def test_directory_symlinks_are_rejected(installation):
    install, data = installation
    (install / "releases/0.2.0/linked-dir").symlink_to("_internal", target_is_directory=True)
    assert updater.stage_local_release(install, data, "0.2.0")["status"] == "error"


def test_unconfirmed_launch_requires_recovery_before_another_install(installation):
    install, data = installation
    updater.stage_local_release(install, data, "0.2.0")
    updater.activate_pending(install, data)
    result = updater.stage_local_release(install, data, "0.3.0")
    assert result["status"] == "error"
    assert "unconfirmed" in result["message"]
    assert updater.activate_pending(install, data)["status"] == "rolled_back"
    assert updater.stage_local_release(install, data, "0.3.0")["status"] == "staged"


@pytest.mark.parametrize("version", ["../0.2.0", "0.2", "00.2.0", "0.2.0-beta"])
def test_invalid_versions_are_rejected(installation, version):
    install, data = installation
    assert updater.stage_local_release(install, data, version)["status"] == "error"
