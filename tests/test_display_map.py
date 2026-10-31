"""High-resolution local map presentation without changing the LED canvas."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from PIL import Image
import pytest

from flightwall.display import BackendConnection, attach_nearby_map, geographic_patch
from flightwall.render import display_content_bounds

NOW = datetime(2026, 10, 21, 18, tzinfo=timezone.utc)


def test_map_point_is_local_only_and_invalid_data_returns_no_point():
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "config.json"
        target.write_text(json.dumps({"home_latitude": 30.4, "home_longitude": -86.5,
                                      "control_token": "synthetic-secret"}))
        connection = BackendConnection(folder, {"control_port": 8765})
        assert connection.local_map_config() == {"home_latitude": 30.4, "home_longitude": -86.5}
        target.write_text('{"home_latitude": true, "home_longitude": -86.5}')
        assert connection.local_map_config() == {}
        target.write_text("not json")
        assert connection.local_map_config() == {}


def test_map_follows_the_entire_content_inset_and_never_exports_the_pin():
    class Connection:
        def local_map_config(self):
            return {"home_latitude": 30.4, "home_longitude": -86.5}
    state = {"view": "nearby", "demo": False, "nearby": [],
             "timers": [{"id": "tea", "label": "Tea", "ends_at": (NOW + timedelta(minutes=5)).isoformat()}],
             "shopping_list": {"items": [{"id": "milk", "name": "milk"}]}}
    config = {"nearby_enabled": True, "nearby_detailed_map": True}
    for top, bottom in ((0, 0), (.5, .2), (1, 1)):
        frame = Image.new("RGB", (512, 256))
        with patch("flightwall.nearby_map.render_map", side_effect=lambda c, size, **kw: Image.new("RGB", size)):
            attach_nearby_map(frame, state, config, Connection(), (3840, 2160), NOW, top, bottom)
        box, image = frame.info["geographic_patch"]
        left, y, width, height = display_content_bounds(state, NOW, top, bottom)
        assert box == (left + 14 * width / 512, y + 56 * height / 256,
                       297 * width / 512, 149 * height / 256)
        assert image.width > 512 and image.height > 256
        assert image.width <= 2048 and image.height <= 1024
        assert "home_latitude" not in str(frame.info.keys())
        assert set(frame.info) == {"geographic_patch"}


def test_demo_disabled_map_and_missing_asset_keep_position_plot():
    frame = Image.new("RGB", (512, 256))
    for state, config in (({"view": "nearby", "demo": True}, {"nearby_enabled": True}),
                          ({"view": "trip_map"}, {"nearby_enabled": True}),
                          ({"view": "nearby"}, {"nearby_enabled": False}),
                          ({"view": "nearby"}, {"nearby_enabled": True, "nearby_detailed_map": False})):
        with patch("flightwall.nearby_map.render_map") as render:
            attach_nearby_map(frame, state, config, object(), (1920, 1080), NOW)
            render.assert_not_called()
        assert "geographic_patch" not in frame.info
    with patch("flightwall.nearby_map.render_map", return_value=None):
        class Connection:
            def local_map_config(self):
                return {}
        attach_nearby_map(frame, {"view": "nearby"}, {"nearby_enabled": True}, Connection(), (1920, 1080), NOW)
    assert "geographic_patch" not in frame.info


def test_invalid_patch_cannot_draw_over_other_views_or_outside_board():
    frame = Image.new("RGB", (512, 256))
    for box in ((-1, 0, 100, 100), (0, 0, 513, 256), (0, 0, 1, float("nan")), (0, 0, 0, 10)):
        frame.info["geographic_patch"] = (box, Image.new("RGB", (10, 10)))
        assert geographic_patch(frame, (0, 60, 1920, 960)) is None


@pytest.mark.skipif(not importlib.util.find_spec("pygame"), reason="pygame not installed")
def test_software_map_bypasses_dots_but_keeps_title_led_and_brightness():
    import pygame
    from flightwall.display import ShaderScreen
    screen = ShaderScreen.__new__(ShaderScreen)
    screen.pygame, screen.gl = pygame, None
    screen.screen = pygame.Surface((1024, 512))
    frame = Image.new("RGB", (512, 256), (255, 255, 255))
    frame.info["geographic_patch"] = ((14, 56, 297, 149), Image.new("RGB", (594, 298), (100, 200, 40)))
    with patch.object(pygame.display, "flip"):
        screen.draw(frame, {"brightness": .5, "led_effect": True, "dot_gap": .5, "glow": 0})
    assert tuple(screen.screen.get_at((100, 150)))[:3] == (50, 100, 20)
    assert tuple(screen.screen.get_at((100, 151)))[:3] == (50, 100, 20)
    assert tuple(screen.screen.get_at((100, 10)))[:3] != (50, 100, 20)


@pytest.mark.skipif(not (importlib.util.find_spec("pygame") and importlib.util.find_spec("OpenGL")),
                    reason="OpenGL dependencies not installed")
def test_actual_gl_map_is_oriented_unshaded_and_handles_odd_rgb_row_width():
    import pygame
    from flightwall.display import ShaderScreen
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
    pygame.display.init()
    try:
        screen = ShaderScreen(pygame, (1536, 768), False)
        if not screen.gl:
            if os.environ.get("FLIGHTWALL_REQUIRE_OPENGL") == "1":
                pytest.fail("Release validation requires actual OpenGL map composition")
            pytest.skip("No OpenGL context on this host")
        frame = Image.new("RGB", (512, 256), (255, 255, 255))
        image = Image.new("RGB", (593, 297), (40, 200, 100))
        image.paste((255, 0, 0), (0, 0, 593, 100))
        image.paste((0, 0, 255), (0, 200, 593, 297))
        frame.info["geographic_patch"] = ((14, 56, 297, 149), image)
        screen.draw(frame, {"brightness": 1, "led_effect": True, "dot_gap": .4, "glow": 0})
        GL = screen.gl
        GL.glReadBuffer(GL.GL_FRONT)
        raw = GL.glReadPixels(0, 0, 1536, 768, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        result = Image.frombytes("RGB", (1536, 768), raw).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        assert result.getpixel((100, 190)) == (255, 0, 0)
        assert result.getpixel((100, 390)) == (40, 200, 100)
        assert result.getpixel((101, 390)) == (40, 200, 100)
        assert result.getpixel((100, 580)) == (0, 0, 255)
        assert result.getpixel((100, 20)) != result.getpixel((101, 20))
    finally:
        pygame.quit()
