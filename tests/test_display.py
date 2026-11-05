"""TV presentation checks, including actual OpenGL framebuffer readback."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import os
import unittest
from unittest.mock import patch

from PIL import Image

from flightwall.config import DEFAULTS
from flightwall.display import display_viewport, is_sleeping, led_image, TimerStripTransition
from flightwall.render import demo_snapshot


class PresentationTests(unittest.TestCase):
    def test_fit_and_integer_preserve_the_complete_canvas_at_hd_and_4k(self):
        self.assertEqual(display_viewport((1920, 1080)), (0, 60, 1920, 960))
        self.assertEqual(display_viewport((1920, 1080), mode="integer"), (192, 156, 1536, 768))
        self.assertEqual(display_viewport((3840, 2160)), (0, 120, 3840, 1920))
        self.assertEqual(display_viewport((3840, 2160), mode="integer"), (128, 184, 3584, 1792))
        for size in ((1280, 720), (321, 201), (300, 100), (901, 1701), (1, 1), (0, 0)):
            for mode in ("fit", "integer"):
                x, y, width, height = display_viewport(size, mode=mode)
                self.assertEqual(width, height * 2)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + width, size[0])
                self.assertLessEqual(y + height, size[1])

    def test_timer_strip_transition_is_continuous_reversible_and_settles(self):
        animation = TimerStripTransition(duration=1)
        self.assertEqual(animation.value(5, False), 0)
        self.assertEqual(animation.value(6, True), 0)
        self.assertAlmostEqual(animation.value(6.5, True), .5)
        self.assertAlmostEqual(animation.value(6.5, False), .5)
        self.assertAlmostEqual(animation.value(7, False), .25)
        self.assertEqual(animation.value(7.5, False), 0)
        self.assertEqual(animation.value(8, True), 0)
        self.assertEqual(animation.value(9, True), 1)
        self.assertEqual(animation.value(90, True), 1)

    def test_supported_gaps_work_at_small_tv_scales(self):
        source = Image.new("RGB", (16, 8), (220, 120, 30))
        for scale in (1, 2, 3, 4, 8):
            for gap in (0, .22, .5, .65):
                with self.subTest(scale=scale, gap=gap):
                    image = led_image(source, {"brightness": 1, "led_effect": True, "dot_gap": gap, "glow": .25}, scale)
                    self.assertEqual(image.size, (16 * scale, 8 * scale))
                    self.assertEqual(image.mode, "RGB")
                    self.assertGreater(image.getbbox()[2], 0)

    def test_disabled_effect_preserves_pixels_and_brightness(self):
        source = Image.new("RGB", (4, 4), (200, 120, 40))
        image = led_image(source, {"brightness": .5, "led_effect": False}, 3)
        self.assertEqual(image.getpixel((4, 4)), (100, 60, 20))
        self.assertEqual(source.getpixel((1, 1)), (200, 120, 40))

    def test_sleep_schedule_handles_midnight_and_local_timezone(self):
        config = {"sleep_enabled": True, "sleep_start": "23:00", "sleep_end": "06:00", "timezone": "America/New_York"}
        self.assertTrue(is_sleeping(config, datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)))
        self.assertFalse(is_sleeping(config, datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)))
        config["sleep_start"] = config["sleep_end"]
        self.assertFalse(is_sleeping(config))


@unittest.skipUnless(importlib.util.find_spec("pygame"), "pygame is optional for renderer-only tests")
class GuiInputTests(unittest.TestCase):
    def test_timer_survives_settings_overlay_and_expires_while_rotation_is_paused(self):
        import pygame
        from flightwall.display import run_gui
        from flightwall.render import compose_display_overlays
        os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
        now = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)
        timers = [{"id": "tea", "label": "TEA", "ends_at": (now + timedelta(seconds=1)).isoformat()}]
        shopping = {"items": [{"id": "milk", "name": "Milk"}]}
        status = {"config": deepcopy(DEFAULTS), "snapshot": demo_snapshot(now), "timers": timers, "shopping_list": shopping}

        class Connection:
            def status(self):
                return deepcopy(status)

        class Screen:
            gl = None
            frames = []
            def __init__(self, *args):
                pass
            def draw(self, frame, config):
                self.frames.append(frame.copy())

        frame_index = [0]
        with patch("flightwall.display.datetime") as wall_clock, patch("flightwall.display.ShaderScreen", Screen), \
                patch("flightwall.display.time.monotonic", return_value=0):
            wall_clock.now.return_value = now
            def events():
                index = frame_index[0]
                frame_index[0] += 1
                wall_clock.now.return_value = now + timedelta(seconds=index)
                return [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_s), pygame.event.Event(pygame.KEYDOWN, key=pygame.K_SPACE)] if index == 0 else []
            with patch.object(pygame.event, "get", side_effect=events):
                run_gui(Connection(), windowed=True, max_frames=2)
        for index, image in enumerate(Screen.frames):
            expected = compose_display_overlays(Image.new("RGB", (512, 256)), {"timers": timers, "shopping_list": shopping}, now + timedelta(seconds=index))
            self.assertEqual(image.crop((0, 0, 512, 36)).tobytes(), expected.crop((0, 0, 512, 36)).tobytes())
            self.assertEqual(image.crop((0, 238, 512, 256)).tobytes(), expected.crop((0, 238, 512, 256)).tobytes())
        self.assertNotEqual(Screen.frames[0].crop((0, 0, 512, 36)).tobytes(), Screen.frames[1].crop((0, 0, 512, 36)).tobytes())

    def test_software_fallback_uses_fit_without_cropping_or_stretching(self):
        import pygame
        from flightwall.display import ShaderScreen
        screen = ShaderScreen.__new__(ShaderScreen)
        screen.pygame, screen.gl = pygame, None
        screen.screen = pygame.Surface((1920, 1080))
        frame = Image.new("RGB", (512, 256), (255, 255, 255))
        frame.paste((255, 0, 0), (0, 0, 20, 20))
        frame.paste((0, 0, 255), (492, 236, 512, 256))
        with patch.object(pygame.display, "flip"):
            screen.draw(frame, {"display_scale_mode": "fit", "led_effect": False, "brightness": 1})
        self.assertEqual(tuple(screen.screen.get_at((0, 59)))[:3], (0, 0, 0))
        self.assertEqual(tuple(screen.screen.get_at((0, 60)))[:3], (255, 0, 0))
        self.assertEqual(tuple(screen.screen.get_at((1919, 1019)))[:3], (0, 0, 255))
        self.assertEqual(tuple(screen.screen.get_at((1919, 1020)))[:3], (0, 0, 0))

    def test_software_leds_at_1080p_fit_have_dark_gaps_and_unblended_centers(self):
        import pygame
        from flightwall.display import ShaderScreen
        screen = ShaderScreen.__new__(ShaderScreen)
        screen.pygame, screen.gl = pygame, None
        screen.screen = pygame.Surface((1920, 1080))
        frame = Image.new("RGB", (512, 256), (255, 255, 255))
        with patch.object(pygame.display, "flip"):
            screen.draw(frame, {"display_scale_mode": "fit", "brightness": 1, "dot_gap": .2})
        row = [screen.screen.get_at((x, 61)).r for x in range(1920)]
        self.assertEqual(set(row), {0, 255})
        self.assertEqual(row.count(0), 512)
        self.assertEqual(sum(a == 0 and b == 255 for a, b in zip(row, row[1:])), 512)

    def test_refresh_and_led_shortcuts_survive_backend_outage(self):
        import pygame
        from flightwall.display import run_gui
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")

        class Connection:
            def status(self):
                return {"config": deepcopy(DEFAULTS), "snapshot": demo_snapshot(), "on_duty": True}

            def settings(self, value):
                raise OSError("Disconnected")

            def request(self, *args):
                raise OSError("Disconnected")

        class Screen:
            gl = None
            frames = []

            def __init__(self, *args):
                pass

            def draw(self, frame, config):
                self.frames.append(frame)

        events = [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_l),
                  pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r)]
        with patch("flightwall.display.ShaderScreen", Screen), patch.object(pygame.event, "get", return_value=events):
            result = run_gui(Connection(), windowed=True, max_frames=1)
        self.assertEqual(result["frames"], 1)
        self.assertEqual(len(Screen.frames), 1)


@unittest.skipUnless(importlib.util.find_spec("pygame") and importlib.util.find_spec("OpenGL"), "OpenGL dependencies not installed")
class ShaderTests(unittest.TestCase):
    def test_real_shader_orientation_letterbox_and_led_gap_at_3x(self):
        import pygame
        from flightwall.display import ShaderScreen
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
        os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp")
        os.environ.setdefault("MESA_SHADER_CACHE_DIR", "/tmp/flightwall-test-mesa")
        pygame.display.init()
        try:
            screen = ShaderScreen(pygame, (320, 240), False)
            if not screen.gl:
                if os.environ.get("FLIGHTWALL_REQUIRE_OPENGL") == "1":
                    self.fail("Release validation requires an actual offscreen OpenGL context")
                self.skipTest("Host cannot create an OpenGL context")
            GL = screen.gl
            frame = Image.new("RGB", (64, 64), (255, 255, 255))
            frame.paste((255, 0, 0), (0, 0, 20, 20))
            frame.paste((0, 0, 255), (44, 44, 64, 64))

            def capture(config):
                screen.draw(frame, {"display_scale_mode": "integer", **config})
                GL.glReadBuffer(GL.GL_FRONT)
                data = GL.glReadPixels(0, 0, 320, 240, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
                return Image.frombytes("RGB", (320, 240), data).transpose(Image.Transpose.FLIP_TOP_BOTTOM)

            plain = capture({"led_effect": False, "brightness": 1})
            # Native integer scale is 3x; horizontal/vertical margins are 64/24.
            self.assertEqual(plain.getpixel((0, 0)), (0, 0, 0))
            self.assertEqual(plain.getpixel((65, 25)), (255, 0, 0))
            self.assertEqual(plain.getpixel((254, 214)), (0, 0, 255))
            led = capture({"led_effect": True, "brightness": 1, "dot_gap": .22, "glow": 0})
            # A white source pixel has a bright center and darker cell edge.
            center = led.getpixel((64 + 30 * 3 + 1, 24 + 30 * 3 + 1))[0]
            edge = led.getpixel((64 + 30 * 3, 24 + 30 * 3))[0]
            self.assertGreater(center, edge + 30)
            self.assertNotEqual(plain.tobytes(), led.tobytes())
            fit = capture({"display_scale_mode": "fit", "led_effect": False, "brightness": 1})
            self.assertEqual(fit.getpixel((39, 0)), (0, 0, 0))
            self.assertEqual(fit.getpixel((40, 0)), (255, 0, 0))
            self.assertEqual(fit.getpixel((279, 239)), (0, 0, 255))
            self.assertEqual(fit.getpixel((280, 239)), (0, 0, 0))
            fit_led = capture({"display_scale_mode": "fit", "brightness": 1, "dot_gap": .2})
            # 3.75x is also the board pitch on a 1080p screen. There is
            # exactly one black gap per dot, with no phase-dependent fade.
            row = [fit_led.getpixel((x, 113))[0] for x in range(40, 280)]
            self.assertEqual(set(row), {0, 255})
            self.assertEqual(row.count(0), 64)
            self.assertEqual(sum(a == 0 and b == 255 for a, b in zip(row, row[1:])), 64)
        finally:
            pygame.quit()


if __name__ == "__main__":
    unittest.main()
