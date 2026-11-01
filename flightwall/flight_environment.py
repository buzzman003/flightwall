"""Opt-in official national radar and FAA airport programs, never a briefing.

Both requests are global fixed public URLs: scheduled airports only select
what is displayed locally. Source clocks are distinct from retrieval clocks.
FAA XML follows https://www.fly.faa.gov/fly/dtd/AirportStatus.dtd; the DTD is
documentation only and is never fetched or expanded by this parser.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.request
import warnings
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from PIL import Image

from . import __version__
from .config import private_json
from .trip_weather import select_airports

UTC = timezone.utc
RADAR_URL = "https://radar.weather.gov/ridge/standard/CONUS_0.gif"
FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
POLL_SECONDS = {"radar": 600, "airport_alerts": 300}
FETCH_MAX_AGE = {"radar": 1200, "airport_alerts": 600}
SOURCE_MAX_AGE = {"radar": 1800, "airport_alerts": 900}
MAX_RESPONSE = {RADAR_URL: 10_000_000, FAA_URL: 2_000_000}
MAX_PIXELS = 10_000_000
_KINDS = {"ground_stop", "ground_delay", "arrival_delay", "departure_delay"}
_IATA = re.compile(r"[A-Z]{3}")
_HASH = re.compile(r"[a-f0-9]{64}")


def _now(value=None):
    value = datetime.now(UTC) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Flight environment requires an aware timestamp")
    return value.astimezone(UTC)


def _stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid environment timestamp")
    return _now(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _iso(value):
    return value.isoformat(timespec="seconds")


def request_bytes(url):
    """Only two fixed HTTPS endpoints, with no redirects, proxies or queries."""
    if url not in MAX_RESPONSE:
        raise ValueError("Unexpected flight environment endpoint")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = urllib.request.Request(url, headers={
        "User-Agent": "FlightWall/" + __version__ + " (https://github.com/buzzman003/flightwall)",
        "Accept": "image/gif,image/png" if url == RADAR_URL else "application/xml",
        "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=10) as response:
        if response.geturl() != url or response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("Unexpected flight environment response")
        raw = response.read(MAX_RESPONSE[url] + 1)
        if not raw or len(raw) > MAX_RESPONSE[url]:
            raise ValueError("Flight environment response exceeds bounds")
        return raw, response.headers.get("Last-Modified")


def _source_header(value):
    try:
        if not isinstance(value, str) or len(value) > 100:
            return None
        return _iso(_now(parsedate_to_datetime(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _radar_png(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RESPONSE[RADAR_URL]:
        raise ValueError("Invalid radar image size")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as image:
            if image.format not in {"GIF", "PNG"} or not 0 < image.width * image.height <= MAX_PIXELS:
                raise ValueError("Invalid radar image")
            image.seek(0)
            image.load()
            rgb = image.convert("RGB")
            rgb.thumbnail((2048, 1024), Image.Resampling.LANCZOS)
            output = BytesIO()
            rgb.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _private_bytes(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _text(value, limit=160):
    if not isinstance(value, str):
        return ""
    # No markup, control characters or unbounded source text in status/cache.
    value = re.sub(r"<[^>]*>", "", value[:2000])
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    return " ".join(value.split())[:limit]


def _duration(value):
    value = _text(value, 40)
    # Keep the provider's explicit units; never guess that bare numbers are minutes.
    return value if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:minutes?|mins?|hours?|hrs?)", value, re.I) else ""


def _faa_stamp(value):
    # Fixed English FAA GMT format, independent of the computer's locale.
    match = re.fullmatch(r"[A-Z][a-z]{2} ([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4}) GMT", value or "")
    if not match:
        raise ValueError("Invalid FAA source clock")
    month, day, hour, minute, second, year = match.groups()
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    if month not in months:
        raise ValueError("Invalid FAA source month")
    return datetime(int(year), months.index(month) + 1, int(day), int(hour), int(minute), int(second), tzinfo=UTC)


def _parse_faa(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RESPONSE[FAA_URL]:
        raise ValueError("Invalid FAA response size")
    # Reject UTF-16/32 and declarations before parsing so no encoding can conceal
    # a DTD/entity declaration. Dtd_File is inert text and is never followed.
    if b"\x00" in raw or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
        raise ValueError("FAA XML declarations are not permitted")
    root = ET.fromstring(raw)
    if root.tag != "AIRPORT_STATUS_INFORMATION" or sum(1 for _ in root.iter()) > 15000:
        raise ValueError("Invalid FAA document")
    issued = _iso(_faa_stamp(root.findtext("Update_Time")))
    events = []

    def add(row, kind, delay):
        airport = row.findtext("ARPT", "").strip()
        if _IATA.fullmatch(airport):
            event = {"airport": airport, "kind": kind, "reason": _text(row.findtext("Reason")),
                     "delay_text": delay}
            if event not in events:
                events.append(event)
        if len(events) > 500:
            raise ValueError("Too many FAA programs")

    for group in root.findall("Delay_type"):
        for row in group.findall("Ground_Stop_List/Program"):
            # End_Time is a provider-supplied UTC label, not an inferred date.
            end = _text(row.findtext("End_Time"), 60)
            add(row, "ground_stop", "Until " + end if end else "")
        for row in group.findall("Ground_Delay_List/Ground_Delay"):
            parts = [(label + " " + duration) for label, key in (("Average", "Avg"), ("Maximum", "Max"))
                     if (duration := _duration(row.findtext(key)))]
            add(row, "ground_delay", "; ".join(parts))
        for row in group.findall("Arrival_Departure_Delay_List/Delay"):
            for direction in row.findall("Arrival_Departure"):
                kind = {"Arrival": "arrival_delay", "Departure": "departure_delay"}.get(direction.get("Type"))
                if kind:
                    parts = [(label + " " + duration) for label, key in (("Minimum", "Min"), ("Maximum", "Max"))
                             if (duration := _duration(direction.findtext(key)))]
                    add(row, kind, "; ".join(parts))
    # Closures (often GA-only restrictions), CTOP and unknown program types
    # deliberately do not become airline closures or flight cancellations.
    return {"issued_at": issued, "events": events}


def _snapshot_context(snapshot):
    """Only selection fields in RAM; no roster, IDs or source text on disk."""
    if not isinstance(snapshot, dict) or snapshot.get("demo", True):
        return {"demo": True, "flights": []}
    flights = snapshot.get("flights", [])
    return {"demo": False, "flights": [
        {key: row[key] for key in ("origin", "destination", "start", "end", "deadhead") if key in row}
        for row in (flights[:2000] if isinstance(flights, list) else []) if isinstance(row, dict)]}


def _radar_context(snapshot, config, now):
    """Whole household-local flight day/day before; never observed presence."""
    if snapshot.get("demo", True):
        return False
    try:
        zone = ZoneInfo(config.get("timezone", "UTC"))
    except (KeyError, TypeError, ValueError):
        return False
    today = now.astimezone(zone).date()
    tomorrow = today + timedelta(days=1)
    for flight in snapshot.get("flights", []):
        if flight.get("deadhead") and config.get("show_deadheads", True) is not True:
            continue
        try:
            start, end = _stamp(flight.get("start")), _stamp(flight.get("end"))
            if (0 < (end - start).total_seconds() <= 86400
                    and start.astimezone(zone).date() <= tomorrow
                    and end.astimezone(zone).date() >= today):
                return True
        except (ValueError, TypeError, OverflowError):
            continue
    return any("layover" in row["roles"] for row in select_airports(
        snapshot, now, timezone=config.get("timezone", "UTC")))


class FlightEnvironmentFeed:
    """Independent bounded poller; status is local and coordinate/path free."""

    def __init__(self, data_dir, config_getter):
        self.path = Path(data_dir) / "flight-environment.json"
        self.image_path = Path(data_dir) / "national-radar.png"
        self.config_getter = config_getter
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._last_attempt = {key: -math.inf for key in POLL_SECONDS}
        self._failed = {key: False for key in POLL_SECONDS}
        self._snapshot = {"demo": True, "flights": []}
        self._cache = {}
        try:
            if self.path.stat().st_size <= 300_000:
                cache = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(cache, dict) and cache.get("schema") == 1:
                    for key in POLL_SECONDS:
                        row = cache.get(key)
                        if isinstance(row, dict):
                            fetched, issued = _iso(_stamp(row.get("fetched_at"))), row.get("issued_at")
                            issued = _iso(_stamp(issued)) if issued is not None else None
                            parsed = {"fetched_at": fetched, "issued_at": issued}
                            if key == "radar" and isinstance(row.get("image_sha256"), str) and _HASH.fullmatch(row["image_sha256"]):
                                parsed["image_sha256"] = row["image_sha256"]
                            elif key == "airport_alerts" and issued and isinstance(row.get("events"), list) and len(row["events"]) <= 500:
                                parsed["events"] = [
                                    {"airport": event["airport"], "kind": event["kind"],
                                     "reason": _text(event.get("reason")), "delay_text": _text(event.get("delay_text"))}
                                    for event in row["events"] if isinstance(event, dict)
                                    and isinstance(event.get("airport"), str) and _IATA.fullmatch(event["airport"])
                                    and event.get("kind") in _KINDS]
                            else:
                                continue
                            self._cache[key] = parsed
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            self._cache = {}

    def _mode(self, key, config, snapshot, now):
        if config.get("demo", True) or snapshot.get("demo", True):
            return "demo"
        flag = "national_radar_enabled" if key == "radar" else "airport_alerts_enabled"
        if config.get(flag) is not True:
            return "off"
        eligible = (_radar_context(snapshot, config, now) if key == "radar" else
                    bool(select_airports(snapshot, now, timezone=config.get("timezone", "UTC"))))
        return "enabled" if eligible else "outside_window"

    def poll(self, snapshot, now=None):
        now = _now(now)
        with self._lock:
            self._snapshot = _snapshot_context(snapshot)
        if not self._poll_lock.acquire(blocking=False):
            return self.status(now)
        try:
            for key, url in (("radar", RADAR_URL), ("airport_alerts", FAA_URL)):
                with self._lock:
                    context = copy.deepcopy(self._snapshot)
                if self._mode(key, self.config_getter(), context, now) != "enabled":
                    continue
                tick = time.monotonic()
                with self._lock:
                    if tick - self._last_attempt[key] < POLL_SECONDS[key]:
                        continue
                    self._last_attempt[key] = tick
                try:
                    raw, modified = request_bytes(url)
                    if key == "radar":
                        png = _radar_png(raw)
                        row = {"fetched_at": _iso(now), "issued_at": _source_header(modified),
                               "image_sha256": hashlib.sha256(png).hexdigest()}
                    else:
                        row = {"fetched_at": _iso(now), **_parse_faa(raw)}
                    with self._lock:
                        # Consent/DEMO/context may have changed during the request.
                        if self._mode(key, self.config_getter(), self._snapshot, now) != "enabled":
                            continue
                        if key == "radar":
                            _private_bytes(self.image_path, png)
                        self._cache[key] = row
                        self._failed[key] = False
                        private_json(self.path, {"schema": 1, **self._cache})
                except (OSError, ValueError, TypeError, KeyError, OverflowError, ET.ParseError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                    with self._lock:
                        self._failed[key] = True
            return self.status(now)
        finally:
            self._poll_lock.release()

    def status(self, now=None):
        now = _now(now)
        config = self.config_getter()
        with self._lock:
            snapshot, cache, failed = copy.deepcopy(self._snapshot), copy.deepcopy(self._cache), self._failed.copy()
        selected = {row["iata"] for row in select_airports(snapshot, now, timezone=config.get("timezone", "UTC"))}
        result = {}
        for key in POLL_SECONDS:
            radar = key == "radar"
            mode = self._mode(key, config, snapshot, now)
            row = cache.get(key, {}) if mode == "enabled" else {}
            section = {"status": mode, "fetched_at": row.get("fetched_at"), "issued_at": row.get("issued_at"),
                       "source": "NOAA/NWS national radar" if radar else "FAA airport status",
                       "source_url": RADAR_URL if radar else "https://nasstatus.faa.gov/"}
            if radar:
                available = False
                try:
                    # The GUI additionally hash-binds its read to this generation.
                    available = bool(row.get("image_sha256")) and 0 < self.image_path.stat().st_size <= MAX_RESPONSE[RADAR_URL]
                except OSError:
                    pass
                section.update(source_time=row.get("issued_at"), image_available=available,
                               image_sha256=row.get("image_sha256") if available else None)
            else:
                section["events"] = [event for event in row.get("events", []) if event["airport"] in selected]
            if mode == "enabled":
                try:
                    fetched_age = (now - _stamp(row["fetched_at"])).total_seconds()
                    issued = row.get("issued_at")
                    if radar and not section["image_available"]:
                        section["status"] = "unavailable"
                    elif radar and issued is None:
                        section["status"] = "stale" if failed[key] else "unknown_time"
                    else:
                        age = (now - _stamp(issued)).total_seconds()
                        section["status"] = ("ready" if not failed[key] and 0 <= fetched_age < FETCH_MAX_AGE[key]
                                             and 0 <= age < SOURCE_MAX_AGE[key] else "stale")
                except (KeyError, ValueError, TypeError, OverflowError):
                    section["status"] = "unavailable"
            result[key] = section
        return result
