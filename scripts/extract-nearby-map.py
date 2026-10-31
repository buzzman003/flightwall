#!/usr/bin/env python3
"""Prepare a fixed public regional OSM map, never a user's saved home point.

Requires Shapely from requirements-build.txt, not on the installed board.
Use --download to request the fixed Fort Walton Beach / Destin bounding box;
or --input an existing Overpass JSON response. The runtime has no map network
client. Raw source is written only to the chosen maintenance cache.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import urllib.parse
import urllib.request

from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import linemerge, polygonize, unary_union


# Generic public city-region bounds, not derived from household coordinates.
BOUNDS = (-86.82, 30.28, -86.15, 30.63)  # west, south, east, north
SOURCE = "https://overpass-api.de/api/interpreter"
MAX_BYTES = 64 * 1024 * 1024
ROADS = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
         "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}


def query():
    west, south, east, north = BOUNDS
    bbox = f"({south},{west},{north},{east})"
    return ("[out:json][timeout:120][maxsize:67108864];("
            f'way["natural"="coastline"]{bbox};'
            f'way["natural"="water"]{bbox};relation["natural"="water"]{bbox};'
            f'way["waterway"="riverbank"]{bbox};relation["waterway"="riverbank"]{bbox};'
            f'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified)(_link)?$"]{bbox};'
            f'way["aeroway"="runway"]{bbox};);out geom;')


def coordinates(geometry):
    return [(item["lon"], item["lat"]) for item in geometry or [] if "lon" in item and "lat" in item]


def polygons(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        for part in geometry.geoms:
            yield from polygons(part)


def lines(geometry):
    if geometry.geom_type in {"LineString", "LinearRing"}:
        yield geometry
    elif geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        for part in geometry.geoms:
            yield from lines(part)


def relation_water(element):
    """Join actual outer/inner member rings; never close an open waterway."""
    rings = {}
    for role in ("outer", "inner"):
        parts = [LineString(points) for member in element.get("members", [])
                 if member.get("type") == "way" and member.get("role", "outer") == role
                 and len(points := coordinates(member.get("geometry"))) >= 2]
        if not parts:
            rings[role] = []
            continue
        merged = linemerge(parts)
        candidates = list(lines(merged))
        # An incomplete relation is not safe to fill. Coastlines independently
        # establish sea/bay geometry, so skipping it remains an honest fallback.
        if any(not line.is_ring for line in candidates):
            return None
        rings[role] = [Polygon(line) for line in candidates]
    if not rings.get("outer"):
        return None
    result = unary_union(rings["outer"])
    if rings.get("inner"):
        result = result.difference(unary_union(rings["inner"]))
    return result if result.is_valid else None


def coastal_land(elements, region):
    """Polygonize complete coastlines and region edges, respecting land-left.

    Open coastline ends are accepted only on the region edge; any interior gap
    fails the build rather than creating invented land/water connections.
    """
    directed = []
    for element in elements:
        if element.get("tags", {}).get("natural") == "coastline":
            points = coordinates(element.get("geometry"))
            if len(points) >= 2:
                directed.extend(lines(LineString(points).intersection(region)))
    if not directed:
        raise ValueError("No regional coastline returned")
    stitched = linemerge(directed)
    for line in lines(stitched):
        if not line.is_ring:
            for point in (line.coords[0], line.coords[-1]):
                if region.boundary.distance(Point(point)) > 1e-8:
                    raise ValueError("Incomplete coastline: interior endpoint")
    faces = list(polygonize(unary_union([*directed, region.boundary])))
    segments = [(a, b, LineString((a, b))) for line in directed
                for a, b in zip(line.coords, list(line.coords)[1:]) if a != b]
    land = []
    for face in faces:
        point = face.representative_point()
        a, b, _ = min(segments, key=lambda item: item[2].distance(point))
        cross = (b[0] - a[0]) * (point.y - a[1]) - (b[1] - a[1]) * (point.x - a[0])
        if cross > 0:  # OSM coastline direction has land on the left.
            land.append(face)
    if not land:
        raise ValueError("Coastline orientation did not produce land")
    return unary_union(land)


def extract(data):
    elements = data.get("elements", [])
    if not isinstance(elements, list) or not elements or data.get("remark"):
        raise ValueError("Missing or incomplete Overpass response")
    region = box(*BOUNDS)
    features = []

    def add(kind, geometry, **properties):
        if geometry is None or geometry.is_empty:
            return
        geometry = geometry.intersection(region).simplify(.000006, preserve_topology=True)
        parts = polygons(geometry) if kind in {"land", "water"} else lines(geometry)
        for part in parts:
            features.append({"kind": kind, "geometry": mapping(part), **properties})

    add("land", coastal_land(elements, region))
    for element in elements:
        tags = element.get("tags", {})
        is_water = tags.get("natural") == "water" or tags.get("waterway") == "riverbank"
        if is_water:
            if element.get("type") == "relation":
                add("water", relation_water(element))
            else:
                points = coordinates(element.get("geometry"))
                if len(points) >= 4 and points[0] == points[-1]:
                    polygon = Polygon(points)
                    if polygon.is_valid:
                        add("water", polygon)
        points = coordinates(element.get("geometry"))
        if element.get("type") != "way" or len(points) < 2:
            continue
        if tags.get("aeroway") == "runway":
            add("runway", LineString(points))
        elif tags.get("highway") in ROADS:
            refs = tags.get("ref", "").replace(";", " ").split()
            is_98 = "98" in refs or "US 98" in tags.get("ref", "")
            add("road", LineString(points), road_class=tags["highway"], highway_98=is_98)
    counts = {kind: sum(row["kind"] == kind for row in features) for kind in ("land", "water", "road", "runway")}
    if not counts["land"] or not counts["road"] or not counts["runway"]:
        raise ValueError(f"Incomplete map feature set: {counts}")
    return {"schema": 1, "bounds": BOUNDS, "region": "Fort Walton Beach / Destin, Florida",
            "source": "OpenStreetMap via Overpass API", "source_url": SOURCE,
            "source_timestamp": data.get("osm3s", {}).get("timestamp_osm_base"),
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "license": "ODbL-1.0", "attribution": "OpenStreetMap contributors",
            "license_url": "https://www.openstreetmap.org/copyright", "features": features}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(".local-install/nearby-map-overpass.json"))
    parser.add_argument("--output", type=Path, default=Path("assets/maps/fort-walton-destin.json"))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--system-curl", action="store_true", help="Use normally verified system curl (e.g. Windows Schannel)")
    args = parser.parse_args()
    if args.download:
        user_agent = "FlightWallMapMaintenance/0.4 (+https://github.com/buzzman003/flightwall)"
        if args.system_curl:
            args.input.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["curl", "--fail", "--silent", "--show-error", "--proto", "=https",
                            "--max-time", "150", "--max-filesize", str(MAX_BYTES), "--user-agent", user_agent,
                            "--data-urlencode", "data=" + query(), "--output", str(args.input), SOURCE], check=True)
            raw = args.input.read_bytes()
        else:
            request = urllib.request.Request(SOURCE, data=urllib.parse.urlencode({"data": query()}).encode(),
                                             headers={"User-Agent": user_agent})
            with urllib.request.urlopen(request, timeout=150) as response:
                raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Map source exceeds bounded maintenance download")
        args.input.parent.mkdir(parents=True, exist_ok=True)
        args.input.write_bytes(raw)
    if args.input.stat().st_size > MAX_BYTES:
        raise ValueError("Map source exceeds bounded maintenance input")
    result = extract(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "features": len(result["features"]),
                      "source_timestamp": result["source_timestamp"], "bytes": args.output.stat().st_size}))


if __name__ == "__main__":
    main()
