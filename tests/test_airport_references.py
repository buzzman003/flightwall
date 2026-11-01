"""Weather/runway reference identities come from public metadata, not guesses."""
import importlib.util
import io
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("airport_references", ROOT / "scripts/extract-airport-references.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_bundled_station_and_runway_references_are_bounded_and_known():
    stations = json.loads((ROOT / "assets/maps/airport-weather-stations.json").read_text())
    runways = json.loads((ROOT / "assets/maps/airport-runways.json").read_text())
    airports = json.loads((ROOT / "assets/maps/airports.json").read_text())
    assert 1000 < len(stations) < 20000 and stations["VPS"] == "KVPS" and stations["ATL"] == "KATL"
    assert set(stations) <= set(airports) and set(runways) <= set(stations)
    assert all(re.fullmatch(r"[A-Z][A-Z0-9]{3}", station) for station in stations.values())
    assert len(runways["ATL"]) == 5 and len(runways["VPS"]) == 2
    for code, rows in runways.items():
        assert len(rows) <= 30
        for row in rows:
            assert row["a"] != row["b"]
            assert all(abs(point[0] - airports[code]["lon"]) <= .25
                       and abs(point[1] - airports[code]["lat"]) <= .25 for point in (row["a"], row["b"]))


def test_reference_extraction_skips_unknown_moved_closed_and_malformed_data():
    airports = io.StringIO("ident,type,latitude_deg,longitude_deg,gps_code,iata_code\nX001,small_airport,1,2,X001,ABC\nY001,large_airport,1,2,Y001,ABC\nZ001,large_airport,40,50,Z001,DEF\nBAD,large_airport,1,2,,ZZZ\n")
    runways = io.StringIO("airport_ident,closed,le_longitude_deg,le_latitude_deg,he_longitude_deg,he_latitude_deg,width_ft\nY001,0,2,1,2.01,1.01,150\nY001,1,2,1,2.02,1.02,150\nY001,0,100,50,101,51,150\n")
    stations, geometry = module.extract(airports, runways, {"ABC": {"lat": 1, "lon": 2}, "DEF": {"lat": 1, "lon": 2}})
    assert stations == {"ABC": "Y001"}
    assert geometry == {"ABC": [{"a": [2, 1], "b": [2.01, 1.01], "width_feet": 150}]}
