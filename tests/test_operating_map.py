"""Geographic route fitting uses fictional received positions, never a home pin."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from PIL import Image
import pytest

from flightwall import operating_map as maps
from flightwall import nearby_map, render
from flightwall.flight_alerts import normalize_operating_status


NOW = datetime(2026, 11, 1, 16, tzinfo=timezone.utc)
FLIGHT = {"id": "fictional-operating-map", "number": "DL123", "origin": "ATL", "destination": "VPS",
          "start": "2026-11-01T15:30:00Z", "end": "2026-11-01T17:00:00Z", "deadhead": False}
PROVIDER_ID = "fictional-provider-map"


def status(**changes):
    provider = {"fa_flight_id": PROVIDER_ID, "actual_off": "2026-11-01T15:45:00Z",
                "scheduled_out": FLIGHT["start"], "scheduled_in": FLIGHT["end"], **changes}
    return normalize_operating_status(provider, FLIGHT, NOW)


def live(fraction=0, **changes):
    endpoints = maps.route_endpoints(FLIGHT)
    lon, lat = maps._along(endpoints["origin"], endpoints["destination"], fraction)
    return {"flight_id": FLIGHT["id"], "provider_flight_id": PROVIDER_ID,
            "latitude": lat, "longitude": lon, "observed_at": NOW.isoformat(), "source": "FlightAware",
            "altitude": 24000, "altitude_units": "feet", "groundspeed": 400, "heading": 210, **changes}


def test_initial_view_includes_both_airports_and_real_fix_without_private_configuration():
    viewport = maps.plan_view(FLIGHT, status(), live(), NOW)
    assert viewport["mode"] == "route" and viewport["detail"] == "limited"
    assert viewport["radius_miles"] > 50 and viewport["remaining_miles"] > 200
    project, _, footer = maps.projection(viewport, (1110, 555))
    for point in (viewport["origin"], viewport["destination"], viewport["position"]):
        x, y = project(point)
        assert 12 <= x <= 1098 and 12 <= y <= 555 - footer - 12
    assert "home_latitude" not in viewport and "home_longitude" not in viewport
    assert viewport["telemetry"] == {"altitude_feet": 24000, "groundspeed_knots": 400, "heading_degrees": 210}


def test_view_zooms_progressively_with_received_distance_not_wall_clock_or_estimate():
    views = [maps.plan_view(FLIGHT, status(), live(fraction), NOW) for fraction in (0, .2, .5, .8, .95, .99)]
    assert all(a["radius_miles"] > b["radius_miles"] for a, b in zip(views, views[1:]))
    assert all(a["remaining_miles"] > b["remaining_miles"] for a, b in zip(views, views[1:]))
    assert views[-1]["mode"] == "airport" and views[-1]["detail"] == "regional"
    position = live(.9)
    first = maps.plan_view(FLIGHT, status(), position, NOW)
    later = maps.plan_view(FLIGHT, status(), position, NOW + timedelta(seconds=300))
    for key in ("latitude", "longitude", "radius_miles", "position", "remaining_miles"):
        assert first[key] == later[key]  # no extrapolation between observations


@pytest.mark.parametrize("phase", ("landed", "at_gate", "tracking_ended", "blocked", "unknown"))
def test_landing_gate_arrival_or_unknown_phase_requests_parent_fallback(phase):
    assert maps.plan_view(FLIGHT, status() | {"phase": phase}, live(.99), NOW) is None


def test_actual_landing_clock_and_diversion_stop_even_an_inconsistent_cached_active_phase():
    assert maps.plan_view(FLIGHT, status(actual_on="2026-11-01T15:59:00Z") | {"phase": "airborne"}, live(.99), NOW) is None
    assert maps.plan_view(FLIGHT, status(diverted=True), live(.8), NOW) is None
    assert maps.plan_view(FLIGHT, status(cancelled=True), live(.8), NOW) is None


def test_fresh_actual_surface_fix_is_allowed_without_inventing_ground_or_taxi_movement():
    value = status(actual_off=None, actual_out=None)
    point = live(0, altitude=0, groundspeed=0, heading=None)
    viewport = maps.plan_view(FLIGHT, value, point, NOW)
    assert viewport is not None
    assert viewport["position"] == viewport["origin"]
    assert viewport["telemetry"]["groundspeed_knots"] == 0
    assert "on_ground" not in viewport
    assert maps.render_map(FLIGHT, value, point, (1110, 555), NOW) is not None


@pytest.mark.parametrize("change", ({"flight_id": "another-leg"}, {"provider_flight_id": "another-instance"},
                                    {"source": "ADSB.lol"}, {"latitude": True}, {"longitude": float("nan")},
                                    {"observed_at": None}, {"observed_at": (NOW + timedelta(seconds=1)).isoformat()}))
def test_mismatched_future_invalid_or_missing_positions_never_draw_live(change):
    assert maps.plan_view(FLIGHT, status(), live(.5, **change), NOW) is None
    assert maps.render_map(FLIGHT, status(), live(.5, **change), (1110, 555), NOW) is None


def test_expiry_exact_boundary_backend_loss_disabled_demo_and_unknown_provider_are_fail_closed():
    value, point = status(), live(.5)
    assert maps.plan_view(FLIGHT, value, point, NOW + timedelta(seconds=300)) is not None
    assert maps.plan_view(FLIGHT, value, point, NOW + timedelta(seconds=300, microseconds=1)) is None
    for keyword in ({"backend_available": False}, {"enabled": False}, {"demo": True}):
        assert maps.render_map(FLIGHT, value, point, (1110, 555), NOW, **keyword) is None
    for mode in ("pending", "stale", "unavailable", "ambiguous", "not_found"):
        assert maps.plan_view(FLIGHT, value | {"status": mode}, point, NOW) is None
    assert maps.plan_view(FLIGHT, value | {"origin": "LAX"}, point, NOW) is None
    assert maps.plan_view(FLIGHT | {"deadhead": True}, value, point, NOW) is None


def test_high_resolution_regional_detail_and_honest_broad_fallback_have_no_airport_code_labels():
    labels = []
    original = render._text
    def text(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=text):
        broad = maps.render_map(FLIGHT, status(), live(), (1110, 555), NOW)
        assert "NATURAL EARTH / LIMITED DETAIL" in labels
        labels.clear()
        detail = maps.render_map(FLIGHT, status(), live(.99), (1110, 555), NOW)
    assert "(C) OPENSTREETMAP CONTRIBUTORS / ODbL" in labels
    assert "REFERENCE LINE / RECEIVED FIX" in labels
    assert "ATL" not in labels and "VPS" not in labels and "HOME +" not in labels
    assert broad.size == detail.size == (1110, 555)
    assert broad.mode == detail.mode == "RGB"


def test_all_inputs_remain_unchanged_and_bounded_bad_sizes_or_missing_airports_return_none():
    value, point, flight = status(), live(.9), deepcopy(FLIGHT)
    originals = deepcopy((value, point, flight))
    assert maps.render_map(flight, value, point, (1110, 555), NOW) is not None
    assert (value, point, flight) == originals
    for size in (None, (1, 1), (True, 300), (5000, 2500), (1110, float("nan"))):
        assert maps.plan_view(flight, value, point, NOW, size) is None
    assert maps.route_endpoints(flight | {"origin": "ZZZ"}) is None
    assert maps.plan_view(flight | {"destination": "ZZZ"}, value | {"destination": "ZZZ"}, point, NOW) is None


def test_regional_background_has_no_traffic_circle_home_cross_or_home_input_requirement():
    nearby_map._base_map.cache_clear()
    image = nearby_map.regional_background(30.48, -86.51, 5, (1110, 555))
    assert isinstance(image, Image.Image)
    assert image.getpixel((555, 259)) != nearby_map.WHITE
    assert nearby_map.regional_background(33.63, -84.43, 5, (1110, 555)) is None


def test_dateline_route_fits_short_direction_instead_of_wrapping_around_world():
    flight = FLIGHT | {"origin": "AAA", "destination": "BBB"}
    value = status() | {"origin": "AAA", "destination": "BBB"}
    point = live(latitude=10, longitude=179)
    with patch.object(maps, "_airports", return_value={"AAA": (179, 10), "BBB": (-179, 12)}):
        viewport = maps.plan_view(flight, value, point, NOW)
    assert viewport is not None and viewport["radius_miles"] < 150
    project, _, footer = maps.projection(viewport, (1110, 555))
    for endpoint in (viewport["origin"], viewport["destination"]):
        x, y = project(endpoint)
        assert 12 < x < 1098 and 12 < y < 555 - footer - 12
