#!/usr/bin/env python3
"""Register only Flight Wall's own Ports image/logo/video; no service controls.

Default is a read-only plan. --apply makes private backups and atomic writes.
Other game/folder entries remain byte-for-byte identical. Existing artwork at
other paths is left untouched; overwritten Flight Wall-owned filenames and
the original gamelist are backed up outside releases before any replacement.
EmulationStation may cache gamelists: use its normal Update Gamelists after a
safe opportunity. This helper never stops/restarts ES, games, services or OS.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import uuid
import xml.etree.ElementTree as ET
from xml.parsers import expat

MEDIA = {"image": ("flightwall-image.png", "images/flightwall-image.png"),
         "marquee": ("flightwall-logo.png", "images/flightwall-logo.png"),
         "video": ("flightwall-preview.mp4", "videos/flightwall-preview.mp4")}
MAX_XML = 8_000_000
MAX_MEDIA = 25_000_000


class MediaError(ValueError):
    """Only fixed nonsecret diagnostics are returned."""


def require(condition, message):
    if not condition:
        raise MediaError(message)


def _tag_end(raw, start):
    quote = None
    for index in range(start, len(raw)):
        value = raw[index]
        if quote:
            if value == quote:
                quote = None
        elif value in (34, 39):
            quote = value
        elif value == 62:
            return index + 1
    raise MediaError("An XML tag is incomplete")


def _spans(raw):
    """Expat byte offsets retain every unrelated entry and comment verbatim."""
    parser = expat.ParserCreate()
    depth, active, spans, root = 0, None, [], {}
    def start(name, attrs):
        nonlocal depth, active
        offset = parser.CurrentByteIndex
        if depth == 0:
            root.update(start=offset, open_end=_tag_end(raw, offset))
            root["self_closing"] = raw[offset:root["open_end"]].rstrip().endswith(b"/>")
        if depth == 1 and name == "game":
            end = _tag_end(raw, offset)
            active = (offset, end, raw[offset:end].rstrip().endswith(b"/>"))
        depth += 1
    def end(name):
        nonlocal depth, active
        depth -= 1
        if depth == 1 and name == "game" and active is not None:
            left, open_end, self_closing = active
            right = open_end if self_closing else _tag_end(raw, parser.CurrentByteIndex)
            spans.append((left, right))
            active = None
        if depth == 0:
            root["close_start"] = parser.CurrentByteIndex
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(raw, True)
    return spans, root


def update_gamelist(raw, ports):
    require(len(raw) <= MAX_XML and b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
            "Unsupported or oversized gamelist XML; unchanged")
    try:
        raw.decode("utf-8")
        tree = ET.fromstring(raw)
        require(tree.tag == "gameList", "Unsupported gamelist root; unchanged")
        spans, root = _spans(raw)
    except (UnicodeError, ET.ParseError, expat.ExpatError):
        raise MediaError("Gamelist must be valid UTF-8 XML; unchanged") from None
    targets = []
    expected = str(ports / "Flight Wall.sh").replace(os.sep, "/")
    for left, right in spans:
        node = ET.fromstring(raw[left:right])
        paths = node.findall("path")
        if len(paths) == 1:
            path = str(PurePosixPath((paths[0].text or "").strip()))
            if path in {"Flight Wall.sh", expected}:
                targets.append((left, right, node))
    require(len(targets) <= 1, "Multiple Flight Wall entries require review; unchanged")
    if targets:
        left, right, node = targets[0]
    else:
        node = ET.Element("game")
        ET.SubElement(node, "path").text = "./Flight Wall.sh"
        ET.SubElement(node, "name").text = "Flight Wall"
    changed = not targets
    for key, (_, relative) in MEDIA.items():
        fields = node.findall(key)
        require(len(fields) <= 1, "Duplicate Flight Wall media fields require review; unchanged")
        field = fields[0] if fields else ET.SubElement(node, key)
        value = "./" + relative
        changed |= field.text != value
        field.text = value
    if not changed:
        return raw, False
    ET.indent(node, space="  ")
    entry = ET.tostring(node, encoding="utf-8", short_empty_elements=True)
    if targets:
        return raw[:left] + entry + raw[right:], True
    if root["self_closing"]:
        opening = raw[root["start"]:root["open_end"]]
        opening = opening.rstrip()[:-2] + b">"
        return raw[:root["start"]] + opening + b"\n" + entry + b"\n</gameList>" + raw[root["open_end"]:], True
    location = root["close_start"]
    return raw[:location] + entry + b"\n" + raw[location:], True


def _read(path, maximum):
    require(not path.is_symlink(), "A linked media/gamelist file requires review; unchanged")
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(info.st_mode) and info.st_size <= maximum, "A file is nonregular or oversized; unchanged")
    data = path.read_bytes()
    require(len(data) <= maximum, "A file grew beyond its size bound; unchanged")
    return data


def _atomic(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".flightwall-media-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def register(ports, assets, backup_dir, *, apply=False):
    ports, assets, backup_dir = Path(ports), Path(assets), Path(backup_dir)
    require(ports.is_dir() and not ports.is_symlink(), "Ports must be an existing real directory")
    require((ports / "Flight Wall.sh").is_file(), "Install the Flight Wall launcher before registering its media")
    require(assets.is_dir() and not assets.is_symlink(), "Media source directory is missing or linked")
    root = ports.resolve(strict=True)
    gamelist = ports / "gamelist.xml"
    original = _read(gamelist, MAX_XML)
    raw = original if original is not None else b'<?xml version="1.0" encoding="utf-8"?>\n<gameList>\n</gameList>\n'
    revised, xml_changed = update_gamelist(raw, ports)
    plans = []
    for source_name, relative in MEDIA.values():
        source, target = assets / source_name, ports / relative
        desired = _read(source, MAX_MEDIA)
        require(desired is not None and len(desired) > 0, "A required public media asset is missing")
        require(not target.parent.is_symlink() and target.parent.resolve().is_relative_to(root),
                "Media destination must remain inside Ports")
        previous = _read(target, MAX_MEDIA)
        if previous != desired:
            plans.append((relative, target, previous, desired))
    result = {"entry": "Flight Wall", "media_files_changed": len(plans), "gamelist_changed": xml_changed,
              "other_entries_unchanged": True, "requires_update_gamelists": xml_changed, "applied": False}
    if not apply or not (plans or xml_changed):
        return result
    require(not backup_dir.is_symlink(), "Backup directory must not be a link")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    backup = backup_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    if original is not None:
        (backup / "gamelist.xml").write_bytes(original)
    for relative, _, previous, _ in plans:
        if previous is not None:
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            saved.write_bytes(previous)
    (backup / "receipt.json").write_text(json.dumps({
        "entry": "Flight Wall", "gamelist_existed": original is not None,
        "gamelist_before_sha256": hashlib.sha256(raw).hexdigest(),
        "changed_media": [relative for relative, _, _, _ in plans]}, indent=2) + "\n")
    # Check the live gamelist once more before publishing any media. Each file
    # replacement is atomic, and the XML is last so it never points to missing
    # new files. Never overwrite a concurrent ES metadata save silently.
    require(_read(gamelist, MAX_XML) == original, "Gamelist changed concurrently; backup retained, no replacements made")
    for _, target, previous, desired in plans:
        require(_read(target, MAX_MEDIA) == previous, "Flight Wall artwork changed concurrently; backup retained")
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic(target, desired)
    if xml_changed:
        require(_read(gamelist, MAX_XML) == original, "Gamelist changed concurrently; media copied, XML unchanged; backup retained")
        _atomic(gamelist, revised)
    result.update(applied=True, backup_name=backup.name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports-dir", type=Path, default=Path("/userdata/roms/ports"))
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, default=Path("/userdata/system/configs/flightwall/ports-media-backups"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(register(args.ports_dir, args.assets_dir, args.backup_dir, apply=args.apply)))
    except (OSError, ValueError) as error:
        print(json.dumps({"applied": False, "error": str(error) if isinstance(error, MediaError) else
                          "Media registration could not complete; original backup retained if created"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
