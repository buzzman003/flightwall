"""Outer aircraft rows obey the same maintenance boundary as the inner ring."""
from datetime import datetime, timezone
from unittest.mock import patch

from flightwall.backend import Runtime


def test_outer_aircraft_reaches_display_without_exposing_home_geometry_to_maintenance(tmp_path):
    runtime = Runtime(tmp_path / "state", tmp_path / "install")
    row = {"hex": "abcdef", "registration": "N12345", "distance_miles": 8.0,
           "latitude": 30.0, "longitude": -86.0, "east_miles": 7.0, "north_miles": 3.0}
    with patch.object(runtime.feeds, "nearby_for_config", return_value=[]), \
         patch.object(runtime.feeds, "nearby_outer_for_config", return_value=[row]):
        now = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
        display = runtime.display_status(now)
        assert display["nearby_outer"] == [row]
        maintenance = runtime.status(now)
    assert maintenance["nearby_outer"] == [
        {key: value for key, value in row.items() if key not in {"latitude", "longitude"}}]
    assert display["nearby_outer"][0]["latitude"] == 30.0
