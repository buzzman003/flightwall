#!/usr/bin/env python3
"""Reduce public OurAirports CSVs into bounded display/weather references.

No downloads or user configuration are read. Supply the published airports.csv
and runways.csv from https://ourairports.com/data/. All data is public-domain.
Only matching bundled airport identifiers and runway endpoint geometry survive.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import re


def extract(airports_csv, runways_csv, references):
    chosen = {}
    priorities = {"large_airport": 3, "medium_airport": 2, "small_airport": 1}
    for row in csv.DictReader(airports_csv):
        code = row.get("iata_code", "")
        identifier = row.get("icao_code") or row.get("gps_code") or row.get("ident", "")
        if code not in references or row.get("type") not in priorities or not re.fullmatch(r"[A-Z][A-Z0-9]{3}", identifier):
            continue
        try:
            latitude, longitude = float(row["latitude_deg"]), float(row["longitude_deg"])
            reference = references[code]
            if (not all(math.isfinite(v) for v in (latitude, longitude))
                    or abs(latitude - reference["lat"]) > .2 or abs(longitude - reference["lon"]) > .2):
                continue
        except (ValueError, TypeError, KeyError):
            continue
        priority = priorities[row["type"]]
        if code not in chosen or priority > chosen[code][0]:
            chosen[code] = (priority, identifier, row["ident"])
    stations = {code: value[1] for code, value in sorted(chosen.items())}
    by_identifier = {value[2]: code for code, value in chosen.items()}
    runways = {}
    for row in csv.DictReader(runways_csv):
        code = by_identifier.get(row.get("airport_ident"))
        if not code or row.get("closed") != "0":
            continue
        try:
            a = [float(row["le_longitude_deg"]), float(row["le_latitude_deg"])]
            b = [float(row["he_longitude_deg"]), float(row["he_latitude_deg"])]
            width = float(row["width_ft"]) if row.get("width_ft") else None
            if (a == b or any(not math.isfinite(v) for point in (a, b) for v in point)
                    or any(abs(point[0] - references[code]["lon"]) > .25
                           or abs(point[1] - references[code]["lat"]) > .25 for point in (a, b))):
                continue
            if width is not None and (not math.isfinite(width) or not 0 < width <= 1000):
                width = None
        except (ValueError, TypeError, KeyError):
            continue
        runways.setdefault(code, []).append({"a": a, "b": b, "width_feet": width})
    return stations, runways


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airports-csv", type=Path, required=True)
    parser.add_argument("--runways-csv", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path, default=Path(__file__).resolve().parents[1] / "assets/maps")
    args = parser.parse_args()
    references = json.loads((args.assets_dir / "airports.json").read_text(encoding="utf-8"))
    for path in (args.airports_csv, args.runways_csv):
        if path.stat().st_size > 20_000_000:
            raise SystemExit("Public reference CSV exceeds its size bound")
    with args.airports_csv.open(encoding="utf-8-sig", newline="") as airports, args.runways_csv.open(encoding="utf-8-sig", newline="") as runways:
        stations, geometry = extract(airports, runways, references)
    if not stations or not geometry:
        raise SystemExit("No usable public airport references; existing assets were not replaced")
    for name, data in (("airport-weather-stations.json", stations), ("airport-runways.json", geometry)):
        (args.assets_dir / name).write_bytes((json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode())
    print(json.dumps({"stations": len(stations), "runway_airports": len(geometry)}))


if __name__ == "__main__":
    main()
