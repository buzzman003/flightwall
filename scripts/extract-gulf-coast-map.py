#!/usr/bin/env python3
"""Clip public Gulf Coast vectors to a bounded offline display asset.

The fixed region covers the western Florida Panhandle and neighboring states.
See assets/maps/README.txt for the source downloads. Shapely is used only by
this maintenance script.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re

from shapely.geometry import LineString, box, shape
from shapely.ops import linemerge


BOUNDS = (-90.9, 28, -82, 33.1)
SOURCE_FILES = {
    "nearby-land.geojson": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_land.geojson",
    "nearby-lakes.geojson": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_lakes.geojson",
    "nearby-tiger-2.geojson": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/2/query",
    "nearby-tiger-4.geojson": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/4/query",
}


def parts(geometry, kind):
    if geometry.is_empty:
        return
    if geometry.geom_type == kind:
        yield geometry
    elif geometry.geom_type.startswith("Multi") or geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from parts(part, kind)


def coordinates(points):
    result = []
    for x, y in points:
        point = [round(x, 5), round(y, 5)]
        if not result or result[-1] != point:
            result.append(point)
    return result


def extract(source_dir, runway_file):
    region = box(*BOUNDS)
    features, sources = [], []

    def read(name):
        path = source_dir / name
        if path.stat().st_size > 20_000_000:
            raise ValueError(f"Oversize source: {name}")
        content = path.read_bytes()
        document = json.loads(content)
        rows = document.get("features")
        if (not isinstance(rows, list) or not rows or document.get("error")
                or document.get("exceededTransferLimit")):
            raise ValueError(f"Missing or partial source: {name}")
        sources.append({"url": SOURCE_FILES[name], "sha256": hashlib.sha256(content).hexdigest()})
        return rows

    for name, kind in (("nearby-land.geojson", "land"), ("nearby-lakes.geojson", "water")):
        for row in read(name):
            geometry = shape(row["geometry"])
            if not geometry.intersects(region):
                continue
            clipped = geometry.intersection(region).simplify(.00012, preserve_topology=True)
            for polygon in parts(clipped, "Polygon"):
                rings = [coordinates(ring.coords) for ring in (polygon.exterior, *polygon.interiors)]
                if len(rings[0]) >= 4:
                    features.append({"kind": kind, "geometry": {"type": "Polygon",
                                     "coordinates": [ring for ring in rings if len(ring) >= 4]}})

    roads = {}
    for layer in (2, 4):
        for row in read(f"nearby-tiger-{layer}.geojson"):
            props = row["properties"]
            road_class = ("motorway" if props.get("RTTYP") == "I" else "primary" if layer == 2
                          else "secondary" if props.get("RTTYP") in {"U", "S"} else "tertiary")
            route = props.get("NAME") or ""
            highway_98 = bool(re.fullmatch(r"US (?:Hwy|Highway) 98", route, re.IGNORECASE))
            pieces = parts(shape(row["geometry"]).intersection(region), "LineString")
            roads.setdefault((road_class, highway_98), []).extend(pieces)
    for (road_class, highway_98), segments in roads.items():
        for segment in parts(linemerge(segments), "LineString"):
            points = coordinates(segment.simplify(.0002).coords)
            if len(points) >= 2:
                features.append({"kind": "road", "road_class": road_class, "highway_98": highway_98,
                                 "geometry": {"type": "LineString", "coordinates": points}})

    runway_source = runway_file.read_bytes()
    sources.append({"url": "https://ourairports.com/data/",
                    "bundled_reference_sha256": hashlib.sha256(runway_source).hexdigest()})
    for airport in json.loads(runway_source).values():
        for runway in airport:
            geometry = LineString((runway["a"], runway["b"])).intersection(region)
            for line in parts(geometry, "LineString"):
                points = coordinates(line.coords)
                if len(points) >= 2:
                    features.append({"kind": "runway", "geometry": {"type": "LineString", "coordinates": points}})

    if {row["kind"] for row in features} != {"land", "water", "road", "runway"}:
        raise ValueError("Incomplete Gulf Coast geography")
    return {"schema": 1, "bounds": BOUNDS, "region": "North-central Gulf Coast, United States",
            "source": "Natural Earth 1:10m, U.S. Census TIGERweb, OurAirports",
            "license": "public-domain", "sources": sources,
            "prepared_at": datetime.now(timezone.utc).isoformat(), "features": features}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path(".local-install"))
    parser.add_argument("--output", type=Path, default=Path("assets/maps/gulf-coast.json.gz"))
    args = parser.parse_args()
    result = extract(args.input_dir, Path("assets/maps/airport-runways.json"))
    content = json.dumps(result, separators=(",", ":")).encode("utf-8")
    args.output.write_bytes(gzip.compress(content, mtime=0))
    print(f"{len(result['features'])} features, {len(content)} bytes before compression")


if __name__ == "__main__":
    main()
