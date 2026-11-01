"""Narrow, idempotent Ports artwork registration preserves unrelated metadata."""
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ports_media", ROOT / "scripts/register-ports-media.py")
media = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(media)


@pytest.fixture
def folders(tmp_path):
    ports, assets, backups = (tmp_path / name for name in ("ports", "assets", "private-backups"))
    ports.mkdir(); assets.mkdir()
    (ports / "Flight Wall.sh").write_text("#!/bin/sh\n# fictional launcher\n")
    for source, _ in media.MEDIA.values():
        (assets / source).write_bytes(b"fictional asset " + source.encode())
    return ports, assets, backups


def test_only_flight_wall_changes_other_game_folder_comments_and_metadata_stay_exact(folders):
    ports, assets, backups = folders
    other = b'<game id="123"><path>./Other.sh</path><desc><![CDATA[Keep <game> text & symbols]]></desc><favorite>true</favorite></game>'
    folder = b'<folder custom="yes"><path>./group</path><name>Keep group</name></folder>'
    own = b'<game id="owner-id"><path>./Flight Wall.sh</path><name>My Flight Wall</name><playcount>19</playcount><image>./images/my-art.png</image></game>'
    original = b'<?xml version="1.0"?>\r\n<gameList>\r\n<!-- keep comment -->\r\n' + other + b'\r\n' + own + b'\r\n' + folder + b'\r\n</gameList>\r\n'
    (ports / "gamelist.xml").write_bytes(original)
    (ports / "images").mkdir(); (ports / "images/my-art.png").write_bytes(b"keep custom artwork")
    dry = media.register(ports, assets, backups)
    assert not dry["applied"] and not backups.exists()
    assert (ports / "gamelist.xml").read_bytes() == original
    result = media.register(ports, assets, backups, apply=True)
    revised = (ports / "gamelist.xml").read_bytes()
    assert other in revised and folder in revised and b'<!-- keep comment -->' in revised
    selected = next(game for game in ET.fromstring(revised).findall("game") if game.findtext("path") == "./Flight Wall.sh")
    assert selected.attrib == {"id": "owner-id"}
    assert selected.findtext("name") == "My Flight Wall" and selected.findtext("playcount") == "19"
    assert selected.findtext("video") == "./videos/flightwall-preview.mp4"
    assert selected.findtext("marquee") == "./images/flightwall-logo.png"
    assert (ports / "images/my-art.png").read_bytes() == b"keep custom artwork"
    assert (backups / result["backup_name"] / "gamelist.xml").read_bytes() == original
    second = media.register(ports, assets, backups, apply=True)
    assert not second["applied"] and not second["gamelist_changed"] and second["media_files_changed"] == 0
    assert len(list(backups.iterdir())) == 1 and (ports / "gamelist.xml").read_bytes() == revised


def test_prior_own_filename_art_is_backed_up_before_replacement(folders):
    ports, assets, backups = folders
    (ports / "images").mkdir()
    (ports / "images/flightwall-logo.png").write_bytes(b"original owner image")
    result = media.register(ports, assets, backups, apply=True)
    assert (backups / result["backup_name"] / "images/flightwall-logo.png").read_bytes() == b"original owner image"
    assert (ports / "images/flightwall-logo.png").read_bytes() == (assets / "flightwall-logo.png").read_bytes()


@pytest.mark.parametrize("raw", (b'<gameList/>', b'<?xml version="1.0"?><gameList>\n</gameList>'))
def test_empty_gamelist_addition_has_the_exact_installed_launcher_path(folders, raw):
    ports, _, _ = folders
    revised, changed = media.update_gamelist(raw, ports)
    assert changed and ET.fromstring(revised).findtext("game/path") == "./Flight Wall.sh"
    assert media.update_gamelist(revised, ports) == (revised, False)


@pytest.mark.parametrize("raw", (b"broken", b'<!DOCTYPE gameList [<!ENTITY x "bad">]><gameList/>',
    b'<gameList><game><path>./Flight Wall.sh</path></game><game><path>Flight Wall.sh</path></game></gameList>',
    b'<gameList><game><path>./Flight Wall.sh</path><video>a</video><video>b</video></game></gameList>'))
def test_ambiguous_or_malformed_xml_is_never_written(folders, raw):
    ports, assets, backups = folders
    (ports / "gamelist.xml").write_bytes(raw)
    with pytest.raises(media.MediaError):
        media.register(ports, assets, backups, apply=True)
    assert (ports / "gamelist.xml").read_bytes() == raw and not backups.exists()


def test_similar_named_games_are_not_mistaken_for_the_target(folders):
    ports, _, _ = folders
    other = b'<game><path>./Flight Wall Backup.sh</path><name>Do not edit</name></game>'
    revised, changed = media.update_gamelist(b'<gameList>' + other + b'</gameList>', ports)
    assert changed and other in revised and len(ET.fromstring(revised).findall("game")) == 2


def test_installer_packages_helper_and_uses_frozen_public_assets_without_app_dependency():
    package = (ROOT / "scripts/package.py").read_text()
    installer = (ROOT / "scripts/install.sh").read_text()
    assert 'arcname=f"{prefix}/scripts/register-ports-media.py"' in package
    assert '_internal/assets/ports' in installer and '--backup-dir "$DATA_DIR/ports-media-backups"' in installer
    assert 'register-ports-media.py' in installer
