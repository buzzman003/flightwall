"""Observed operating-flight maps with geographic fitting, never fake motion.

All geometry is bundled. No home pin, network request, tiles, location history
or filesystem writes are used. The GUI owns enablement and the high-resolution
presentation inset; both GL and software paths can use the same RGB patch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
import math
from pathlib import Path
import re

from PIL import Image, ImageDraw

from .flight_alerts import fresh_status, stamp
from .nearby_map import CYAN, LAND, MUTED, SHORE, WATER, WHITE, MILES_PER_DEGREE, regional_background

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "maps"
EARTH_MILES = 3958.7613
MIN_RADIUS_MILES = 5.0
ACTIVE_PHASES = frozenset({"scheduled", "departed_gate", "airborne"})


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _point(lat, lon):
    return _number(lat) and _number(lon) and -85 <= lat <= 85 and -180 <= lon <= 180


def _wrap(value):
    return (value + 180) % 360 - 180


def distance_miles(a, b):
    """Great-circle distance between public/received (longitude, latitude) pairs."""
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, math.radians(_wrap(b[0] - a[0]))
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return EARTH_MILES * 2 * math.asin(math.sqrt(min(1.0, max(0.0, hav))))


@lru_cache(maxsize=1)
def _airports():
    try:
        path = ASSETS / "airports.json"
        if path.stat().st_size > 4_000_000:
            return {}
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or len(values) > 20000:
            return {}
        return {code: (row["lon"], row["lat"]) for code, row in values.items()
                if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code) and isinstance(row, dict)
                and _point(row.get("lat"), row.get("lon"))}
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {}


def route_endpoints(flight):
    if not isinstance(flight, dict):
        return None
    values = _airports()
    origin, destination = flight.get("origin"), flight.get("destination")
    if (not isinstance(origin, str) or not isinstance(destination, str)
            or origin not in values or destination not in values or origin == destination):
        return None
    return {"origin": values[origin], "destination": values[destination]}


@lru_cache(maxsize=1)
def _runways():
    """Public airport reference geometry, never a route or a taxi clearance."""
    try:
        path = ASSETS / "airport-runways.json"
        if path.stat().st_size > 3_000_000:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or len(data) > 20000:
            return {}
        result = {}
        for code, rows in data.items():
            if not re.fullmatch(r"[A-Z]{3}", code) or not isinstance(rows, list) or len(rows) > 30:
                return {}
            clean = []
            for row in rows:
                a, b = row.get("a"), row.get("b")
                if (not isinstance(a, list) or not isinstance(b, list) or len(a) != 2 or len(b) != 2
                        or not _point(a[1], a[0]) or not _point(b[1], b[0]) or a == b):
                    return {}
                width = row.get("width_feet")
                clean.append((tuple(a), tuple(b), width if _number(width) and 0 < width <= 1000 else 100))
            result[code] = tuple(clean)
        return result
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return {}


def _along(a, b, fraction):
    from .render import great_circle
    # This is a viewport/context anchor on a plainly labeled direct reference,
    # never an interpolated aircraft observation or a filed flight path.
    if fraction <= 0:
        return a
    if fraction >= 1:
        return b
    points = great_circle(a, b, points=101)
    index = min(99, max(0, int(fraction * 100)))
    local = fraction * 100 - index
    left, right = points[index], points[index + 1]
    return (_wrap(left[0] + _wrap(right[0] - left[0]) * local), left[1] + (right[1] - left[1]) * local)


def projection(viewport, size):
    """North-up miles projection, with short dateline wrapping and footer space."""
    width, height = size
    footer = max(14, round(height * .065))
    content_height = height - footer
    lat, lon, radius = viewport["latitude"], viewport["longitude"], viewport["radius_miles"]
    pixels = min(width - 12, content_height - 12) / (2 * radius)
    xscale = MILES_PER_DEGREE * math.cos(math.radians(lat)) * pixels
    yscale = MILES_PER_DEGREE * pixels
    cx, cy = width / 2, content_height / 2
    def project(point):
        return cx + _wrap(point[0] - lon) * xscale, cy - (point[1] - lat) * yscale
    return project, pixels, footer


def plan_view(flight, status, live, now, size=(1110, 555), *, enabled=True, demo=False, backend_available=True):
    """Fit the route then zoom by received distance remaining, not elapsed time.

    Missing/stale/mismatched observations return None immediately. Landing or
    gate arrival returns None so the caller can resume its ordinary home map;
    this function never modifies or infers a household presence state.
    """
    if (enabled is not True or demo is not False or not backend_available or not isinstance(flight, dict)
            or flight.get("deadhead") or not isinstance(live, dict) or not isinstance(now, datetime)
            or now.tzinfo is None or now.utcoffset() is None or not isinstance(size, (tuple, list)) or len(size) != 2
            or any(type(value) is not int for value in size) or not 256 <= size[0] <= 4096 or not 128 <= size[1] <= 2160):
        return None
    current = fresh_status(status, now, backend_available=backend_available)
    identity = flight.get("id")
    if (not isinstance(identity, str) or not identity or current.get("status") != "fresh"
            or current.get("source") != "FlightAware AeroAPI" or current.get("phase") not in ACTIVE_PHASES
            or current.get("cancelled") is True or current.get("diverted") is True
            or current.get("flight_id") != identity or live.get("flight_id") != identity
            or (current.get("origin"), current.get("destination")) != (flight.get("origin"), flight.get("destination"))
            or not current.get("provider_flight_id") or current.get("provider_flight_id") != live.get("provider_flight_id")
            or live.get("source") not in {"FlightAware", "FlightAware AeroAPI"}):
        return None
    try:
        if not 0 <= (now - stamp(live.get("observed_at"))).total_seconds() <= 300:
            return None
        times = current.get("times") if isinstance(current.get("times"), dict) else {}
        if any(times.get(key) and stamp(times[key]) <= now for key in ("actual_on", "actual_in")):
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    latitude, longitude = live.get("latitude"), live.get("longitude")
    endpoints = route_endpoints(flight)
    if not _point(latitude, longitude) or endpoints is None:
        return None
    origin, destination, position = endpoints["origin"], endpoints["destination"], (longitude, latitude)
    total = distance_miles(origin, destination)
    if total < 1:
        return None
    remaining = distance_miles(position, destination)
    progress = min(1.0, max(0.0, 1 - remaining / total))
    anchor_fraction = progress * max(0.0, (progress - .15) / .85)
    anchor = _along(origin, destination, anchor_fraction)
    points = (anchor, destination, position)
    lat = (min(point[1] for point in points) + max(point[1] for point in points)) / 2
    offsets = [_wrap(point[0] - destination[0]) for point in points]
    lon = _wrap(destination[0] + (min(offsets) + max(offsets)) / 2)
    width, height = size
    footer = max(14, round(height * .065))
    dx = max(abs(_wrap(point[0] - lon)) for point in points) * MILES_PER_DEGREE * math.cos(math.radians(lat))
    dy = max(abs(point[1] - lat) for point in points) * MILES_PER_DEGREE
    pixels = min((width - 48) / max(.01, 2 * dx), (height - footer - 48) / max(.01, 2 * dy))
    radius = max(MIN_RADIUS_MILES, min(width - 12, height - footer - 12) / (2 * pixels))
    if not _point(lat, lon) or not _number(radius) or not .1 <= radius <= 12000:
        return None
    viewport = {"latitude": lat, "longitude": lon, "radius_miles": radius, "remaining_miles": remaining,
                "origin": origin, "destination": destination, "position": position,
                "mode": "route" if progress <= .15 else "airport" if remaining <= 10 else "approach",
                "detail": "limited", "observed_at": live["observed_at"],
                "position_age_seconds": (now - stamp(live["observed_at"])).total_seconds(),
                "telemetry": {"altitude_feet": live.get("altitude") if live.get("altitude_units") == "feet" and _number(live.get("altitude")) and -2000 <= live["altitude"] <= 100000 else None,
                              "groundspeed_knots": live.get("groundspeed") if _number(live.get("groundspeed")) and 0 <= live["groundspeed"] <= 2000 else None,
                              "heading_degrees": live.get("heading") if _number(live.get("heading")) and 0 <= live["heading"] <= 360 else None}}
    # A complete viewport is required. Partial regional data must never imply
    # that unmapped land is water or that runway detail exists outside coverage.
    if regional_background(lat, lon, radius, tuple(size)) is not None:
        viewport["detail"] = "regional"
    return viewport


@lru_cache(maxsize=1)
def _world():
    try:
        path = ASSETS / "world.json"
        if path.stat().st_size > 6_000_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        result, count = {}, 0
        for name in ("land", "states"):
            rows = data.get(name)
            if not isinstance(rows, list) or len(rows) > 10000:
                return None
            result[name] = []
            for row in rows:
                if not isinstance(row, list) or not 2 <= len(row) <= 100000:
                    return None
                if any(not isinstance(point, list) or len(point) != 2 or not _point(point[1], point[0]) for point in row):
                    # Polar geometry outside the local projection's valid
                    # latitude is not needed for these bounded map views.
                    continue
                count += len(row)
                if count > 500000:
                    return None
                result[name].append(tuple(tuple(point) for point in row))
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


@lru_cache(maxsize=4)
def _world_background(latitude, longitude, radius, size):
    viewport = {"latitude": latitude, "longitude": longitude, "radius_miles": radius}
    project, pixels, footer = projection(viewport, size)
    world = _world()
    image = Image.new("RGB", size, WATER if world is not None else (3, 12, 18))
    draw = ImageDraw.Draw(image)
    if world:
        for ring in world["land"]:
            points = [project(point) for point in ring]
            xs, ys = zip(*points)
            if max(xs) < 0 or min(xs) >= size[0] or max(ys) < 0 or min(ys) >= size[1] - footer:
                continue
            if max(xs) - min(xs) < 300 * MILES_PER_DEGREE * math.cos(math.radians(latitude)) * pixels:
                draw.polygon(points, fill=LAND, outline=SHORE)
        for line in world["states"]:
            points = [project(point) for point in line]
            for a, b in zip(points, points[1:]):
                if abs(a[0] - b[0]) < size[0] * 2:
                    draw.line((a, b), fill=(43, 66, 73), width=max(1, size[1] // 400))
    return image


def render_map(flight, status, live, size, now, *, enabled=True, demo=False, backend_available=True):
    """Return an RGB observed-flight patch, or None for the caller's fallback."""
    viewport = plan_view(flight, status, live, now, size, enabled=enabled, demo=demo, backend_available=backend_available)
    if viewport is None:
        return None
    size = tuple(size)
    image = regional_background(viewport["latitude"], viewport["longitude"], viewport["radius_miles"], size)
    detail = image is not None
    if image is None:
        image = _world_background(viewport["latitude"], viewport["longitude"], viewport["radius_miles"], size).copy()
    draw = ImageDraw.Draw(image)
    project, pixels_per_mile, footer = projection(viewport, size)
    if viewport["radius_miles"] <= 25:
        for code in (flight.get("origin"), flight.get("destination")):
            for a, b, width in _runways().get(code, ()):
                pa, pb = project(a), project(b)
                if (max(pa[0], pb[0]) >= 0 and min(pa[0], pb[0]) < size[0]
                        and max(pa[1], pb[1]) >= 0 and min(pa[1], pb[1]) < size[1] - footer):
                    draw.line((pa, pb), fill=(174, 190, 192),
                              width=max(2, min(24, round(width / 5280 * pixels_per_mile))))
    from .render import AMBER, _text, great_circle
    route = great_circle(viewport["origin"], viewport["destination"])
    points = [project(point) for point in route]
    for a, b in zip(points, points[1:]):
        if abs(a[0] - b[0]) < size[0] * 2:
            draw.line((a, b), fill=(150, 125, 79), width=max(1, size[1] // 300))
    marker = max(3, size[1] // 90)
    for point in (viewport["origin"], viewport["destination"]):
        x, y = project(point)
        if marker <= x < size[0] - marker and marker <= y < size[1] - footer - marker:
            draw.ellipse((x - marker, y - marker, x + marker, y + marker), outline=AMBER, width=max(1, marker // 3))
    x, y = project(viewport["position"])
    heading = live.get("heading")
    if _number(heading) and 0 <= heading <= 360:
        angle = math.radians(heading)
        forward, right = (math.sin(angle), -math.cos(angle)), (math.cos(angle), math.sin(angle))
        shape = [(x + forward[0] * marker * 1.8, y + forward[1] * marker * 1.8),
                 (x - forward[0] * marker + right[0] * marker, y - forward[1] * marker + right[1] * marker),
                 (x - forward[0] * marker - right[0] * marker, y - forward[1] * marker - right[1] * marker)]
        draw.polygon(shape, fill=CYAN, outline=(0, 0, 0))
    else:
        draw.ellipse((x - marker, y - marker, x + marker, y + marker), fill=CYAN, outline=(0, 0, 0))
    scale = max(1, min(3, size[1] // 180, size[0] // 216))
    _text(draw, (8, 8), "N", CYAN, scale)
    _text(draw, (size[0] - 8, 8), f"{viewport['remaining_miles']:.0f} MI TO ARRIVAL", WHITE, scale, align="right")
    draw.rectangle((0, size[1] - footer, size[0] - 1, size[1] - 1), fill=(3, 12, 18))
    attribution = "(C) OPENSTREETMAP CONTRIBUTORS / ODbL" if detail else "NATURAL EARTH / LIMITED DETAIL"
    if not detail and _world() is None:
        attribution = "MAP GEOGRAPHY UNAVAILABLE"
    _text(draw, (5, size[1] - footer + max(1, (footer - 7 * scale) // 2)), attribution, MUTED, scale)
    _text(draw, (8, size[1] - footer - 12 * scale), "REFERENCE LINE / RECEIVED FIX", MUTED, scale,
          max_width=size[0] - 16)
    return image
