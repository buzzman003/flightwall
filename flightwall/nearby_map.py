"""Local high-resolution geography for the nearby-aircraft presentation layer.

The normal board remains 512x256. This module renders only its geographic
inset at the actual presentation size, without the LED postprocess. It never
performs network requests or writes the private display center to disk.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import gzip
import json
import math
from pathlib import Path
import re

from PIL import Image, ImageDraw


ASSET_PATH = Path(__file__).resolve().parent.parent / "assets" / "maps" / "fort-walton-destin.json"
REGIONAL_ASSET_PATH = ASSET_PATH.with_name("gulf-coast.json.gz")
MAX_ASSET_BYTES = 12 * 1024 * 1024
WATER = (6, 25, 40)
LAND = (19, 39, 39)
SHORE = (52, 88, 96)
ROAD = (48, 67, 69)
MAJOR_ROAD = (124, 126, 111)
HIGHWAY_98 = (227, 184, 100)
RUNWAY = (174, 190, 192)
CYAN = (83, 214, 222)
MUTED = (117, 151, 158)
WHITE = (233, 241, 230)
MILES_PER_DEGREE = 69.09342


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _ring(value):
    if not isinstance(value, list) or not 2 <= len(value) <= 100000:
        raise ValueError("Invalid map geometry")
    for point in value:
        if (not isinstance(point, list) or len(point) != 2 or not all(_number(v) for v in point)
                or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
            raise ValueError("Invalid map coordinate")
    return tuple(tuple(point) for point in value)


@lru_cache(maxsize=1)
def _load_asset():
    return _read_asset(ASSET_PATH, "ODbL-1.0")


@lru_cache(maxsize=1)
def _load_regional_asset():
    return _read_asset(REGIONAL_ASSET_PATH, "public-domain", compressed=True)


def _read_asset(path, license_name, *, compressed=False):
    """Only bundled, bounded, explicitly licensed map data is accepted."""
    try:
        if path.stat().st_size > MAX_ASSET_BYTES:
            return None
        if compressed:
            with gzip.open(path, "rb") as source:
                contents = source.read(MAX_ASSET_BYTES + 1)
            if len(contents) > MAX_ASSET_BYTES:
                return None
        else:
            contents = path.read_text(encoding="utf-8")
        data = json.loads(contents)
        if not isinstance(data, dict):
            return None
        bounds = data.get("bounds")
        if (data.get("schema") != 1 or data.get("license") != license_name
                or not isinstance(bounds, list) or len(bounds) != 4 or not all(_number(v) for v in bounds)
                or not -180 <= bounds[0] < bounds[2] <= 180 or not -90 <= bounds[1] < bounds[3] <= 90):
            return None
        features = data.get("features")
        if not isinstance(features, list) or not 1 <= len(features) <= 50000:
            return None
        parsed, total_points = [], 0
        for feature in features:
            if not isinstance(feature, dict) or feature.get("kind") not in {"land", "water", "road", "runway"}:
                return None
            geometry = feature.get("geometry")
            if not isinstance(geometry, dict):
                return None
            kind = feature["kind"]
            if kind in {"land", "water"}:
                values = geometry.get("coordinates")
                if geometry.get("type") != "Polygon" or not isinstance(values, list) or not 1 <= len(values) <= 2000:
                    return None
                rings = tuple(_ring(ring) for ring in values)
                if any(len(ring) < 4 or ring[0] != ring[-1] for ring in rings):
                    return None
                total_points += sum(map(len, rings))
            else:
                if geometry.get("type") != "LineString":
                    return None
                rings = (_ring(geometry.get("coordinates")),)
                total_points += len(rings[0])
            if total_points > 300000:
                return None
            # Reject coordinate-range corruption even though Pillow clips to
            # the image. A regional asset is not an arbitrary drawing command.
            if any(not bounds[0] - 1e-6 <= lon <= bounds[2] + 1e-6
                   or not bounds[1] - 1e-6 <= lat <= bounds[3] + 1e-6
                   for ring in rings for lon, lat in ring):
                return None
            parsed.append((kind, rings, feature.get("road_class"), feature.get("highway_98") is True))
        if not any(feature[0] == "land" for feature in parsed):
            return None
        return tuple(bounds), tuple(parsed)
    except (OSError, EOFError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return None


def _parameters(config, size):
    if not isinstance(config, dict) or config.get("nearby_enabled") is not True or config.get("demo", True) is not False:
        return None
    latitude, longitude = config.get("home_latitude"), config.get("home_longitude")
    if not _number(latitude) or not _number(longitude) or not -85 <= latitude <= 85 or not -180 <= longitude <= 180:
        return None
    radius = config.get("nearby_radius_miles")
    if radius is None:
        nautical = config.get("nearby_radius_nm")
        radius = nautical * 1.15078 if _number(nautical) else None
    if not _number(radius) or not .1 <= radius <= 250:
        return None
    if (not isinstance(size, (tuple, list)) or len(size) != 2
            or any(type(v) is not int for v in size) or not 256 <= size[0] <= 4096 or not 128 <= size[1] <= 2160):
        return None
    return float(latitude), float(longitude), float(radius), tuple(size)


def _projection(latitude, longitude, radius, size):
    width, height = size
    # Keep radius circles circular on widescreen map insets. The longer axis
    # contains additional geography, but aircraft remain filtered to the radius.
    footer = max(14, round(height * .065))
    usable_height = height - footer
    pixels = max(1e-8, min(width - 12, usable_height - 12) / (2 * radius))
    xscale = MILES_PER_DEGREE * math.cos(math.radians(latitude)) * pixels
    yscale = MILES_PER_DEGREE * pixels
    center = width / 2, usable_height / 2
    def project(point):
        lon, lat = point
        return center[0] + (lon - longitude) * xscale, center[1] - (lat - latitude) * yscale
    extent = (longitude - center[0] / xscale, latitude - (usable_height - center[1]) / yscale,
              longitude + (width - center[0]) / xscale, latitude + center[1] / yscale)
    return project, center, pixels, footer, extent


@lru_cache(maxsize=4)
def _base_map(latitude, longitude, radius, size, draw_radius=True):
    project, center, pixels, footer, extent = _projection(latitude, longitude, radius, size)
    local = _load_asset()
    if local is not None and _covers(local[0], extent):
        features = local[1]
        attribution = "(C) OPENSTREETMAP CONTRIBUTORS / ODbL"
    else:
        regional = _load_regional_asset()
        if regional is None or not _covers(regional[0], extent):
            if local is None and regional is None:
                return None
            return _wide_base_map(latitude, longitude, radius, size, draw_radius)
        features = regional[1]
        attribution = "NATURAL EARTH / US CENSUS / OURAIRPORTS"
    image = Image.new("RGB", size, WATER)
    draw = ImageDraw.Draw(image)
    for kind in ("land", "water"):
        fill, hole = (LAND, WATER) if kind == "land" else (WATER, LAND)
        for feature_kind, rings, _, _ in features:
            if feature_kind != kind:
                continue
            draw.polygon([project(point) for point in rings[0]], fill=fill)
            draw.line([project(point) for point in rings[0]], fill=SHORE, width=1)
            for ring in rings[1:]:
                draw.polygon([project(point) for point in ring], fill=hole)
                draw.line([project(point) for point in ring], fill=SHORE, width=1)
    highway_label = None
    for wanted in ("minor", "major", "98", "runway"):
        for kind, rings, road_class, highway_98 in features:
            major = road_class in {"motorway", "trunk", "primary", "secondary"}
            layer = "runway" if kind == "runway" else "98" if highway_98 else "major" if major else "minor"
            if kind not in {"road", "runway"} or layer != wanted:
                continue
            if radius >= 50 and layer == "minor":
                continue
            points = [project(point) for point in rings[0]]
            if (max(x for x, _ in points) < 0 or min(x for x, _ in points) >= size[0]
                    or max(y for _, y in points) < 0 or min(y for _, y in points) >= size[1] - footer):
                continue
            color = {"minor": ROAD, "major": MAJOR_ROAD, "98": HIGHWAY_98, "runway": RUNWAY}[layer]
            width = max(1, round(size[1] * {"minor": .002, "major": .0035, "98": .005, "runway": .009}[layer]))
            draw.line(points, fill=color, width=width, joint="curve")
            if highway_98 and highway_label is None:
                # Place just one highway label on an actually visible segment,
                # never a guessed road or airport marker.
                for point in points:
                    if 50 < point[0] < size[0] - 95 and 35 < point[1] < size[1] - footer - 35:
                        highway_label = point
                        break
    from .render import _text
    text_scale = max(1, min(3, size[1] // 180, size[0] // 216))
    _text(draw, (8, 8), "N", CYAN, text_scale)
    if draw_radius:
        cx, cy = center
        r = radius * pixels
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(70, 111, 124), width=max(1, text_scale))
        _text(draw, (size[0] - 8, 8), f"{radius:g} MI RADIUS", MUTED, text_scale, align="right")
    if highway_label:
        x, y = highway_label
        label_width, label_height = 29 * text_scale, 11 * text_scale
        draw.rectangle((x - 3, y - 2, x + label_width, y + label_height), fill=(12, 24, 28), outline=HIGHWAY_98)
        _text(draw, (x, y), "US 98", HIGHWAY_98, text_scale)
    draw.rectangle((0, size[1] - footer, size[0] - 1, size[1] - 1), fill=(3, 12, 18))
    # This text is deliberately outside the LED shader. Keep it on every map,
    # including an empty-aircraft view, to satisfy the map-data attribution.
    _text(draw, (5, size[1] - footer + max(1, (footer - 7 * text_scale) // 2)),
          attribution, MUTED, text_scale, max_width=size[0] - 10)
    return image


def _covers(bounds, extent):
    return (bounds[0] <= extent[0] and bounds[1] <= extent[1]
            and bounds[2] >= extent[2] and bounds[3] >= extent[3])


@lru_cache(maxsize=4)
def _wide_base_map(latitude, longitude, radius, size, draw_radius=True):
    """Public low-detail geography when a wider range outgrows local OSM data."""
    from .operating_map import _world_background, _world
    if _world() is None:
        return None
    image = _world_background(latitude, longitude, radius, size).copy()
    draw = ImageDraw.Draw(image)
    _project, center, pixels, footer, _extent = _projection(latitude, longitude, radius, size)
    from .render import _text
    scale = max(1, min(3, size[1] // 180, size[0] // 216))
    _text(draw, (8, 8), "N", CYAN, scale)
    if draw_radius:
        cx, cy = center
        r = radius * pixels
        draw.ellipse((cx-r, cy-r, cx+r, cy+r), outline=(70, 111, 124), width=max(1, scale))
        _text(draw, (size[0]-8, 8), f"{radius:g} MI RADIUS", MUTED, scale, align="right")
    draw.rectangle((0, size[1]-footer, size[0]-1, size[1]-1), fill=(3, 12, 18))
    _text(draw, (5, size[1]-footer+max(1, (footer-7*scale)//2)),
          "NATURAL EARTH / LIMITED DETAIL", MUTED, scale)
    return image


def regional_background(latitude, longitude, radius_miles, size):
    """Public regional geography for any local-only viewport, without a home pin.

    This draws no center cross, traffic circle or aircraft. Operating-flight
    maps can reuse the licensed region without pretending its center is home.
    A viewport outside the local asset returns None so operating-flight maps
    can select and correctly label their own lower-detail world fallback.
    """
    parameters = _parameters({"demo": False, "nearby_enabled": True,
                              "home_latitude": latitude, "home_longitude": longitude,
                              "nearby_radius_miles": radius_miles}, size)
    if parameters is None:
        return None
    latitude, longitude, radius, size = parameters
    asset = _load_asset()
    if asset is None:
        return None
    bounds = asset[0]
    extent = _projection(latitude, longitude, radius, size)[-1]
    if not _covers(bounds, extent):
        return None
    image = _base_map(latitude, longitude, radius, size, False)
    return image.copy() if image is not None else None


def render_map(config, size, rows=(), now=None):
    """Return an independently rendered map patch, or None for radar fallback.

    `config` is local-only private configuration; no home coordinates need be
    added to backend maintenance/status responses. Aircraft coordinates are
    received observations, never interpolated or inferred airport routes.
    """
    parameters = _parameters(config, size)
    if parameters is None:
        return None
    latitude, longitude, radius, size = parameters
    base = _base_map(latitude, longitude, radius, size)
    if base is None:
        return None
    image = base.copy()
    draw = ImageDraw.Draw(image)
    project, center, pixels, footer, _ = _projection(latitude, longitude, radius, size)
    now = now or datetime.now(timezone.utc)
    from .feeds import nearby_distance_miles, parse_time
    valid = {}
    for row in rows[:5000] if isinstance(rows, (tuple, list)) else []:
        if not isinstance(row, dict):
            continue
        try:
            observed = parse_time(row.get("observed_at"))
            if not 0 <= (now - observed).total_seconds() <= 120:
                continue
            lat, lon = row.get("latitude", row.get("lat")), row.get("longitude", row.get("lon"))
            ident = row.get("hex")
            if (not _number(lat) or not _number(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180
                    or not isinstance(ident, str) or not re.fullmatch(r"~?[a-fA-F0-9]{6}", ident)):
                continue
            distance = nearby_distance_miles(latitude, longitude, lat, lon)
            if distance > radius:
                continue
            previous = valid.get(ident.lower())
            if previous is None or observed > previous[0]:
                valid[ident.lower()] = (observed, distance, lon, lat, row)
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
    ranked = sorted(valid.values(), key=lambda item: (item[1], item[4]["hex"]))[:40]
    cx, cy = center
    marker = max(3, round(size[1] / 110))
    draw.ellipse((cx - marker - 2, cy - marker - 2, cx + marker + 2, cy + marker + 2), fill=(3, 12, 18))
    draw.line((cx - marker, cy, cx + marker, cy), fill=WHITE, width=2)
    draw.line((cx, cy - marker, cx, cy + marker), fill=WHITE, width=2)
    from .render import _text
    scale = max(1, min(3, size[1] // 180))
    home_x, home_y = cx + marker + 4, cy + marker + 2
    draw.rectangle((home_x - 2, home_y - 2, home_x + 23 * scale, home_y + 7 * scale + 1), fill=(3, 12, 18))
    _text(draw, (home_x, home_y), "HOME", WHITE, scale)
    for index, (_, _, lon, lat, row) in reversed(list(enumerate(ranked))):
        x, y = project((lon, lat))
        color = CYAN if index < 3 else MUTED
        track = row.get("track")
        if _number(track) and 0 <= track <= 360:
            angle = math.radians(track)
            forward = math.sin(angle), -math.cos(angle)
            right = math.cos(angle), math.sin(angle)
            points = [(x + forward[0] * marker * 1.8, y + forward[1] * marker * 1.8),
                      (x - forward[0] * marker + right[0] * marker, y - forward[1] * marker + right[1] * marker),
                      (x - forward[0] * marker - right[0] * marker, y - forward[1] * marker - right[1] * marker)]
            draw.polygon(points, fill=color, outline=(3, 12, 18))
        else:
            draw.ellipse((x - marker, y - marker, x + marker, y + marker), fill=color, outline=(3, 12, 18))
        if index < 3:
            tx, ty = min(size[0] - 10 * scale, x + marker + 3), min(size[1] - footer - 10 * scale, max(3, y - marker - 8 * scale))
            _text(draw, (tx + 1, ty + 1), str(index + 1), (0, 0, 0), scale)
            _text(draw, (tx, ty), str(index + 1), CYAN, scale)
    return image
