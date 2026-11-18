"""Native-display-only photos from a fixed, private local directory."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat

from PIL import Image, ImageOps

VIEW = "couple_photos"
FOLDER = "couple-photos"
MAX_PHOTOS = 24
WEEKLY_PHOTOS = 4
MAX_BYTES = 4 * 1024 * 1024
MAX_DIMENSION = 3840
MAX_PIXELS = 3840 * 2160


def eligible(status, config):
    """Use only the GUI's freshly revalidated presence reports."""
    presence = status.get("presence")
    snapshot = status.get("snapshot")
    if (config.get("guest_mode", False) is not False or config.get("demo") is not False
            or config.get("presence_enabled") is not True or status.get("backend_available") is not True
            or not isinstance(snapshot, dict) or snapshot.get("demo") is not False
            or not isinstance(presence, dict)):
        return False
    states = set()
    for person in ("chris", "lisa"):
        report = presence.get(person)
        if not isinstance(report, dict) or report.get("stale") is not False:
            return False
        state = report.get("state")
        if state not in ("home", "away"):
            return False
        states.add(state)
    return states == {"home", "away"}


def _identity(info):
    return info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _file_identity(path, limit):
    try:
        info = path.lstat()
        return _identity(info) if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit else None
    except OSError:
        return None


def _read_file(path, identity, limit):
    """Read only the same regular file that was inventoried."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, os.O_RDONLY | nofollow | nonblock), "rb") as stream:
        if _identity(os.fstat(stream.fileno())) != identity:
            return None
        raw = stream.read(limit + 1)
        return raw if len(raw) == identity[3] and _identity(os.fstat(stream.fileno())) == identity else None


def _decode(path, identity):
    try:
        raw = _read_file(path, identity, MAX_BYTES)
        if raw is None:
            return None
        with Image.open(io.BytesIO(raw)) as source:
            if (source.format != "JPEG" or max(source.size) > MAX_DIMENSION
                    or source.width * source.height > MAX_PIXELS):
                return None
            picture = ImageOps.exif_transpose(source).convert("RGB")
            picture.info.clear()
            return picture
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return None


class LocalPhotoDeck:
    """Feature one set per local week and advance at the next card visit."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir) if isinstance(data_dir, (str, os.PathLike)) else None
        self._refresh_at = float("-inf")
        self._entries = ()
        self._invalid = set()
        self._week = 0
        self._last = 0
        self._file = self._picture = self._projection = None

    def _inventory(self):
        if self.data_dir is None:
            return ()
        settings = self.data_dir / "config.json"
        config_id = _file_identity(settings, 256 * 1024)
        if config_id is None:
            return ()
        try:
            saved = json.loads(_read_file(settings, config_id, 256 * 1024))
            if not isinstance(saved, dict) or saved.get("couple_photos_enabled") is not True:
                return ()
            directory = self.data_dir / FOLDER
            if not stat.S_ISDIR(directory.lstat().st_mode):
                return ()
        except (OSError, ValueError, TypeError, RecursionError):
            return ()
        found = []
        for number in range(1, MAX_PHOTOS + 1):
            path = directory / f"{number:02d}.jpg"
            identity = _file_identity(path, MAX_BYTES)
            if identity is not None:
                found.append((number, path, identity))
        return tuple(found)

    def _featured(self):
        if not self._entries:
            return ()
        groups = (len(self._entries) + WEEKLY_PHOTOS - 1) // WEEKLY_PHOTOS
        start = self._week % groups * WEEKLY_PHOTOS
        return self._entries[start:start + WEEKLY_PHOTOS]

    def refresh(self, tick, permitted, local_day):
        if not permitted:
            self._entries = ()
            self._invalid.clear()
            self._refresh_at = float("-inf")
            self.select(False)
            return False
        # Date ordinals start on a Monday. Use the board's local date so a
        # daylight saving transition or a restart never shifts the selection.
        self._week = (local_day.toordinal() - 1) // 7
        if tick >= self._refresh_at:
            self._entries = self._inventory()
            self._invalid.intersection_update(self._entries)
            self._refresh_at = tick + 1
        return any(entry not in self._invalid for entry in self._featured())

    def select(self, visible):
        featured = self._featured()
        if visible and self._file in featured:
            return self._picture
        self._file = self._picture = self._projection = None
        if not visible:
            return None
        entries = sorted(featured, key=lambda item: (item[0] <= self._last, item[0]))
        for entry in entries:
            if entry in self._invalid:
                continue
            number, path, identity = entry
            picture = _decode(path, identity)
            if picture is not None:
                self._last, self._file, self._picture = number, entry, picture
                return picture
            self._invalid.add(entry)
        return None

    def fit(self, size):
        """Keep the entire picture, with only one scaled surface cached."""
        if self._picture is None:
            return None
        if self._projection is None or self._projection[0] != size:
            picture = ImageOps.contain(self._picture, size, Image.Resampling.LANCZOS)
            self._projection = (size, picture)
        return self._projection[1]
