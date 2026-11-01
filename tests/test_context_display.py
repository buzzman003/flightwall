"""Native flight-context layers and long reveals fail closed between polls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin
import pytest

from flightwall import display, operating_map
from flightwall.config import DEFAULTS
from flightwall.flight_alerts import normalize_operating_status
from flightwall.render import display_content_bounds

NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)
RED, GREEN, PATCH = (177, 13, 23), (27, 101, 211), (100, 200, 40)
FLIGHT = {"id": "context-own", "number": "DL123", "origin": "ATL", "destination": "MEM", "deadhead": False,
          "start": (NOW - timedelta(minutes=30)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}


def config(**changes):
    return DEFAULTS | {"demo": False, "flightaware_enabled": True, "national_radar_enabled": True,
                       "timezone": "America/Chicago", "glow": 0, "brightness": .5, **changes}


def operating_state(**changes):
    provider = {"fa_flight_id": "context-provider", "scheduled_out": FLIGHT["start"], "scheduled_in": FLIGHT["end"],
                "actual_out": (NOW - timedelta(minutes=25)).isoformat(), "actual_off": (NOW - timedelta(minutes=20)).isoformat()}
    live = {"flight_id": FLIGHT["id"], "provider_flight_id": "context-provider", "latitude": 34.2, "longitude": -88.5,
            "observed_at": NOW.isoformat(), "source": "FlightAware", "altitude": 24000, "altitude_units": "feet",
            "groundspeed": 360, "heading": 265}
    return {"view": "operating_map", "demo": False, "backend_available": True, "operating_flight": deepcopy(FLIGHT),
            "operating_status": normalize_operating_status(provider, FLIGHT, NOW), "operating_live": live,
            "timers": [], "shopping_list": {}, **changes}


def radar_state(**changes):
    return {"view": "national_radar", "demo": False, "backend_available": True,
            "national_radar": {"status": "ready", "fetched_at": NOW.isoformat(), "issued_at": NOW.isoformat(),
                               "image_available": True, "image_sha256": "a" * 64}, **changes}


def png(color=PATCH, size=(64, 32), metadata=False):
    buffer = BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("private", "PRIVATE-METADATA")
    Image.new("RGB", size, color).save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def test_radar_reader_is_hash_bound_to_one_fixed_local_png_and_discards_metadata(tmp_path):
    raw = png(metadata=True)
    path = tmp_path / "national-radar.png"
    path.write_bytes(raw)
    connection = display.BackendConnection(tmp_path, {"control_port": 8765})
    digest = hashlib.sha256(raw).hexdigest()
    image = connection.local_radar_image(digest)
    assert image.mode == "RGB" and image.size == (64, 32) and image.info == {}
    assert image.getpixel((0, 0)) == PATCH
    assert connection.local_radar_image("b" * 64) is None
    path.write_bytes(png(color=GREEN))
    assert connection.local_radar_image(digest) is None  # Concurrent cache generation changed.
    for invalid in (None, "../national-radar.png", "A" * 64, "a" * 63):
        assert connection.local_radar_image(invalid) is None


@pytest.mark.parametrize("raw", [b"not a png", b"x" * 4_000_001, png(size=(2049, 1)), png(size=(1, 1025))],
                         ids=["invalid", "oversized-file", "too-wide", "too-tall"])
def test_radar_reader_rejects_bad_or_oversized_content_even_with_matching_hash(tmp_path, raw):
    (tmp_path / "national-radar.png").write_bytes(raw)
    connection = display.BackendConnection(tmp_path, {"control_port": 8765})
    assert connection.local_radar_image(hashlib.sha256(raw).hexdigest()) is None


@pytest.mark.parametrize("view", ["operating_map", "national_radar"])
def test_context_layer_tracks_timer_and_ticker_insets_with_bounded_presentation_resolution(view):
    state = operating_state() if view == "operating_map" else radar_state()
    state["timers"] = [{"id": "tea", "ends_at": (NOW + timedelta(minutes=5)).isoformat()}]
    state["shopping_list"] = {"items": [{"name": "olives"}]}
    connection = SimpleNamespace(local_radar_image=lambda digest: Image.new("RGB", (64, 32), PATCH))
    widths, heights = [], []
    for top, bottom in ((0, 0), (.5, .2), (1, 1)):
        frame = Image.new("RGB", (512, 256), RED)
        with patch.object(operating_map, "render_map", side_effect=lambda *args, **kwargs: Image.new("RGB", args[3], PATCH)):
            display.attach_flight_context(frame, state, config(), connection, (3840, 2160), NOW, top, bottom)
        box, image = frame.info["geographic_patch"]
        left, y, width, height = display_content_bounds(state, NOW, top, bottom)
        assert left <= box[0] < box[0] + box[2] <= left + width
        assert y <= box[1] < box[1] + box[3] <= y + height
        assert 512 < image.width <= 2048 and 256 < image.height <= 1024
        assert set(frame.info) == {"geographic_patch"}
        widths.append(box[2]); heights.append(box[3])
    assert widths[0] > widths[1] > widths[2] and heights[0] > heights[1] > heights[2]


@pytest.mark.parametrize("view", ["operating_map", "national_radar"])
def test_config_demo_and_backend_loss_cannot_be_overridden_by_cached_real_state(view):
    state = operating_state() if view == "operating_map" else radar_state()
    connection = Mock()
    for modified_state, cfg in ((state, config(demo=True)), (state | {"demo": True}, config()),
                                (state | {"backend_available": False}, config())):
        frame = Image.new("RGB", (512, 256))
        with patch.object(operating_map, "render_map") as render:
            display.attach_flight_context(frame, modified_state, cfg, connection, (1920, 1080), NOW)
            render.assert_not_called()
        assert "geographic_patch" not in frame.info
    connection.local_radar_image.assert_not_called()


def test_config_demo_never_reads_the_private_nearby_map_point_from_cached_real_state():
    connection = Mock()
    frame = Image.new("RGB", (512, 256))
    with patch("flightwall.nearby_map.render_map") as render:
        display.attach_nearby_map(frame, {"view": "nearby", "demo": False},
                                 config(demo=True, nearby_enabled=True), connection, (1920, 1080), NOW)
        render.assert_not_called()
    connection.local_map_config.assert_not_called()
    assert "geographic_patch" not in frame.info


@pytest.mark.parametrize("changes", [{"status": "stale"}, {"image_available": False},
    {"fetched_at": (NOW - timedelta(seconds=1200)).isoformat()},
    {"issued_at": (NOW - timedelta(seconds=1800)).isoformat()},
    {"issued_at": (NOW + timedelta(seconds=1)).isoformat()}, {"image_sha256": "../file.png"}])
def test_expired_unknown_or_disabled_radar_never_reads_the_local_image(changes):
    state = radar_state()
    state["national_radar"].update(changes)
    connection = Mock()
    frame = Image.new("RGB", (512, 256))
    display.attach_flight_context(frame, state, config(), connection, (1920, 1080), NOW)
    assert "geographic_patch" not in frame.info
    connection.local_radar_image.assert_not_called()
    display.attach_flight_context(frame, radar_state(), config(national_radar_enabled=False), connection, (1920, 1080), NOW)
    connection.local_radar_image.assert_not_called()


def test_operating_layer_matches_current_flight_and_fails_closed_on_expiry_or_landing():
    state = operating_state()
    before = deepcopy(state)
    good = Image.new("RGB", (512, 256))
    display.attach_flight_context(good, state, config(), object(), (1920, 1080), NOW)
    assert "geographic_patch" in good.info
    assert state == before
    bad_states = [state | {"operating_live": state["operating_live"] | {"flight_id": "another-leg"}},
                  state | {"operating_status": state["operating_status"] | {"phase": "landed"}}]
    for candidate in bad_states:
        frame = Image.new("RGB", (512, 256))
        display.attach_flight_context(frame, candidate, config(), object(), (1920, 1080), NOW)
        assert "geographic_patch" not in frame.info
    expired = Image.new("RGB", (512, 256))
    display.attach_flight_context(expired, state, config(), object(), (1920, 1080), NOW + timedelta(seconds=301))
    assert "geographic_patch" not in expired.info


def test_operating_telemetry_keeps_received_units_and_does_not_guess_altitude_units():
    state = operating_state()
    viewport = operating_map.plan_view(state["operating_flight"], state["operating_status"], state["operating_live"], NOW)
    assert viewport["position"] == (-88.5, 34.2)
    assert viewport["telemetry"] == {"altitude_feet": 24000, "groundspeed_knots": 360, "heading_degrees": 265}
    for units in (None, "meters", "flight-level"):
        changed = state["operating_live"] | {"altitude_units": units}
        viewport = operating_map.plan_view(state["operating_flight"], state["operating_status"], changed, NOW)
        assert viewport["telemetry"]["altitude_feet"] is None


@pytest.mark.parametrize("view", ["operating_map", "national_radar"])
def test_software_context_layer_retains_brightness_without_led_gaps_and_stays_inset(monkeypatch, view):
    pygame = pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    screen = display.ShaderScreen.__new__(display.ShaderScreen)
    screen.pygame, screen.gl, screen.screen = pygame, None, pygame.Surface((1024, 512))
    frame = Image.new("RGB", (512, 256), (255, 255, 255))
    state = operating_state() if view == "operating_map" else radar_state()
    connection = SimpleNamespace(local_radar_image=lambda digest: Image.new("RGB", (64, 32), PATCH))
    with patch.object(operating_map, "render_map", side_effect=lambda *args, **kwargs: Image.new("RGB", args[3], PATCH)):
        display.attach_flight_context(frame, state, config(), connection, (1024, 512), NOW)
    image, box = display.geographic_patch(frame, display.display_viewport((1024, 512)))
    with patch.object(pygame.display, "flip"):
        screen.draw(frame, config(led_effect=True, dot_gap=.5))
    x, y, width, height = box
    # Radar is letterboxed inside its patch; choose its center.
    center = x + width // 2, y + height // 2
    assert tuple(screen.screen.get_at(center))[:3] == (50, 100, 20)
    assert tuple(screen.screen.get_at((center[0] + 1, center[1])))[:3] == (50, 100, 20)
    assert tuple(screen.screen.get_at((1, 1)))[:3] != (50, 100, 20)


def test_split_flap_uses_two_seconds_and_guard_cancellation_forgets_all_prior_frames():
    effect = display.CardTransition(chooser=lambda choices: "split_flap")
    old, current = Image.new("RGB", (512, 256), RED), Image.new("RGB", (512, 256), GREEN)
    old.info["geographic_patch"] = ((0, 0, 1, 1), Image.new("RGB", (1, 1)))
    effect.render(old, "home", b"same", 0, view="home")
    effect.render(current, "personal", b"same", .1, view="personal")
    intermediate = effect.render(current, "personal", b"same", 1.0, view="personal")
    assert effect.active and intermediate.info == {}
    assert RED in set(intermediate.getdata()) and GREEN in set(intermediate.getdata())
    effect.render(current, "personal", b"same", 2.0999, view="personal")
    assert effect.active
    assert effect.render(current, "personal", b"same", 2.1, view="personal").tobytes() == current.tobytes()
    assert not effect.active
    effect.render(old, "home", b"same", 3.0, view="home")
    assert effect.active
    assert effect.render(current, "home", b"changed", 3.1, view="home").tobytes() == current.tobytes()
    assert not effect.active and effect._previous is None and effect._last.info == {}
    effect.render(current, "home", None, 3.2, view="home", enabled=False)
    assert effect._last is effect._previous is None


def gui(monkeypatch, times, events, *, slots, status=None, fail_fetch=False):
    pygame = pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    ticks, wall, index = [0.0], [NOW], [-1]
    data = deepcopy(status or {"config": config(), "snapshot": {"demo": False, "flights": []}, "personal_items": []})
    states, frames, active = [], [], []
    original_screen = display.ShaderScreen
    transitions = []
    class Screen:
        def __init__(self, *args):
            self.pygame, self.gl, self.screen = pygame, None, pygame.display.set_mode((1280, 720))
        def draw(self, frame, cfg):
            frames.append(frame.copy())
            active.append(transitions[0].active)
            original_screen.draw(self, frame, cfg)
    class Transition(display.CardTransition):
        def __init__(self):
            super().__init__(chooser=lambda choices: "split_flap")
            transitions.append(self)
    class Connection:
        count = 0
        def status(self):
            self.count += 1
            if fail_fetch and self.count > 1:
                raise OSError("fictional outage")
            return deepcopy(data)
        def settings(self, changes):
            data["config"].update(changes)
    def render(state, cfg):
        states.append(deepcopy(state))
        return Image.new("RGB", (512, 256), GREEN if state["view"] == "personal" else RED)
    def event_poll():
        index[0] += 1
        ticks[0] = times[index[0]]
        wall[0] = NOW + timedelta(seconds=ticks[0])
        return events[index[0]]
    with patch.object(display, "datetime") as clock, patch.object(display, "ShaderScreen", Screen), \
            patch.object(display, "CardTransition", Transition), patch.object(display, "render_frame", side_effect=render), \
            patch.object(display.time, "monotonic", side_effect=lambda: ticks[0]), \
            patch.object(pygame.event, "get", side_effect=event_poll), \
            patch.object(pygame.time, "Clock", return_value=SimpleNamespace(tick=lambda rate: None)), \
            patch.object(pygame.joystick, "get_count", return_value=0), \
            patch.object(operating_map, "render_map", side_effect=lambda *args, **kwargs: Image.new("RGB", args[3], PATCH)):
        clock.now.side_effect = lambda *args: wall[0]
        if slots is None:
            display.run_gui(Connection(), windowed=True, max_frames=len(times))
        else:
            with patch.object(display, "build_rotation", return_value=slots):
                display.run_gui(Connection(), windowed=True, max_frames=len(times))
    return states, frames, active, transitions[0]


def test_actual_gui_keeps_backend_outage_sticky_across_frames_and_removes_map_layer(monkeypatch):
    state = operating_state()
    status = state | {"config": config(), "snapshot": {"demo": False, "flights": [FLIGHT]},
                      "personal_items": [], "selected_flight": FLIGHT}
    slots = [{"key": "operating_map", "view": "operating_map", "duration_seconds": 15}]
    states, frames, active, effect = gui(monkeypatch, [0, 2.01, 2.2, 3.9], [[], [], [], []], slots=slots,
                                        status=status, fail_fetch=True)
    assert [row["backend_available"] for row in states] == [True, False, False, False]
    assert "geographic_patch" in frames[0].info
    assert all("geographic_patch" not in frame.info for frame in frames[1:])
    assert all(row["operating_live"] is None for row in states[1:])
    assert all(row["operating_status"]["status"] == "stale" for row in states[1:])
    assert effect._last is effect._previous is None


@pytest.mark.parametrize("outage", [False, True])
def test_guest_or_outage_cancels_a_two_second_flap_midway_and_stays_cancelled(monkeypatch, outage):
    pygame = pytest.importorskip("pygame")
    right = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT)
    guest = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_g)
    slots = [{"key": view, "view": view, "duration_seconds": 15} for view in ("home", "personal")]
    times = [1.8, 1.9, 2.01, 2.2] if outage else [0, .1, 1.0, 1.5]
    states, frames, active, effect = gui(monkeypatch, times, [[], [right], [] if outage else [guest], []],
                                        slots=slots, fail_fetch=outage)
    assert active[1] and not active[2] and not active[3]
    assert RED not in set(frames[2].crop((0, 0, 512, 230)).getdata())
    assert effect._last is effect._previous is None


def test_personal_expiry_cancels_the_two_second_reveal_at_its_exact_boundary(monkeypatch):
    pygame = pytest.importorskip("pygame")
    status = {"config": config(), "snapshot": {"demo": False, "flights": []},
              "personal_items": [{"id": "brief-card", "kind": "appointment", "title": "Fictional appointment",
                                  "display_start": NOW.isoformat(), "display_end": (NOW + timedelta(seconds=1.2)).isoformat()}]}
    states, frames, active, effect = gui(monkeypatch, [0, .1, .5, 1.2],
        [[], [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT)], [], []], slots=None, status=status)
    assert states[1]["view"] == "personal" and active[1] and active[2]
    assert states[3]["view"] == "home" and not active[3]
    assert GREEN not in set(frames[3].getdata())
