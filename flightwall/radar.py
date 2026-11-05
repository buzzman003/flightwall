"""Bounded NOAA observation loops and fixed public regional viewports.

GIF playback delays are animation timing, never observation timestamps. The
standard NOAA loops retain their painted UTC clocks. WMS frames request exact
times advertised by NOAA's CONUS BREF_QCD capabilities, without a home point.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
from io import BytesIO
import math
import re
import urllib.parse
import warnings
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw

UTC = timezone.utc
NATIONAL_URL = "https://radar.weather.gov/ridge/standard/CONUS_loop.gif"
SOUTHEAST_URL = "https://radar.weather.gov/ridge/standard/SOUTHEAST_loop.gif"
WMS_BASE = "https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows"
CAPABILITIES_URL = WMS_BASE + "?service=WMS&version=1.3.0&request=GetCapabilities"
LEGEND_URL = WMS_BASE + "?service=WMS&version=1.3.0&request=GetLegendGraphic&format=image%2Fpng&width=500&height=30&layer=conus_bref_qcd"
MAX_INPUT_BYTES = 10_000_000
MAX_ANIMATION_BYTES = 16_000_000
MAX_POSTER_BYTES = 4_000_000
MAX_FRAMES = 10
MAX_FRAME_PIXELS = 10_000_000
MAX_TOTAL_PIXELS = 24_000_000
MAX_OUTPUT_SIZE = (2048, 1024)
SCOPES = frozenset({"national", "southeast", "local", "hyperlocal"})
SCOPE_LABELS = {"national": "NATIONAL", "southeast": "SOUTHEAST", "local": "DESTIN / 200 MI",
                "hyperlocal": "FORT WALTON / DESTIN"}
# Public Destin Executive Airport reference, not any household location.
PUBLIC_CENTER = (30.4001, -86.4715)
_DEGREES = 69.09342
_LAT_RANGE = 200 / _DEGREES
_LON_RANGE = 200 / (_DEGREES * math.cos(math.radians(PUBLIC_CENTER[0])))
BOUNDS = {"national": (-126.0, 23.0, -65.0, 51.0),
          "southeast": (-94.0, 23.0, -74.0, 38.0),
          "local": (PUBLIC_CENTER[1] - _LON_RANGE, PUBLIC_CENTER[0] - _LAT_RANGE,
                    PUBLIC_CENTER[1] + _LON_RANGE, PUBLIC_CENTER[0] + _LAT_RANGE),
          "hyperlocal": (-86.82, 30.28, -86.15, 30.63)}
SIZES = {"national": (1600, 760), "southeast": (1200, 900),
         "local": (900, 900), "hyperlocal": (1200, 724)}
FOOTER_HEIGHT = 74
_HASH = re.compile(r"[0-9a-f]{64}")


def stamp(value):
    if (not isinstance(value, str) or len(value) > 40
            or re.search(r"\.\d{7,}", value)):
        raise ValueError("Invalid radar clock")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Radar clocks require a timezone")
    return result.astimezone(UTC)


def _observation_iso(value):
    return value.isoformat(timespec="microseconds" if value.microsecond else "seconds")


def _bbox(scope):
    return ",".join(f"{number:.6f}" for number in BOUNDS[scope])


def map_geometry(scope):
    """Public fixed-grid identity, independent of any household coordinate."""
    return {"crs": "CRS:84", "bounds": [float(value) for value in _bbox(scope).split(",")],
            "map_size": list(SIZES[scope]), "footer_height": FOOTER_HEIGHT}


def mark_home(image, scope, latitude, longitude):
    """Render a local marker on a verified WMS frame before letterboxing."""
    if (scope not in BOUNDS or not isinstance(image, Image.Image) or image.mode not in {"RGB", "RGBA"}
            or image.size != (SIZES[scope][0], SIZES[scope][1] + FOOTER_HEIGHT)
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in (latitude, longitude))):
        return image
    west, south, east, north = map_geometry(scope)["bounds"]
    if not west <= longitude <= east or not south <= latitude <= north:
        return image
    width, height = SIZES[scope]
    x = round((longitude - west) * (width - 1) / (east - west))
    y = round((north - latitude) * (height - 1) / (north - south))
    marked = image.copy()
    area = marked.crop((0, 0, width, height))
    draw = ImageDraw.Draw(area)
    from .render import CYAN, WHITE, _text
    radius = max(7, min(12, width // 100))
    for color, stroke in (((0, 0, 0), 6), (CYAN, 2)):
        draw.line((x - radius, y, x + radius, y), fill=color, width=stroke)
        draw.line((x, y - radius, x, y + radius), fill=color, width=stroke)
    draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=WHITE)
    label_x = x + radius + 5 if x + radius + 55 < width else x - radius - 55
    label_x = max(2, min(width - 52, label_x))
    label_y = max(2, min(height - 16, y - 6))
    draw.rectangle((label_x - 2, label_y - 2, label_x + 48, label_y + 15), fill=(0, 0, 0))
    _text(draw, (label_x, label_y), "HOME", WHITE, 2)
    marked.paste(area, (0, 0))
    return marked


def map_url(scope, observed_at):
    if scope not in BOUNDS:
        raise ValueError("Unexpected radar viewport")
    observed_at = _observation_iso(stamp(observed_at)).replace("+00:00", "Z")
    width, height = SIZES[scope]
    return WMS_BASE + "?" + urllib.parse.urlencode({"service": "WMS", "version": "1.3.0", "request": "GetMap",
        "layers": "conus_bref_qcd", "styles": "radar_reflectivity", "format": "image/png", "transparent": "true",
        "crs": "CRS:84", "bbox": _bbox(scope), "width": str(width), "height": str(height), "time": observed_at})


def request_limit(url):
    """Allow only fixed NOAA sources or exact public-box/time map requests."""
    if url in {NATIONAL_URL, SOUTHEAST_URL}:
        return MAX_INPUT_BYTES
    if url == CAPABILITIES_URL:
        return 2_000_000
    if url == LEGEND_URL:
        return 200_000
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "opengeo.ncep.noaa.gov" or parsed.fragment:
        raise ValueError("Unexpected radar endpoint")
    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    times = query.get("time")
    if not isinstance(times, list) or len(times) != 1:
        raise ValueError("Unexpected radar time query")
    for scope in BOUNDS:
        if url == map_url(scope, times[0]):
            return 4_000_000
    raise ValueError("Unexpected radar endpoint or private viewport")


def _duration(value, *, static=False):
    if value is None and static:
        return 1000
    if type(value) not in (int, float) or not math.isfinite(value) or int(value) != value or not 20 <= value <= 10000:
        raise ValueError("Invalid radar playback delay")
    return int(value)


def decode_loop(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_INPUT_BYTES:
        raise ValueError("Invalid radar loop size")
    frames, durations, total = [], [], 0
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as source:
            count = getattr(source, "n_frames", 1)
            if source.format not in {"GIF", "PNG"} or not 1 <= count <= MAX_FRAMES:
                raise ValueError("Invalid radar frame count")
            for index in range(count):
                source.seek(index)
                pixels = source.width * source.height
                total += pixels
                if not 0 < pixels <= MAX_FRAME_PIXELS or total > MAX_TOTAL_PIXELS:
                    raise ValueError("Radar decoded pixels exceed bounds")
                durations.append(_duration(source.info.get("duration"), static=count == 1))
                source.load()
                frame = source.convert("RGB")
                frame.thumbnail(MAX_OUTPUT_SIZE, Image.Resampling.LANCZOS)
                frame.info.clear()
                frames.append(frame)
    return frames, durations


def encode_loop(frames, durations, observed_at=None):
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES or len(frames) != len(durations):
        raise ValueError("Invalid radar frame sequence")
    if any(not isinstance(frame, Image.Image) or frame.mode != "RGB" or not 1 <= frame.width <= 2048
           or not 1 <= frame.height <= 1024 or frame.size != frames[0].size for frame in frames):
        raise ValueError("Invalid radar presentation frames")
    if sum(frame.width * frame.height for frame in frames) > MAX_TOTAL_PIXELS:
        raise ValueError("Radar presentation pixels exceed bounds")
    durations = [_duration(value) for value in durations]
    poster, animation = BytesIO(), BytesIO()
    frames[-1].save(poster, format="PNG", optimize=True)
    frames[0].save(animation, format="PNG", save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, disposal=0, blend=0)
    poster, animation = poster.getvalue(), animation.getvalue()
    if len(poster) > MAX_POSTER_BYTES or len(animation) > MAX_ANIMATION_BYTES:
        raise ValueError("Radar cache exceeds bounds")
    # Pillow may merge identical adjacent frames. Match the actual sanitized
    # file, rather than advertising phantom frames or changing observation times.
    actual_frames, actual_durations = decode_cached(animation)
    if observed_at is not None and len(actual_frames) != len(frames):
        raise ValueError("Timestamped radar frames were unexpectedly merged")
    meta = {"sha256": hashlib.sha256(animation).hexdigest(), "frame_count": len(actual_frames),
            "durations_ms": actual_durations, "total_duration_ms": sum(actual_durations),
            "observed_at": observed_at if observed_at is not None else [None] * len(actual_frames)}
    validate_animation(meta)
    return poster, animation, meta


def decode_cached(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_ANIMATION_BYTES:
        raise ValueError("Invalid cached radar size")
    # Cached PNG limits differ from network input bytes but share frame/pixel bounds.
    frames, durations, pixels = [], [], 0
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as source:
            count = getattr(source, "n_frames", 1)
            if source.format != "PNG" or not 1 <= count <= MAX_FRAMES:
                raise ValueError("Invalid cached radar format")
            for index in range(count):
                source.seek(index)
                if not 1 <= source.width <= 2048 or not 1 <= source.height <= 1024:
                    raise ValueError("Invalid cached radar dimensions")
                pixels += source.width * source.height
                if pixels > MAX_TOTAL_PIXELS:
                    raise ValueError("Cached radar pixels exceed bounds")
                durations.append(_duration(source.info.get("duration"), static=count == 1))
                source.load()
                frame = source.convert("RGB")
                frame.info.clear()
                frames.append(frame)
    return frames, durations


def validate_animation(meta):
    if not isinstance(meta, dict) or set(meta) != {"sha256", "frame_count", "durations_ms", "total_duration_ms", "observed_at"}:
        raise ValueError("Invalid radar animation metadata")
    count, durations = meta["frame_count"], meta["durations_ms"]
    if (not isinstance(meta["sha256"], str) or not _HASH.fullmatch(meta["sha256"]) or type(count) is not int
            or not 1 <= count <= MAX_FRAMES or not isinstance(durations, list) or len(durations) != count
            or [_duration(value) for value in durations] != durations or type(meta["total_duration_ms"]) is not int
            or sum(durations) != meta["total_duration_ms"] or not 20 <= sum(durations) <= 100000):
        raise ValueError("Invalid radar animation timing")
    times = meta["observed_at"]
    if not isinstance(times, list) or len(times) != count:
        raise ValueError("Invalid radar frame clocks")
    if any(value is not None for value in times):
        stamps = [stamp(value) for value in times]
        if any(a >= b for a, b in zip(stamps, stamps[1:])) or stamps[-1] - stamps[0] > timedelta(hours=2):
            raise ValueError("Invalid radar observation order")
    return meta


def frame_index(meta, seconds):
    validate_animation(meta)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Invalid radar playback clock")
    elapsed = (seconds * 1000) % meta["total_duration_ms"]
    for index, duration in enumerate(meta["durations_ms"]):
        if elapsed < duration:
            return index
        elapsed -= duration
    return 0


def observation_times(raw, now):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 2_000_000 or b"\x00" in raw or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
        raise ValueError("Invalid NOAA radar capabilities")
    root = ET.fromstring(raw)
    ns = {"w": "http://www.opengis.net/wms"}
    if root.tag != "{http://www.opengis.net/wms}WMS_Capabilities" or sum(1 for _ in root.iter()) > 10000:
        raise ValueError("Unexpected NOAA radar capabilities")
    layers = [node for node in root.findall(".//w:Layer", ns) if node.findtext("w:Name", namespaces=ns) == "conus_bref_qcd"]
    if len(layers) != 1:
        raise ValueError("NOAA reflectivity layer unavailable")
    dimensions = [node for node in layers[0].findall("w:Dimension", ns) if node.get("name") == "time"]
    if len(dimensions) != 1 or dimensions[0].get("units") != "ISO8601":
        raise ValueError("NOAA observation clocks unavailable")
    values = (dimensions[0].text or "").split(",")
    if not 1 <= len(values) <= 200:
        raise ValueError("NOAA observation clock list exceeds bounds")
    times = sorted({stamp(value.strip()) for value in values})
    times = [value for value in times if now - timedelta(hours=2) <= value <= now]
    if not times or now - times[-1] >= timedelta(minutes=30):
        raise ValueError("NOAA radar observations are stale")
    return [_observation_iso(value) for value in times[-MAX_FRAMES:]]


@lru_cache(maxsize=2)
def background(scope):
    """Bundled public geography in the exact CRS:84 WMS rectangle."""
    from .nearby_map import LAND, MAJOR_ROAD, ROAD, RUNWAY, SHORE, WATER, HIGHWAY_98, _load_asset
    from .operating_map import _world
    width, height = SIZES[scope]
    west, south, east, north = map_geometry(scope)["bounds"]
    project = lambda point: ((point[0] - west) * width / (east - west), (north - point[1]) * height / (north - south))
    image = Image.new("RGB", (width, height), WATER)
    draw = ImageDraw.Draw(image)
    asset = _load_asset() if scope == "hyperlocal" else None
    if asset is not None and all(abs(a - b) < 1e-6 for a, b in zip(asset[0], BOUNDS[scope])):
        image.info["map_attribution"] = "OSM ODbL"
        _, features = asset
        for kind in ("land", "water"):
            for feature_kind, rings, _, _ in features:
                if feature_kind == kind:
                    fill, hole = (LAND, WATER) if kind == "land" else (WATER, LAND)
                    draw.polygon([project(p) for p in rings[0]], fill=fill, outline=SHORE)
                    for ring in rings[1:]:
                        draw.polygon([project(p) for p in ring], fill=hole, outline=SHORE)
        for kind, rings, road_class, highway in features:
            if kind in {"road", "runway"}:
                color = RUNWAY if kind == "runway" else HIGHWAY_98 if highway else MAJOR_ROAD if road_class in {"primary", "trunk", "motorway"} else ROAD
                for ring in rings:
                    draw.line([project(p) for p in ring], fill=color, width=2 if highway or kind == "runway" else 1)
    else:
        world = _world()
        image.info["map_attribution"] = "NATURAL EARTH / LIMITED DETAIL" if world else "MAP GEOGRAPHY UNAVAILABLE"
        if not world:
            image.paste((5, 15, 21), (0, 0, width, height))
        if world:
            for ring in world["land"]:
                draw.polygon([project(p) for p in ring], fill=LAND, outline=SHORE)
            for line in world["states"]:
                draw.line([project(p) for p in line], fill=SHORE)
    return image


def compose_wms(raw, scope, observed_at, legend=None):
    if scope not in BOUNDS or not isinstance(raw, bytes) or not 1 <= len(raw) <= 4_000_000:
        raise ValueError("Invalid scoped radar image")
    with Image.open(BytesIO(raw)) as source:
        if source.format != "PNG" or source.size != SIZES[scope] or getattr(source, "n_frames", 1) != 1:
            raise ValueError("Unexpected scoped radar dimensions")
        source.load()
        overlay = source.convert("RGBA")
    base = background(scope)
    attribution = base.info.get("map_attribution", "BUNDLED GEOGRAPHY")
    image = base.convert("RGBA")
    image.alpha_composite(overlay)
    map_image = image.convert("RGB")
    image = Image.new("RGB", (map_image.width, map_image.height + FOOTER_HEIGHT), (3, 12, 18))
    image.paste(map_image, (0, 0))
    draw = ImageDraw.Draw(image)
    from .render import CYAN, MUTED, WHITE, _text
    scale = 2
    time_label = stamp(observed_at).strftime("%m/%d %H:%M:%S UTC")
    _text(draw, (8, image.height - 68), "NOAA MRMS OBSERVED " + time_label, WHITE, scale)
    _text(draw, (8, image.height - 17), "REFLECTIVITY / NATIVE GRID LIMIT / " + attribution, MUTED)
    if legend is not None:
        if not isinstance(legend, bytes) or not 1 <= len(legend) <= 200000:
            raise ValueError("Invalid NOAA legend")
        with Image.open(BytesIO(legend)) as source:
            if source.format != "PNG" or not 1 <= source.width <= 800 or not 1 <= source.height <= 100:
                raise ValueError("Invalid NOAA legend dimensions")
            source.load()
            key = source.convert("RGBA")
            key.thumbnail((600, 30), Image.Resampling.LANCZOS)
            image.paste(key, (8, image.height - 48), key)
            _text(draw, (key.width + 18, image.height - 38), "dBZ", WHITE)
    else:
        _text(draw, (8, image.height - 45), "dBZ LEGEND UNAVAILABLE", MUTED)
    _text(draw, (8, 8), SCOPE_LABELS[scope], CYAN, 2)
    return image
