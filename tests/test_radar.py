"""Actual observation frames, fixed public scopes, and bounded PNG playback."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

from PIL import Image, ImageDraw, ImageOps
import pytest

from flightwall import radar, display, flight_context_views
from flightwall.flight_environment import FlightEnvironmentFeed
from flightwall.render import WHITE

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 17, tzinfo=UTC)


def gif(colors=("red", "blue", "green"), durations=(200, 200, 1500), size=(60, 40)):
    frames = [Image.new("RGB", size, value) for value in colors]
    out = BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=list(durations), loop=0)
    return out.getvalue()


def png(size, color=(0, 0, 0, 0)):
    out = BytesIO()
    Image.new("RGBA", size, color).save(out, format="PNG")
    return out.getvalue()


def capabilities(times):
    return ("""<WMS_Capabilities xmlns="http://www.opengis.net/wms"><Capability><Layer><Layer>
      <Name>conus_bref_qcd</Name><Dimension name="time" units="ISO8601">""" + ",".join(times)
      + "</Dimension></Layer></Layer></Capability></WMS_Capabilities>").encode()


def stamps(now=NOW):
    return [(now - timedelta(minutes=value)).isoformat() for value in (4, 2, 0)]


def wms_response(scope):
    def request(url):
        if url == radar.CAPABILITIES_URL:
            return capabilities(stamps()), None
        if url == radar.LEGEND_URL:
            return png((500, 30)), None
        assert url in [radar.map_url(scope, at) for at in stamps()]
        return png(radar.SIZES[scope], (255, 0, 0, 80)), None
    return request


def config(**changes):
    return {"demo": False, "national_radar_enabled": True, "national_radar_flight_only": False,
            "national_radar_scope": "national", "radar_card_visible": True,
            "airport_alerts_enabled": False, "timezone": "America/Chicago", **changes}


def test_gif_frames_and_source_delays_survive_png_cache_and_latest_poster():
    frames, durations = radar.decode_loop(gif())
    assert durations == [200, 200, 1500]
    poster, data, meta = radar.encode_loop(frames, durations)
    decoded, decoded_delays = radar.decode_cached(data)
    assert meta["frame_count"] == 3 and meta["observed_at"] == [None] * 3
    assert decoded_delays == durations
    assert [frame.getpixel((0, 0)) for frame in decoded] == [(255, 0, 0), (0, 0, 255), (0, 128, 0)]
    with Image.open(BytesIO(poster)) as image:
        assert image.getpixel((0, 0)) == (0, 128, 0)
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()
    assert [radar.frame_index(meta, tick) for tick in (0, .1999, .2, .3999, .4, 1.8999, 1.9)] == [0, 0, 1, 1, 2, 2, 0]


@pytest.mark.parametrize("raw", [b"invalid", b"x" * (radar.MAX_INPUT_BYTES + 1),
    gif(tuple((value * 20, 0, 0) for value in range(11)), [200] * 11),
    gif(("red", "blue"), [0, 0]), gif(("red",), [200], size=(4000, 3000))],
    ids=["not-image", "oversized", "too-many-frames", "no-delay", "too-many-pixels"])
def test_malformed_or_excessive_source_loop_is_rejected(raw):
    with pytest.raises((ValueError, OSError)):
        radar.decode_loop(raw)


def test_total_decoded_pixels_are_bounded_across_all_frames():
    with pytest.raises(ValueError, match="pixels"):
        radar.decode_loop(gif(tuple((n * 30, 0, 0) for n in range(7)), [200] * 7, size=(2000, 2000)))


@pytest.mark.parametrize("changes", [
    {"frame_count": 11}, {"durations_ms": [0, 200, 1500]}, {"total_duration_ms": 50},
    {"observed_at": [NOW.isoformat(), None, NOW.isoformat()]},
    {"observed_at": [NOW.isoformat()] * 3}, {"sha256": "../file.png"},
])
def test_animation_metadata_cannot_invent_frames_timing_or_observation_order(changes):
    _, _, meta = radar.encode_loop(*radar.decode_loop(gif()))
    with pytest.raises((ValueError, TypeError)):
        radar.validate_animation(meta | changes)


def test_capabilities_select_only_exact_advertised_recent_unique_observations():
    values = [*stamps(), NOW.isoformat(), (NOW + timedelta(minutes=1)).isoformat(),
              (NOW - timedelta(hours=3)).isoformat()]
    assert radar.observation_times(capabilities(values), NOW) == stamps()
    fractional = "2026-11-01T16:59:59.234Z"
    exact = radar.observation_times(capabilities([fractional]), NOW)[0]
    assert radar.stamp(exact).microsecond == 234000
    assert parse_qs(urlsplit(radar.map_url("local", exact)).query)["time"] == ["2026-11-01T16:59:59.234000Z"]
    for content in (b'<!DOCTYPE a [<!ENTITY b "bad">]><a/>', b"<wrong/>", capabilities([(NOW + timedelta(minutes=1)).isoformat()]),
                    capabilities([(NOW - timedelta(minutes=31)).isoformat()])):
        with pytest.raises(ValueError):
            radar.observation_times(content, NOW)


def test_map_requests_are_exact_public_boxes_not_household_coordinates():
    for scope in sorted(radar.SCOPES):
        url = radar.map_url(scope, NOW.isoformat())
        assert radar.request_limit(url) == 4_000_000
        query = parse_qs(urlsplit(url).query)
        assert query["crs"] == ["CRS:84"] and query["layers"] == ["conus_bref_qcd"]
        assert query["bbox"] == [radar._bbox(scope)]
        assert query["time"] == ["2026-11-01T17:00:00Z"]
        for bad in (url.replace("https:", "http:"), url + "&private=true", url.replace("width=", "width=999"),
                    url.replace(quote(radar._bbox(scope), safe=""), "0%2C0%2C1%2C1"), url.replace("opengeo.ncep.noaa.gov", "untrusted.invalid")):
            with pytest.raises(ValueError):
                radar.request_limit(bad)
    assert radar.request_limit(radar.NATIONAL_URL) == radar.MAX_INPUT_BYTES
    assert radar.request_limit(radar.SOUTHEAST_URL) == radar.MAX_INPUT_BYTES


def test_wms_colors_extent_and_observation_clock_are_preserved_without_fake_resolution():
    size = radar.SIZES["local"]
    raw = png(size, (240, 10, 20, 255))
    with patch.object(radar, "background", return_value=Image.new("RGB", size, "navy")):
        frame = radar.compose_wms(raw, "local", NOW.isoformat())
    assert frame.getpixel((size[0] // 2, size[1] // 2)) == (240, 10, 20)
    assert frame.width == size[0] and frame.height == size[1] + 74
    # The geography/radar rectangle remains complete above a separate clock/key band.
    assert frame.getpixel((size[0] - 1, size[1] - 1)) == (240, 10, 20)
    with pytest.raises(ValueError):
        radar.compose_wms(png((20, 20)), "local", NOW.isoformat())


def test_environment_always_on_is_a_separate_explicit_choice_and_demo_stays_off(tmp_path):
    settings = config(national_radar_flight_only=True)
    feed = FlightEnvironmentFeed(tmp_path, lambda: settings)
    empty = {"demo": False, "flights": []}
    with patch("flightwall.flight_environment.request_bytes") as request:
        assert feed.poll(empty, NOW)["radar"]["status"] == "outside_window"
        request.assert_not_called()
    settings["national_radar_flight_only"] = False
    with patch("flightwall.flight_environment.request_bytes", side_effect=wms_response("national")) as request:
        state = feed.poll(empty, NOW)
    assert state["radar"]["status"] == "ready" and request.call_count == 5
    assert state["radar"]["animation"]["frame_count"] == 3
    settings["demo"] = True
    with patch("flightwall.flight_environment.request_bytes") as request:
        assert feed.poll(empty, NOW)["radar"]["status"] == "demo"
        request.assert_not_called()


@pytest.mark.parametrize("scope", sorted(radar.SCOPES))
def test_home_marker_uses_exact_public_grid_before_contain_without_touching_clock_band(scope):
    width, height = radar.SIZES[scope]
    west, south, east, north = radar.map_geometry(scope)["bounds"]
    longitude, latitude = west + (east - west) * .3, north - (north - south) * .4
    image = Image.new("RGB", (width, height + radar.FOOTER_HEIGHT), (20, 30, 40))
    original = image.tobytes()
    marked = radar.mark_home(image, scope, latitude, longitude)
    x, y = round(.3 * (width - 1)), round(.4 * (height - 1))
    assert marked.getpixel((x, y)) == WHITE
    assert marked.crop((0, height, width, image.height)).tobytes() == image.crop((0, height, width, image.height)).tobytes()
    assert image.tobytes() == original and marked.info == image.info
    fitted = ImageOps.contain(marked, (700, 300))
    mapped_x, mapped_y = round(x * fitted.width / width), round(y * fitted.height / image.height)
    assert max(sum(fitted.getpixel((px, py))) for px in range(mapped_x - 1, mapped_x + 2)
               for py in range(mapped_y - 1, mapped_y + 2)) > 500


@pytest.mark.parametrize("point", [(None, -86.4), (float("nan"), -86.4), (True, -86.4),
                                  (40, -86.4), (30.4, -100)])
def test_invalid_or_outside_home_pin_is_not_drawn(point):
    width, height = radar.SIZES["hyperlocal"]
    image = Image.new("RGB", (width, height + radar.FOOTER_HEIGHT), "navy")
    assert radar.mark_home(image, "hyperlocal", *point).tobytes() == image.tobytes()
    assert radar.mark_home(image.resize((400, 300)), "hyperlocal", 30.4, -86.4).size == (400, 300)


@pytest.mark.parametrize("scope", sorted(radar.SCOPES))
def test_marker_does_not_enter_footer_at_the_edge_of_the_geographic_rectangle(scope):
    width, height = radar.SIZES[scope]
    west, south, east, north = radar.map_geometry(scope)["bounds"]
    image = Image.new("RGB", (width, height + radar.FOOTER_HEIGHT), "navy")
    for latitude, longitude in ((north, west), (south, east)):
        marked = radar.mark_home(image, scope, latitude, longitude)
        assert marked.crop((0, height, width, image.height)).tobytes() == image.crop((0, height, width, image.height)).tobytes()


def test_old_standard_loop_cache_is_visible_but_never_claims_wms_geometry(tmp_path):
    settings = config()
    poster, animation, metadata = radar.encode_loop(*radar.decode_loop(gif()))
    (tmp_path / "national-radar.png").write_bytes(poster)
    (tmp_path / "national-radar-loop.png").write_bytes(animation)
    (tmp_path / "flight-environment.json").write_text(json.dumps({"schema": 1, "radar": {
        "scope": "national", "clock_basis": "embedded_utc", "fetched_at": NOW.isoformat(),
        "issued_at": NOW.isoformat(), "image_sha256": hashlib.sha256(poster).hexdigest(), "animation": metadata}}))
    feed = FlightEnvironmentFeed(tmp_path, lambda: settings)
    feed._snapshot = {"demo": False, "flights": []}
    old = feed.status(NOW)["radar"]
    assert old["status"] == "ready" and old["image_available"] and not old["home_marker_eligible"]
    with patch("flightwall.flight_environment.request_bytes", side_effect=wms_response("national")):
        current = feed.poll({"demo": False, "flights": []}, NOW)["radar"]
    assert current["home_marker_eligible"] and current["clock_basis"] == "advertised_observation"
    cache = json.loads(feed.path.read_text())
    cache["radar"]["geometry"]["bounds"][0] += 1
    feed.path.write_text(json.dumps(cache))
    reloaded = FlightEnvironmentFeed(tmp_path, lambda: settings)
    reloaded._snapshot = {"demo": False, "flights": []}
    assert not reloaded.status(NOW)["radar"]["home_marker_eligible"]


def test_scope_switch_invalidates_old_picture_immediately_and_fetches_only_new_scope(tmp_path):
    settings = config()
    feed = FlightEnvironmentFeed(tmp_path, lambda: settings)
    with patch("flightwall.flight_environment.request_bytes", side_effect=wms_response("national")):
        feed.poll({"demo": False, "flights": []}, NOW)
    settings["national_radar_scope"] = "southeast"
    state = feed.status(NOW)["radar"]
    assert not state["image_available"] and state["animation"] is None
    assert state["scope"] == "southeast" and state["issued_at"] is None
    with patch("flightwall.flight_environment.request_bytes", side_effect=wms_response("southeast")) as request:
        state = feed.poll({"demo": False, "flights": []}, NOW)["radar"]
    assert state["status"] == "ready" and state["home_marker_eligible"]
    assert [call.args[0] for call in request.call_args_list] == [
        radar.CAPABILITIES_URL, *[radar.map_url("southeast", at) for at in stamps()]]


@pytest.mark.parametrize("scope", sorted(radar.SCOPES))
def test_scoped_observations_batch_once_cache_frames_and_skip_duplicate_image_queries(tmp_path, scope):
    settings = config(national_radar_scope=scope)
    feed = FlightEnvironmentFeed(tmp_path, lambda: settings)
    seen = []
    def request(url):
        seen.append(url)
        if url == radar.CAPABILITIES_URL:
            return capabilities(stamps()), None
        if url == radar.LEGEND_URL:
            return png((500, 30)), None
        assert radar.request_limit(url) == 4_000_000
        return png(radar.SIZES[scope], (255, 0, 0, 80)), None
    with patch("flightwall.flight_environment.request_bytes", side_effect=request), \
            patch.object(radar, "background", return_value=Image.new("RGB", radar.SIZES[scope], "navy")), \
            patch("flightwall.flight_environment.time.monotonic", return_value=100):
        state = feed.poll({"demo": False, "flights": []}, NOW)["radar"]
    assert state["status"] == "ready" and state["clock_basis"] == "advertised_observation"
    assert state["home_marker_eligible"]
    assert state["animation"]["observed_at"] == stamps() and state["issued_at"] == stamps()[-1]
    assert len(seen) == 5 and feed.animation_path.exists()
    assert "bbox" not in json.dumps(state) and "PUBLIC" not in feed.path.read_text()
    seen.clear()
    with patch("flightwall.flight_environment.request_bytes", side_effect=request), \
            patch("flightwall.flight_environment.time.monotonic", return_value=700):
        state = feed.poll({"demo": False, "flights": []}, NOW + timedelta(minutes=10))["radar"]
    assert seen == [radar.CAPABILITIES_URL]
    assert state["issued_at"] == stamps()[-1] and state["fetched_at"] != state["issued_at"]


def test_reader_hash_bounds_generations_and_source_delays_control_actual_frames(tmp_path):
    _, raw, meta = radar.encode_loop(*radar.decode_loop(gif()))
    path = tmp_path / "national-radar-loop.png"
    path.write_bytes(raw)
    connection = display.BackendConnection(tmp_path, {"control_port": 8765})
    for tick, color, index in ((0, (255, 0, 0), 0), (.2, (0, 0, 255), 1), (.4, (0, 128, 0), 2)):
        frame, actual, count = connection.local_radar_frame(meta["sha256"], tick)
        assert (actual, count) == (index, 3) and frame.getpixel((0, 0)) == color and frame.info == {}
    assert connection.local_radar_frame("b" * 64, 0) is None
    path.write_bytes(b"invalid-generation")
    assert connection.local_radar_frame(meta["sha256"], 0) is None
    for digest in (None, "../file.png", "A" * 64):
        assert connection.local_radar_frame(digest, 0) is None


def test_radar_animation_uses_monotonic_time_when_card_is_paused_and_preserves_static_fallback(tmp_path):
    poster, raw, meta = radar.encode_loop(*radar.decode_loop(gif(size=(600, 392))))
    (tmp_path / "national-radar-loop.png").write_bytes(raw)
    (tmp_path / "national-radar.png").write_bytes(poster)
    connection = display.BackendConnection(tmp_path, {"control_port": 8765})
    state = {"view": "national_radar", "demo": False, "paused": True, "national_radar": {
        "status": "ready", "fetched_at": NOW.isoformat(), "issued_at": NOW.isoformat(), "image_available": True,
        "image_sha256": hashlib.sha256(poster).hexdigest(), "scope": "national", "animation": meta}}
    colors = []
    for tick in (0, .2):
        image = Image.new("RGB", (512, 256))
        with patch("flightwall.display.time.monotonic", return_value=tick):
            display.attach_flight_context(image, state, config(), connection, (1920, 1080), NOW)
        layer = image.info["geographic_patch"][1]
        colors.append(layer.getpixel((layer.width // 2, layer.height // 2)))
    assert colors == [(255, 0, 0), (0, 0, 255)]
    (tmp_path / "national-radar-loop.png").write_bytes(b"broken")
    image = Image.new("RGB", (512, 256))
    display.attach_flight_context(image, state, config(), connection, (1920, 1080), NOW)
    layer = image.info["geographic_patch"][1]
    assert layer.getpixel((layer.width // 2, layer.height // 2)) == (0, 128, 0)


def test_wrong_scope_visibility_backend_and_future_observation_metadata_fail_closed():
    _, _, meta = radar.encode_loop(*radar.decode_loop(gif()), observed_at=stamps())
    state = {"demo": False, "national_radar": {"status": "ready", "image_available": True,
        "image_sha256": "a" * 64, "scope": "national", "issued_at": NOW.isoformat(),
        "fetched_at": NOW.isoformat(), "animation": meta}}
    assert flight_context_views.radar_ready(state, config(), NOW)
    assert not flight_context_views.radar_ready(state, config(national_radar_scope="southeast"), NOW)
    assert not flight_context_views.radar_ready(state, config(radar_card_visible=False), NOW)
    assert not flight_context_views.radar_ready(state | {"backend_available": False}, config(), NOW)
    state["national_radar"]["animation"]["observed_at"][-1] = (NOW + timedelta(seconds=1)).isoformat()
    assert not flight_context_views.radar_ready(state, config(), NOW)


def test_opt_out_during_capabilities_fetch_stops_followup_requests_and_writes(tmp_path):
    settings = config(national_radar_scope="local")
    feed = FlightEnvironmentFeed(tmp_path, lambda: settings)
    def request(url):
        assert url == radar.CAPABILITIES_URL
        settings["national_radar_enabled"] = False
        return capabilities(stamps()), None
    with patch("flightwall.flight_environment.request_bytes", side_effect=request) as request:
        state = feed.poll({"demo": False, "flights": []}, NOW)["radar"]
    assert request.call_count == 1 and state["status"] == "off"
    assert not feed.image_path.exists() and not feed.animation_path.exists()


def test_future_or_mismatched_observation_clock_cache_cannot_claim_ready(tmp_path):
    settings = config()
    feed = FlightEnvironmentFeed(tmp_path, lambda: settings)
    with patch("flightwall.flight_environment.request_bytes", side_effect=wms_response("national")):
        feed.poll({"demo": False, "flights": []}, NOW)
    feed._cache["radar"]["clock_basis"] = "advertised_observation"
    feed._cache["radar"]["animation"]["observed_at"] = stamps()
    assert feed.status(NOW)["radar"]["status"] == "ready"
    feed._cache["radar"]["animation"]["observed_at"][-1] = (NOW + timedelta(seconds=1)).isoformat()
    assert feed.status(NOW)["radar"]["status"] == "stale"


def test_exact_wms_frame_clock_is_labeled_in_household_zone_without_changing_source_pixels(tmp_path):
    poster, raw, meta = radar.encode_loop(*radar.decode_loop(gif()), observed_at=stamps())
    (tmp_path / "national-radar-loop.png").write_bytes(raw)
    (tmp_path / "national-radar.png").write_bytes(poster)
    connection = display.BackendConnection(tmp_path, {"control_port": 8765})
    state = {"view": "national_radar", "demo": False, "national_radar": {
        "status": "ready", "fetched_at": NOW.isoformat(), "issued_at": NOW.isoformat(), "image_available": True,
        "image_sha256": hashlib.sha256(poster).hexdigest(), "scope": "national", "animation": meta}}
    labels = []
    with patch("flightwall.display.time.monotonic", return_value=0), \
            patch("flightwall.render._text", side_effect=lambda *args, **kwargs: labels.append(args[2])):
        display.attach_flight_context(Image.new("RGB", (512, 256)), state, config(), connection, (1920, 1080), NOW)
    assert "OBS 1/3" in labels and "UTC IN IMAGE" in labels
    assert "10:56 AM CST" in labels  # 16:56 UTC is the actual selected frame's observation time.
