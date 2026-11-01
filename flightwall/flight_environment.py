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
from . import radar as radar_tools

UTC = timezone.utc
RADAR_URL = radar_tools.NATIONAL_URL
FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
POLL_SECONDS = {"radar": 600, "airport_alerts": 300}
FETCH_MAX_AGE = {"radar": 1200, "airport_alerts": 600}
SOURCE_MAX_AGE = {"radar": 1800, "airport_alerts": 900}
MAX_RESPONSE = {RADAR_URL: 10_000_000, radar_tools.SOUTHEAST_URL: 10_000_000,
                radar_tools.CAPABILITIES_URL: 2_000_000, radar_tools.LEGEND_URL: 200_000,
                FAA_URL: 2_000_000}
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
    """Fixed official HTTPS sources/public radar boxes, no redirects/proxies."""
    limit = MAX_RESPONSE[FAA_URL] if url == FAA_URL else radar_tools.request_limit(url)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = urllib.request.Request(url, headers={
        "User-Agent": "FlightWall/" + __version__ + " (https://github.com/buzzman003/flightwall)",
        "Accept": "application/xml" if url in {FAA_URL, radar_tools.CAPABILITIES_URL} else "image/gif,image/png",
        "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=10) as response:
        if response.geturl() != url or response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("Unexpected flight environment response")
        raw = response.read(limit + 1)
        if not raw or len(raw) > limit:
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
    if config.get("national_radar_flight_only", True) is False:
        return True
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
        self.animation_path = Path(data_dir) / "national-radar-loop.png"
        self.config_getter = config_getter
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._last_attempt = {key: -math.inf for key in POLL_SECONDS}
        self._failed = {key: False for key in POLL_SECONDS}
        self._snapshot = {"demo": True, "flights": []}
        self._cache = {}
        self._radar_scope = None
        self._radar_legend = None
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
                                scope = row.get("scope", "national")
                                if scope not in radar_tools.SCOPES:
                                    continue
                                basis = row.get("clock_basis", "embedded_utc")
                                if basis not in {"embedded_utc", "advertised_observation"}:
                                    continue
                                parsed.update(scope=scope, clock_basis=basis)
                                if row.get("animation") is not None:
                                    try:
                                        parsed["animation"] = radar_tools.validate_animation(row["animation"])
                                    except (ValueError, TypeError, KeyError, OverflowError):
                                        pass  # A valid latest poster can still be displayed.
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
        if key == "radar" and config.get("national_radar_scope", "national") not in radar_tools.SCOPES:
            return "unconfigured"
        eligible = (_radar_context(snapshot, config, now) if key == "radar" else
                    bool(select_airports(snapshot, now, timezone=config.get("timezone", "UTC"))))
        return "enabled" if eligible else "outside_window"

    def _radar_files_match(self, row):
        try:
            for path, digest, limit in ((self.image_path, row.get("image_sha256"), radar_tools.MAX_POSTER_BYTES),
                                        (self.animation_path, (row.get("animation") or {}).get("sha256"), radar_tools.MAX_ANIMATION_BYTES)):
                if path.is_symlink() or not isinstance(digest, str) or not _HASH.fullmatch(digest):
                    return False
                with path.open("rb") as stream:
                    raw = stream.read(limit + 1)
                if len(raw) > limit or hashlib.sha256(raw).hexdigest() != digest:
                    return False
            return True
        except OSError:
            return False

    def _fetch_radar(self, scope, now):
        """Only the chosen public scope; never phone/home coordinates or tiles."""
        observed = None
        if scope in {"national", "southeast"}:
            url = RADAR_URL if scope == "national" else radar_tools.SOUTHEAST_URL
            raw, modified = request_bytes(url)
            frames, durations = radar_tools.decode_loop(raw)
            issued, basis = _source_header(modified), "embedded_utc"
        else:
            raw, _ = request_bytes(radar_tools.CAPABILITIES_URL)
            observed = radar_tools.observation_times(raw, now)
            if not self._radar_requested(scope, now):
                raise ValueError("Radar scope/consent changed")
            with self._lock:
                previous = copy.deepcopy(self._cache.get("radar", {}))
            if (previous.get("scope") == scope and previous.get("animation", {}).get("observed_at") == observed
                    and self._radar_files_match(previous)):
                return dict(previous, fetched_at=_iso(now)), None, None
            if self._radar_legend is None:
                self._radar_legend, _ = request_bytes(radar_tools.LEGEND_URL)
            frames = []
            for at in observed:
                if not self._radar_requested(scope, now):
                    raise ValueError("Radar scope/consent changed")
                raw, _ = request_bytes(radar_tools.map_url(scope, at))
                frames.append(radar_tools.compose_wms(raw, scope, at, self._radar_legend))
            durations = [200] * len(frames)
            durations[-1] = 1500
            issued, basis = observed[-1], "advertised_observation"
        poster, animation, metadata = radar_tools.encode_loop(frames, durations, observed)
        row = {"fetched_at": _iso(now), "issued_at": issued, "image_sha256": hashlib.sha256(poster).hexdigest(),
               "scope": scope, "clock_basis": basis, "animation": metadata}
        return row, poster, animation

    def _radar_requested(self, scope, now):
        config = self.config_getter()
        with self._lock:
            context = copy.deepcopy(self._snapshot)
        return (config.get("national_radar_scope", "national") == scope
                and self._mode("radar", config, context, now) == "enabled")

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
                    scope = self.config_getter().get("national_radar_scope", "national") if key == "radar" else None
                    if key == "radar" and self._radar_scope != scope:
                        self._radar_scope, self._last_attempt[key] = scope, -math.inf
                    if tick - self._last_attempt[key] < POLL_SECONDS[key]:
                        continue
                    self._last_attempt[key] = tick
                try:
                    if key == "radar":
                        row, png, animation = self._fetch_radar(scope, now)
                    else:
                        raw, _ = request_bytes(url)
                        row = {"fetched_at": _iso(now), **_parse_faa(raw)}
                    with self._lock:
                        # Consent/DEMO/context may have changed during the request.
                        config = self.config_getter()
                        if (self._mode(key, config, self._snapshot, now) != "enabled"
                                or key == "radar" and config.get("national_radar_scope", "national") != scope):
                            continue
                        if key == "radar" and png is not None:
                            _private_bytes(self.animation_path, animation)
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
            scope = config.get("national_radar_scope", "national") if radar else None
            if radar and row.get("scope", "national") != scope:
                row = {}
            section = {"status": mode, "fetched_at": row.get("fetched_at"), "issued_at": row.get("issued_at"),
                       "source": "NOAA/NWS national radar" if radar else "FAA airport status",
                       "source_url": (RADAR_URL if scope == "national" else radar_tools.SOUTHEAST_URL if scope == "southeast"
                                      else "https://opengeo.ncep.noaa.gov/geoserver/www/index.html") if radar else "https://nasstatus.faa.gov/"}
            if radar:
                available = False
                try:
                    # The GUI additionally hash-binds its read to this generation.
                    available = bool(row.get("image_sha256")) and not self.image_path.is_symlink() and 0 < self.image_path.stat().st_size <= radar_tools.MAX_POSTER_BYTES
                except OSError:
                    pass
                section.update(source_time=row.get("issued_at"), image_available=available,
                               image_sha256=row.get("image_sha256") if available else None,
                               scope=scope, scope_label=radar_tools.SCOPE_LABELS.get(scope, "UNKNOWN"),
                               clock_basis=row.get("clock_basis", "embedded_utc"), animation=None)
                try:
                    meta = radar_tools.validate_animation(row.get("animation"))
                    if not self.animation_path.is_symlink() and 0 < self.animation_path.stat().st_size <= radar_tools.MAX_ANIMATION_BYTES:
                        section["animation"] = copy.deepcopy(meta)
                except (OSError, ValueError, TypeError, KeyError, OverflowError):
                    pass
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
                        if radar and section.get("animation"):
                            times = section["animation"]["observed_at"]
                            if any(at is not None for at in times):
                                observations = [radar_tools.stamp(at) for at in times]
                                if (any(not 0 <= (now - at).total_seconds() < 7200 for at in observations)
                                        or observations[-1] != _stamp(issued)):
                                    section["status"] = "stale"
                except (KeyError, ValueError, TypeError, OverflowError):
                    section["status"] = "unavailable"
            result[key] = section
        return result
