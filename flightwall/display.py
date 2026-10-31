"""TV presentation: true LED grid shader, with a software fallback."""
from __future__ import annotations

import os
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from datetime import datetime, timezone
from functools import lru_cache
import base64
import io
import json
import math
from pathlib import Path
import time
import urllib.request
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageEnhance

from .render import compose_display_overlays, display_content_bounds, render_frame, shopping_ticker_names, visible_timers
from .feeds import parse_time
from .household_policy import build_rotation, card_visible, filter_personal_items, household_context


def fresh_household_status(status, config, now=None, *, backend_available=True):
    """Recheck cached presence every frame; cached booleans are insufficient."""
    now = now or datetime.now(timezone.utc)
    result = dict(status)
    source_presence = status.get("presence") or {}
    presence = {}
    for profile in ("chris", "lisa"):
        record = source_presence.get(profile) if isinstance(source_presence, dict) else None
        valid = False
        if backend_available and config.get("presence_enabled") is True and isinstance(record, dict) and record.get("stale") is False:
            try:
                observed = parse_time(record.get("last_seen") or record.get("observed_at"))
                age = (now - observed).total_seconds()
                valid = -300 <= age < config.get("presence_ttl_minutes", 90) * 60 and record.get("state") in {"home", "away"}
            except (ValueError, TypeError, KeyError, OverflowError):
                pass
        presence[profile] = dict(record) if valid else {"state": "unknown", "stale": True, "last_seen": None}
    result["presence"] = presence
    kids = status.get("kids")
    valid = False
    if backend_available and isinstance(kids, dict) and kids.get("stale") is False:
        try:
            age = (now - parse_time(kids.get("last_sync"))).total_seconds()
            until = parse_time(kids.get("until"))
            valid = (-300 <= age <= config.get("kids_stale_minutes", 30) * 60 and now < until
                     and kids.get("state") in {"home", "away"})
        except (ValueError, TypeError, KeyError, OverflowError):
            pass
    result["kids"] = dict(kids) if valid else {"state": "unknown", "stale": True, "last_sync": None, "until": None}
    outlook = status.get("custody_outlook")
    if isinstance(outlook, dict):
        try:
            age = (now - parse_time(outlook.get("last_sync"))).total_seconds()
            fresh = backend_available and -300 <= age <= config.get("kids_stale_minutes", 30) * 60
        except (ValueError, TypeError, KeyError, OverflowError):
            fresh = False
        result["custody_outlook"] = dict(outlook, stale=outlook.get("stale") is not False or not fresh)
    if not backend_available:
        result["live"], result["nearby"] = None, []
    from .flight_alerts import fresh_status
    result["operating_status"] = fresh_status(status.get("operating_status"), now, backend_available=backend_available)
    if not backend_available:
        weather = status.get("weather")
        if isinstance(weather, dict):
            weather = dict(weather, status="stale")
            for key in ("today", "tomorrow", "overnight"):
                section = weather.get(key)
                if isinstance(section, dict):
                    weather[key] = dict(section, stale=True)
            result["weather"] = weather
        watches = status.get("flight_watches")
        if isinstance(watches, list):
            result["flight_watches"] = [dict(row, live=None, status="stale" if isinstance(row.get("flight"), dict) else "unavailable")
                                        for row in watches if isinstance(row, dict)]
    result["household_context"] = household_context(result, config, now)
    return result


def decode_artwork(data_url):
    """Decode the local service's bounded PNG payload; never open a path."""
    if not isinstance(data_url, str) or len(data_url) > 1_100_000 or not data_url.startswith("data:image/png;base64,"):
        return None
    return _decode_artwork_png(data_url)


@lru_cache(maxsize=8)
def _decode_artwork_png(data_url):
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        if len(raw) > 800_000:
            return None
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or not 1 <= image.width <= 512 or not 1 <= image.height <= 256:
                return None
            return image.convert("RGB")
    except Exception:
        return None


def entry_render_state(status, config, entry, now=None):
    """Compose only the selected slot; the renderer receives no file paths."""
    now = now or datetime.now(timezone.utc)
    status = fresh_household_status(status, config, now)
    view = entry["view"]
    selected = entry.get("selected_flight") or status.get("selected_flight")
    live = status.get("live")
    if not isinstance(live, dict) or not isinstance(selected, dict) or not selected.get("id") or live.get("flight_id") != selected["id"]:
        live = None
    if live:
        try:
            if abs((now - parse_time(live.get("observed_at"))).total_seconds()) > 300:
                live = None
        except (ValueError, TypeError, OverflowError):
            live = None
    item = dict(entry["item"]) if isinstance(entry.get("item"), dict) else None
    image_data = item.pop("image_data", None) if item else None
    context = household_context(status, config, now)
    state = {**status.get("snapshot", {}), "now": now.isoformat(), "view": view,
             "selected_flight": selected, "live": live, "nearby": status.get("nearby", []),
             "household_context": context,
             "timers": status.get("timers", []), "custody_outlook": status.get("custody_outlook", {}),
             "shopping_list": status.get("shopping_list", {}),
             "flight_watches": status.get("flight_watches", []),
             "weather": status.get("weather", {}),
             "operating_status": status.get("operating_status", {}),
             "operating_flight": status.get("operating_flight"),
             "trip_preview": entry.get("trip_preview", status.get("trip_preview", {})), "trip_page": entry.get("trip_page", 0),
             "personal_items": filter_personal_items(status.get("personal_items"), status, config, now),
             "selected_personal_item": item if view == "personal" else None,
             "selected_card": item if view in {"message", "inspiration", "health", "layover", "local_visit", "flight_watch"} else None,
             "selected_layover": item if view == "layover" else None,
             "setup": (item or status.get("setup") or {}) if view == "setup" else None}
    if view in {"personal", "message", "inspiration", "health", "layover", "local_visit", "flight_watch", "flight_status", "trip_summary", "trip_map", "home", "setup", "custody_outlook"}:
        state["demo"] = False
    if view == "inspiration" and image_data and card_visible(view, item, context, config, now):
        artwork = decode_artwork(image_data)
        if artwork is not None:
            state["artwork_image"] = artwork
    return state


class RotationCursor:
    """Retain a stable slot key while eligible cards are inserted or removed."""
    def __init__(self, entries, now):
        self.index, self.key, self.started_at = 0, None, now
        self.reconcile(entries, now)

    def reconcile(self, entries, now):
        keys = [entry["key"] for entry in entries]
        if self.key in keys:
            self.index = keys.index(self.key)
        else:
            self.index, self.started_at = 0, now
            self.key = keys[0] if keys else None

    def select(self, entries, index, now):
        if entries:
            self.index = index % len(entries)
            self.key, self.started_at = entries[self.index]["key"], now

    def advance(self, entries, now):
        self.select(entries, self.index + 1, now)

    def tick(self, entries, now, automatic=True):
        self.reconcile(entries, now)
        if entries and automatic and now - self.started_at >= entries[self.index]["duration_seconds"]:
            self.advance(entries, now)
        return entries[self.index] if entries else None


def active_personal_items(status, config, now=None):
    """Filter again between polls so a card disappears at its exact end time."""
    if not config.get("personal_enabled", True):
        return []
    now = now or datetime.now(timezone.utc)
    result = []
    for item in status.get("personal_items", []):
        try:
            if item.get("cancelled") or item.get("kind") not in {"appointment", "dinner"}:
                continue
            if parse_time(item["display_start"]) <= now < parse_time(item["display_end"]):
                result.append(item)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return result


def rotation_views(status, config, now=None):
    views = ["nearby", "schedule", "map", "details"] if config.get("nearby_enabled") and not status.get("on_duty") else ["details", "map", "schedule"]
    if active_personal_items(status, config, now):
        views.append("personal")
    return views


def is_sleeping(config, now=None):
    if not config.get("sleep_enabled"):
        return False
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(config.get("timezone", "UTC")))
    current = now.strftime("%H:%M")
    start, end = config["sleep_start"], config["sleep_end"]
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


@lru_cache(maxsize=4)
def _led_mask(width, height, scale, gap):
    # Supersample one cell so large gaps remain valid at small TV scales.
    # Drawing directly at 2x used to invert the rectangle at gap > 0.5.
    high_scale = scale * 8
    tile = Image.new("L", (high_scale, high_scale))
    draw = ImageDraw.Draw(tile)
    margin = gap * high_scale / 2
    draw.rounded_rectangle((margin, margin, high_scale - 1 - margin, high_scale - 1 - margin),
                           radius=max(1, (high_scale - 2 * margin) * .15), fill=255)
    tile = tile.resize((scale, scale), Image.Resampling.LANCZOS)
    # Repeat a full row rather than pasting each of 131,072 cells per frame.
    row = Image.new("L", (width * scale, scale))
    for x in range(0, row.width, scale):
        row.paste(tile, (x, 0))
    mask = Image.new("L", (width * scale, height * scale))
    for y in range(0, mask.height, scale):
        mask.paste(row, (0, y))
    return mask


def led_image(frame, config, scale=4):
    """Export the same logical pixel layout with a portable LED treatment."""
    scale = max(1, min(int(scale), 8))
    out = frame.convert("RGB").resize((frame.width * scale, frame.height * scale), Image.Resampling.NEAREST)
    if config.get("led_effect", True) and scale > 1:
        gap = max(0, min(float(config.get("dot_gap", .22)), .65))
        mask = _led_mask(frame.width, frame.height, scale, round(gap, 4))
        out = ImageChops.multiply(out, Image.merge("RGB", (mask, mask, mask)))
        if config.get("glow", 0):
            halo = out.filter(ImageFilter.GaussianBlur(max(.5, scale * .32)))
            out = ImageChops.add(out, ImageEnhance.Brightness(halo).enhance(config["glow"] * .6))
    return ImageEnhance.Brightness(out).enhance(config.get("brightness", .7))


class BackendConnection:
    def __init__(self, data_dir, config):
        self.data_dir = Path(data_dir)
        self.base = f"http://127.0.0.1:{config['control_port']}"

    def request(self, path, patch=None):
        token = (self.data_dir / "control-token.txt").read_text().strip()
        req = urllib.request.Request(self.base + "/api/v1/" + path,
                                     data=None if patch is None else json.dumps(patch).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        # Never send a local control token to a proxy or a redirect.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=2) as response:
            return json.load(response)

    def status(self):
        return self.request("display")

    def settings(self, patch):
        return self.request("settings", patch)

    def local_map_config(self):
        """Read only the native GUI's own private map point, never a web payload.

        Do not add the home pin to display/status/MCP JSON. Rendered map pixels
        are local presentation data and are not written back to disk.
        """
        from .config import validate
        try:
            saved = json.loads((self.data_dir / "config.json").read_text())
            return {key: validate(key, saved.get(key)) for key in ("home_latitude", "home_longitude")}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}


def display_viewport(screen_size, frame_size=(512, 256), mode="fit"):
    """Center a complete canvas with the same aspect ratio in both renderers.

    Fit uses all available width/height; integer retains the legacy LED pitch.
    Neither changes the desktop resolution or stretches a 2:1 board to 16:9.
    """
    width, height = screen_size
    frame_width, frame_height = frame_size
    common = math.gcd(frame_width, frame_height)
    unit_width, unit_height = frame_width // common, frame_height // common
    integer_scale = min(width // frame_width, height // frame_height)
    if mode == "integer" and integer_scale >= 1:
        dest_width, dest_height = frame_width * integer_scale, frame_height * integer_scale
    else:
        units = max(0, min(width // unit_width, height // unit_height))
        dest_width, dest_height = unit_width * units, unit_height * units
    return (width - dest_width) // 2, (height - dest_height) // 2, dest_width, dest_height


class TimerStripTransition:
    """A reversible ease driven by monotonic time, independent of timer expiry."""
    def __init__(self, active=False, duration=.4):
        self.origin = self.target = float(bool(active))
        self.started_at = None
        self.duration = duration

    def value(self, now, active):
        elapsed = self.duration if self.started_at is None else max(0.0, now - self.started_at)
        fraction = min(1.0, elapsed / self.duration)
        eased = fraction * fraction * (3 - 2 * fraction)
        current = self.origin + (self.target - self.origin) * eased
        target = float(bool(active))
        if target != self.target:
            self.origin, self.target, self.started_at = current, target, now
        return current


def attach_nearby_map(frame, state, config, connection, screen_size, now,
                      timer_progress=None, ticker_progress=None):
    """Attach a bounded presentation layer while keeping the board at 512x256.

    The map is drawn after the LED pass, in the same animated content inset.
    Private coordinates are read locally and never attached to frame metadata.
    """
    if (state.get("view") != "nearby" or state.get("demo", config.get("demo", False))
            or config.get("nearby_enabled") is not True or config.get("nearby_detailed_map", True) is not True):
        return frame
    try:
        point = connection.local_map_config()
        from .nearby_map import render_map
        left, top, width, height = display_content_bounds(state, now, timer_progress, ticker_progress)
        box = (left + 14 * width / 512, top + 56 * height / 256,
               297 * width / 512, 149 * height / 256)
        _, _, dest_w, dest_h = display_viewport(screen_size, frame.size, config.get("display_scale_mode", "fit"))
        size = (max(1, min(2048, round(box[2] * dest_w / 512))),
                max(1, min(1024, round(box[3] * dest_h / 256))))
        image = render_map({**config, **point}, size, rows=state.get("nearby", []), now=now)
        if isinstance(image, Image.Image):
            frame.info["geographic_patch"] = (box, image)
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        # A missing regional asset or private point retains the position plot.
        pass
    return frame


def geographic_patch(frame, viewport):
    """Translate a trusted local image patch into display pixels, with bounds."""
    try:
        box, image = frame.info["geographic_patch"]
        left, top, width, height = box
        if (not isinstance(image, Image.Image) or image.mode != "RGB"
                or not 1 <= image.width <= 2048 or not 1 <= image.height <= 1024
                or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in box)
                or left < 0 or top < 0 or width <= 0 or height <= 0
                or left + width > frame.width or top + height > frame.height):
            return None
        vx, vy, vw, vh = viewport
        x1, y1 = round(vx + left / frame.width * vw), round(vy + top / frame.height * vh)
        x2, y2 = round(vx + (left + width) / frame.width * vw), round(vy + (top + height) / frame.height * vh)
        return (image, (x1, y1, x2 - x1, y2 - y1)) if x2 > x1 and y2 > y1 else None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


class ShaderScreen:
    def __init__(self, pygame, size, fullscreen):
        self.pygame = pygame
        self.gl = None
        self.program = None
        flags = pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE
        try:
            from OpenGL import GL
            from OpenGL.GL.shaders import compileProgram, compileShader
            pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 2)
            pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 1)
            self.screen = pygame.display.set_mode(size, flags | pygame.OPENGL | pygame.DOUBLEBUF)
            vertex = """#version 120
            varying vec2 uv;
            void main(){gl_Position=gl_Vertex; uv=gl_MultiTexCoord0.xy;}
            """
            fragment = """#version 120
            uniform sampler2D board; uniform vec2 grid;
            uniform float level; uniform float gap; uniform float bloom; uniform float enabled;
            varying vec2 uv;
            void main(){
                vec2 cell=(floor(uv*grid)+vec2(0.5))/grid;
                vec3 c=texture2D(board,cell).rgb;
                vec2 p=abs(fract(uv*grid)-vec2(0.5))*2.0;
                // Integrate the LED edge over the display pixel footprint.
                // A fixed edge misses every gap at the common 3x TV scale.
                vec2 footprint=max(fwidth(uv*grid)*2.0,vec2(0.001));
                vec2 coverage=clamp((vec2(1.0-gap)-p)/footprint+vec2(0.5),0.0,1.0);
                float mask=coverage.x*coverage.y;
                vec3 halo=vec3(0.0);
                halo+=texture2D(board,cell+vec2(1.0/grid.x,0.0)).rgb;
                halo+=texture2D(board,cell-vec2(1.0/grid.x,0.0)).rgb;
                halo+=texture2D(board,cell+vec2(0.0,1.0/grid.y)).rgb;
                halo+=texture2D(board,cell-vec2(0.0,1.0/grid.y)).rgb;
                vec3 led=c*(0.04+0.96*mask)+bloom*(halo*0.045+c*0.12);
                gl_FragColor=vec4(mix(c,led,enabled)*level,1.0);
            }
            """
            self.program = compileProgram(compileShader(vertex, GL.GL_VERTEX_SHADER), compileShader(fragment, GL.GL_FRAGMENT_SHADER))
            self.texture = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture)
            for axis in (GL.GL_TEXTURE_WRAP_S, GL.GL_TEXTURE_WRAP_T):
                GL.glTexParameteri(GL.GL_TEXTURE_2D, axis, GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
            self.gl = GL
        except Exception:
            self.screen = pygame.display.set_mode(size, flags)
        pygame.display.set_caption("Flight Wall")
        pygame.mouse.set_visible(False)

    def draw(self, frame, config):
        pygame = self.pygame
        width, height = self.screen.get_size()
        x, y, dest_w, dest_h = display_viewport((width, height), frame.size, config.get("display_scale_mode", "fit"))
        patch = geographic_patch(frame, (x, y, dest_w, dest_h))
        if not dest_w or not dest_h:
            if self.gl:
                self.gl.glClearColor(0, 0, 0, 1)
                self.gl.glClear(self.gl.GL_COLOR_BUFFER_BIT)
            else:
                self.screen.fill((0, 0, 0))
            pygame.display.flip()
            return
        if self.gl:
            GL = self.gl
            GL.glViewport(0, 0, width, height)
            GL.glClearColor(0, 0, 0, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            GL.glViewport(x, y, dest_w, dest_h)
            GL.glUseProgram(self.program)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture)
            # RGB map rows may have any width; do not assume four-byte padding.
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB, frame.width, frame.height, 0, GL.GL_RGB, GL.GL_UNSIGNED_BYTE, frame.tobytes())
            GL.glUniform1i(GL.glGetUniformLocation(self.program, "board"), 0)
            GL.glUniform2f(GL.glGetUniformLocation(self.program, "grid"), frame.width, frame.height)
            for name, val in {"level": config.get("brightness", .7), "gap": config.get("dot_gap", .22), "bloom": config.get("glow", .25), "enabled": float(config.get("led_effect", True))}.items():
                GL.glUniform1f(GL.glGetUniformLocation(self.program, name), val)
            GL.glBegin(GL.GL_QUADS)
            for uv, point in (((0, 1), (-1, -1)), ((1, 1), (1, -1)), ((1, 0), (1, 1)), ((0, 0), (-1, 1))):
                GL.glTexCoord2f(*uv)
                GL.glVertex2f(*point)
            GL.glEnd()
            if patch:
                image, (px, py, pw, ph) = patch
                GL.glViewport(px, height - py - ph, pw, ph)
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB, image.width, image.height, 0,
                                GL.GL_RGB, GL.GL_UNSIGNED_BYTE, image.tobytes())
                GL.glUniform2f(GL.glGetUniformLocation(self.program, "grid"), image.width, image.height)
                GL.glUniform1f(GL.glGetUniformLocation(self.program, "enabled"), 0)
                GL.glBegin(GL.GL_QUADS)
                for uv, point in (((0, 1), (-1, -1)), ((1, 1), (1, -1)), ((1, 0), (1, 1)), ((0, 0), (-1, 1))):
                    GL.glTexCoord2f(*uv)
                    GL.glVertex2f(*point)
                GL.glEnd()
            GL.glUseProgram(0)
        else:
            self.screen.fill((0, 0, 0))
            factor = max(1, math.ceil(dest_w / frame.width))
            out = led_image(frame, config, factor)
            surface = pygame.image.frombytes(out.tobytes(), out.size, "RGB")
            if surface.get_size() != (dest_w, dest_h):
                surface = pygame.transform.smoothscale(surface, (dest_w, dest_h))
            self.screen.blit(surface, (x, y))
            if patch:
                image, (px, py, pw, ph) = patch
                image = ImageEnhance.Brightness(image).enhance(config.get("brightness", .7))
                surface = pygame.image.frombytes(image.tobytes(), image.size, "RGB")
                if surface.get_size() != (pw, ph):
                    surface = pygame.transform.smoothscale(surface, (pw, ph))
                self.screen.blit(surface, (px, py))
        pygame.display.flip()


def run_gui(connection, *, windowed=False, max_frames=None, on_healthy=None):
    import pygame
    from .render import _text, PANEL, CYAN, WHITE, AMBER, MUTED

    pygame.display.init()
    pygame.joystick.init()
    # Keep handles alive; their destruction can close SDL's joystick device.
    joysticks = {}
    for i in range(pygame.joystick.get_count()):
        joystick = pygame.joystick.Joystick(i)
        joystick.init()
        joysticks[joystick.get_instance_id()] = joystick
    screen = ShaderScreen(pygame, (1280, 720) if windowed else (0, 0), not windowed)
    status = connection.status()
    config = dict(status["config"])
    initial_status = fresh_household_status(status, config, datetime.now(timezone.utc))
    entries = build_rotation(initial_status, config, datetime.now(timezone.utc))
    cursor = RotationCursor(entries, time.monotonic())
    automatic, settings_open, settings_row, help_open = True, False, 0, False
    settings_rows = ["brightness", "glow", "dot_gap", "display_scale_mode", "rotation_seconds", "led_effect", "sleep_enabled", "shopping_ticker_visible"]
    last_fetch, frames = time.monotonic(), 0
    clock = pygame.time.Clock()
    running, error, backend_available = True, None, True
    guest_hide_latch = False
    timer_transition = TimerStripTransition(bool(visible_timers(initial_status, datetime.now(timezone.utc))))
    ticker_transition = TimerStripTransition(bool(shopping_ticker_names(initial_status)))
    ticker_started_at = last_fetch
    ticker_names = tuple(shopping_ticker_names(initial_status))

    def save_setting(name, value):
        nonlocal error, guest_hide_latch
        if name == "guest_mode" and value is True:
            # The physical hide button works even during a network failure.
            guest_hide_latch = True
            config[name] = True
        try:
            connection.settings({name: value})
            config[name] = value
            if name == "guest_mode" and value is False:
                guest_hide_latch = False
            error = None
        except Exception:
            error = "GUEST MODE ON / SETTING NOT SAVED" if name == "guest_mode" and value is True else "SETTING NOT SAVED / CONNECTION LOST"

    def change_setting(direction):
        nonlocal config, error
        name = settings_rows[settings_row]
        if name in {"led_effect", "sleep_enabled", "shopping_ticker_visible"}:
            value = not config.get(name, True)
        elif name == "display_scale_mode":
            value = "integer" if config.get(name, "fit") == "fit" else "fit"
        else:
            step, low, high = {"brightness": (.05, .05, 1), "glow": (.05, 0, 1), "dot_gap": (.05, 0, .65), "rotation_seconds": (1, 3, 300)}[name]
            value = max(low, min(high, round(config[name] + direction * step, 2)))
        save_setting(name, value)

    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.JOYDEVICEADDED:
                    joystick = pygame.joystick.Joystick(event.device_index)
                    joystick.init()
                    joysticks[joystick.get_instance_id()] = joystick
                elif event.type == pygame.JOYDEVICEREMOVED:
                    joystick = joysticks.pop(event.instance_id, None)
                    if joystick:
                        joystick.quit()
                elif event.type == pygame.JOYBUTTONDOWN:
                    if event.button in {6, 8}:
                        running = False
                    elif event.button in {1, 2}:
                        settings_open = not settings_open
                    else:
                        cursor.advance(entries, time.monotonic())
                elif event.type == pygame.JOYHATMOTION and settings_open:
                    if event.value[1]:
                        settings_row = (settings_row - event.value[1]) % len(settings_rows)
                    if event.value[0]:
                        change_setting(event.value[0])
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        if settings_open or help_open:
                            settings_open = help_open = False
                        else:
                            running = False
                    elif event.key == pygame.K_s:
                        settings_open = not settings_open
                    elif event.key in {pygame.K_h, pygame.K_F1}:
                        help_open = not help_open
                    elif event.key == pygame.K_SPACE:
                        automatic = not automatic
                        cursor.started_at = time.monotonic()
                    elif event.key == pygame.K_TAB:
                        cursor.advance(entries, time.monotonic())
                    elif pygame.K_1 <= event.key <= pygame.K_5:
                        chosen = event.key - pygame.K_1
                        if chosen < len(entries):
                            cursor.select(entries, chosen, time.monotonic())
                            automatic = False
                    elif settings_open and event.key in {pygame.K_UP, pygame.K_DOWN}:
                        settings_row = (settings_row + (1 if event.key == pygame.K_DOWN else -1)) % len(settings_rows)
                    elif settings_open and event.key in {pygame.K_LEFT, pygame.K_RIGHT, pygame.K_RETURN}:
                        change_setting(-1 if event.key == pygame.K_LEFT else 1)
                    elif event.key == pygame.K_l:
                        save_setting("led_effect", not config["led_effect"])
                    elif event.key == pygame.K_g:
                        save_setting("guest_mode", not config.get("guest_mode", False))
                    elif event.key == pygame.K_r:
                        try:
                            connection.request("calendar/refresh", {})
                        except Exception:
                            error = "REFRESH NOT QUEUED / CONNECTION LOST"
            now = time.monotonic()
            if now - last_fetch >= 2:
                try:
                    status, error = connection.status(), None
                    config = dict(status["config"])
                    backend_available = True
                    if guest_hide_latch:
                        if config.get("guest_mode") is True:
                            guest_hide_latch = False
                        else:
                            config["guest_mode"] = True
                except Exception:
                    backend_available = False
                    error = "BACKEND OFFLINE / PRIVATE CARDS HIDDEN"
                last_fetch = now
            wall_now = datetime.now(timezone.utc)
            current_status = fresh_household_status(status, config, wall_now, backend_available=backend_available)
            entries = build_rotation(current_status, config, wall_now)
            entry = cursor.tick(entries, now, automatic)
            state = entry_render_state(current_status, config, entry, wall_now)
            # Entries have already passed per-frame presence/privacy gates.
            # Priority repeats and individual trip pages are real deck slots.
            state["deck_count"], state["deck_index"] = len(entries), cursor.index
            state["defer_display_overlays"] = True
            frame = render_frame(state, config)
            if is_sleeping(config) and not settings_open and not help_open:
                frame = Image.new("RGB", frame.size)
            if settings_open or help_open:
                draw = ImageDraw.Draw(frame)
                draw.rectangle((36, 28, 476, 229), fill=PANEL, outline=CYAN)
                _text(draw, (52, 41), "FLIGHT WALL / " + ("SETTINGS" if settings_open else "CONTROLS"), CYAN)
                if settings_open:
                    for i, name in enumerate(settings_rows):
                        color = AMBER if settings_row == i else WHITE
                        value = config.get(name, "fit")
                        label = ("ON" if value else "OFF") if isinstance(value, bool) else str(value)
                        _text(draw, (52, 64 + i * 17), ("> " if i == settings_row else "  ") + name.replace("_", " ") + "  " + label, color)
                    _text(draw, (52, 198), "UP/DOWN SELECT / LEFT/RIGHT CHANGE", MUTED)
                    _text(draw, (52, 213), "S CLOSE / CHANGES SAVE AUTOMATICALLY", MUTED)
                else:
                    for i, line in enumerate(["TAB NEXT VIEW / 1-5 SELECT VIEW", "SPACE PAUSE / RESUME ROTATION", "S SETTINGS / L LED EFFECT ON-OFF", "G HIDE MESSAGES & PRIVATE CARDS", "R REFRESH CALENDAR / ESC EXIT", "GAMEPAD: A NEXT / B SETTINGS", "D-PAD SETTINGS / SELECT EXIT"]):
                        _text(draw, (52, 66 + 20 * i), line, WHITE if i < 6 else MUTED)
            if error:
                draw = ImageDraw.Draw(frame)
                draw.rectangle((0, 233, 511, 255), fill=PANEL)
                _text(draw, (8, 241), error, AMBER, max_width=496)
            elif config.get("guest_mode") is True:
                draw = ImageDraw.Draw(frame)
                draw.rectangle((0, 233, 511, 255), fill=PANEL)
                _text(draw, (8, 241), "GUEST MODE / MESSAGES & PRIVATE CARDS HIDDEN", AMBER, max_width=496)
            timer_progress = timer_transition.value(now, bool(visible_timers(state, wall_now)))
            current_names = tuple(shopping_ticker_names(state))
            if current_names != ticker_names:
                ticker_names, ticker_started_at = current_names, now
            ticker_active = bool(current_names)
            ticker_progress = ticker_transition.value(now, ticker_active)
            state["shopping_ticker_offset"] = max(0, now - ticker_started_at) * 12
            frame = compose_display_overlays(frame, state, wall_now, timer_progress, ticker_progress)
            if not settings_open and not help_open and not is_sleeping(config):
                display_surface = getattr(screen, "screen", None)
                if display_surface is not None:
                    attach_nearby_map(frame, state, config, connection, display_surface.get_size(),
                                      wall_now, timer_progress, ticker_progress)
            screen.draw(frame, config)
            if frames == 0 and on_healthy:
                on_healthy()
            frames += 1
            if max_frames and frames >= max_frames:
                break
            transitioning = (0 < timer_progress < 1 or timer_progress != timer_transition.target
                             or 0 < ticker_progress < 1 or ticker_progress != ticker_transition.target)
            clock.tick((30 if screen.gl else 12) if transitioning or ticker_active else (12 if screen.gl else 4))
    finally:
        pygame.quit()
    return {"frames": frames, "shader": bool(screen.gl)}
