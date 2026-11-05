"""Pixel-exact LED composition with OpenGL and software presentation."""
from __future__ import annotations

import os
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from datetime import datetime, timezone
from functools import lru_cache
import base64
import hashlib
import io
import json
import math
from pathlib import Path
import random
import time
import urllib.request
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from .render import compose_display_overlays, display_content_bounds, render_frame, shopping_ticker_names, visible_timers
from .feeds import parse_time
from .household_policy import build_rotation, card_visible, filter_personal_items, household_context
from .card_preferences import card_enabled
from .tv_options import GROUPS, draw_options, next_value


def fresh_household_status(status, config, now=None, *, backend_available=True):
    """Recheck cached presence every frame; cached booleans are insufficient."""
    now = now or datetime.now(timezone.utc)
    backend_available = backend_available and status.get("backend_available", True) is not False
    result = dict(status)
    result["backend_available"] = bool(backend_available)
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
        result["operating_live"] = None
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
             "kids": status.get("kids", {}),
             "dinner_options": status.get("dinner_options", {}),
             "dinner_today_selected": status.get("dinner_today_selected", False),
             "timers": status.get("timers", []), "custody_outlook": status.get("custody_outlook", {}),
             "agenda_page": entry.get("agenda_page", 0),
             "shopping_list": status.get("shopping_list", {}),
             "flight_watches": status.get("flight_watches", []),
             "weather": status.get("weather", {}),
             "operating_status": status.get("operating_status", {}),
             "operating_flight": status.get("operating_flight"),
             "operating_live": status.get("operating_live"),
             "backend_available": status.get("backend_available", True),
             "trip_weather": status.get("trip_weather", {}),
             "national_radar": status.get("national_radar", {}),
             "airport_alerts": status.get("airport_alerts", {}),
             "selected_weather_airport": item if view == "trip_weather" else None,
             "trip_preview": entry.get("trip_preview", status.get("trip_preview", {})), "trip_page": entry.get("trip_page", 0),
             "personal_items": filter_personal_items(status.get("personal_items"), status, config, now),
             "selected_personal_item": item if view == "personal" else None,
             "selected_card": item if view in {"message", "inspiration", "health", "layover", "local_visit", "flight_watch"} else None,
             "selected_layover": item if view == "layover" else None,
             "setup": (item or status.get("setup") or {}) if view == "setup" else None}
    if view == "flight_brief":
        from .flight_context import build_flight_context
        state["flight_context"] = build_flight_context(status, config, now)
        state["brief_focus"] = status.get("brief_focus")
    if view in {"personal", "message", "inspiration", "health", "layover", "local_visit", "flight_watch", "flight_status", "flight_brief", "trip_summary", "trip_map", "home", "setup", "custody_outlook", "today_activities", "operating_map", "trip_weather", "national_radar", "airport_alerts", "dinner_options"}:
        state["demo"] = False
    if view == "inspiration" and image_data and card_visible(view, item, context, config, now):
        artwork = decode_artwork(image_data)
        if artwork is not None:
            state["artwork_image"] = artwork
    if view == "personal" and item and item.get("kind") == "dinner" and image_data:
        artwork = decode_artwork(image_data)
        if artwork is not None:
            state["dinner_image"] = artwork
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

    def retreat(self, entries, now):
        self.select(entries, self.index - 1, now)

    def tick(self, entries, now, automatic=True):
        self.reconcile(entries, now)
        if entries and automatic and now - self.started_at >= entries[self.index]["duration_seconds"]:
            self.advance(entries, now)
        return entries[self.index] if entries else None


class AnalogNavigation:
    """One navigation step per horizontal stick excursion, per controller."""
    def __init__(self):
        self.held = {}

    def direction(self, instance, value):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            return 0
        if abs(value) <= .3:
            self.held.pop(instance, None)
            return 0
        if instance in self.held or abs(value) < .6:
            return 0
        direction = 1 if value > 0 else -1
        self.held[instance] = direction
        return direction

    def remove(self, instance):
        self.held.pop(instance, None)


ANIMATED_VIEWS = frozenset({
    "details", "map", "schedule", "trip_summary", "trip_map", "home",
    "flight_status", "personal", "local_visit",
})
CARD_EFFECTS = ("wipe_left", "wipe_right", "wipe_up", "wipe_down")


def transition_signature(status, config, entries, backend_available, now=None):
    """Hash eligibility and card contents, retaining no private text or images.

    The signature is independent of the selected slot and moving clock/timer
    labels. Changes to settings, fresh presence, expired/changed cards or the
    eligible deck invalidate every old pixel, including same-key replacements.
    """
    def bounded(value):
        if isinstance(value, dict):
            return {key: (hashlib.sha256(item.encode()).hexdigest()
                          if key == "image_data" and isinstance(item, str) else bounded(item))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [bounded(item) for item in value]
        return value
    safe = {key: value for key, value in status.items() if key not in {"timers", "shopping_list", "now"}}
    try:
        now = now or datetime.now(timezone.utc)
        from .render import _tz
        local = now.astimezone(_tz(config))
        live = status.get("live")
        live_fresh = False
        if isinstance(live, dict):
            try:
                age = (now - parse_time(live.get("observed_at"))).total_seconds()
                live_fresh = 0 <= age <= 300
            except (ValueError, TypeError, OverflowError):
                pass
        # Weather/nearby/watch views never enter the frame cache. A live route
        # marker and dinner's snack phase still have exact per-frame cutoffs.
        temporal = (local.date().isoformat(), local.hour >= 17, live_fresh)
        payload = json.dumps(bounded([safe, config, entries, bool(backend_available), temporal]), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(payload.encode()).digest() if len(payload) <= 8_000_000 else None
    except (ValueError, TypeError, RecursionError):
        return None


class CardTransition:
    """Short public/household card transitions with an explicit privacy guard.

    Messages, artwork, health, hotel, weather and nearby/watch cards never enter
    this cache. Their eligibility/freshness is handled by the current renderer.
    Every guard/eligibility change immediately forgets all prior image objects.
    Live timer/ticker layers are composed by the GUI after this class returns.
    """
    def __init__(self, duration=.25, chooser=None):
        self.duration = duration if isinstance(duration, (int, float)) and .05 <= duration <= 1 else .25
        self._choose = chooser or random.SystemRandom().choice
        self._seed = 0
        self.cancel()

    @property
    def active(self):
        return self._previous is not None

    def cancel(self):
        self._previous = self._last = None
        self._guard = self._key = self._effect = self._started = None

    @staticmethod
    def _copy(frame):
        image = frame.copy()
        image.info.clear()
        return image

    def render(self, frame, key, guard, now, *, view, enabled=True):
        from .transitions import compose
        if (not enabled or view not in ANIMATED_VIEWS or guard is None
                or isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now)):
            self.cancel()
            return frame
        if guard != self._guard:
            self.cancel()
            self._guard, self._key, self._last = guard, key, self._copy(frame)
            return frame
        if key != self._key:
            self._previous = self._last
            self._key, self._started = key, now
            choices = tuple(effect for effect in CARD_EFFECTS if effect != self._effect)
            self._effect, self._seed = self._choose(choices), self._seed + 1
        if self._previous is not None:
            elapsed = now - self._started
            if elapsed < 0:
                self.cancel()
                return frame
            duration = 2.0 if self._effect == "split_flap" else self.duration
            if elapsed >= duration:
                self._previous = None
            else:
                frame = compose(self._previous, frame, elapsed / duration, self._effect, seed=self._seed)
        self._last = self._copy(frame)
        return frame


def active_personal_items(status, config, now=None):
    """Filter again between polls so a card disappears at its exact end time."""
    if not config.get("personal_enabled", True):
        return []
    now = now or datetime.now(timezone.utc)
    from .personal import personal_card_visible
    result = []
    for item in status.get("personal_items", []):
        try:
            if item.get("cancelled") or item.get("kind") not in {"appointment", "dinner"}:
                continue
            if card_enabled("personal", config, item) and personal_card_visible(item, config, now):
                result.append(item)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return result


def rotation_views(status, config, now=None):
    views = ["nearby", "schedule", "map", "details"] if config.get("nearby_enabled") and not status.get("on_duty") else ["details", "map", "schedule"]
    if active_personal_items(status, config, now):
        views.append("personal")
    return [view for view in views if card_enabled(view, config)]


def is_sleeping(config, now=None):
    if not config.get("sleep_enabled"):
        return False
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(config.get("timezone", "UTC")))
    current = now.strftime("%H:%M")
    start, end = config["sleep_start"], config["sleep_end"]
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


def _led_axis(cells, pixels, gap):
    pitch = pixels / cells
    if not gap or pitch < 2:
        return b"\xff" * pixels
    space = min(int(pitch) - 1, max(1, math.floor(pitch * gap + .5)))
    before, after = (space + 1) // 2, space // 2
    mask = bytearray(pixels)
    start = 0
    for index in range(1, cells + 1):
        # The nearest-neighbor boundary is ceil(index * pitch - 0.5).
        end = (2 * index * pixels + cells - 1) // (2 * cells)
        lit = end - start - space
        mask[start + before:end - after] = b"\xff" * lit
        start = end
    return bytes(mask)


@lru_cache(maxsize=4)
def _led_mask(frame_size, size, gap):
    columns = _led_axis(frame_size[0], size[0], gap)
    dark = bytes(size[0])
    rows = _led_axis(frame_size[1], size[1], gap)
    return Image.frombytes("L", size, b"".join(columns if row else dark for row in rows))


def led_image(frame, config, scale=4):
    """Export the same logical pixel layout with a portable LED treatment."""
    scale = max(1, min(int(scale), 8))
    return _present_frame(frame, config, (frame.width * scale, frame.height * scale))


def _render_led(frame, config, size):
    rgb = frame if frame.mode == "RGB" else frame.convert("RGB")
    out = rgb.resize(size, Image.Resampling.NEAREST)
    pitch = min(size[0] / frame.width, size[1] / frame.height)
    if config.get("led_effect", True) and pitch >= 2:
        gap = max(0, min(float(config.get("dot_gap", .22)), .65))
        if gap:
            mask = _led_mask(frame.size, size, round(gap, 4))
            out = Image.composite(out, Image.new("RGB", size), mask)
    return out


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

    def local_radar_image(self, expected_sha256):
        """Read one fixed local public-radar cache, never a server-supplied path."""
        import re
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return None
        try:
            path = self.data_dir / "national-radar.png"
            if path.is_symlink():
                return None
            with path.open("rb") as stream:
                raw = stream.read(4_000_001)
            if len(raw) > 4_000_000 or hashlib.sha256(raw).hexdigest() != expected_sha256:
                return None
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != "PNG" or not 1 <= source.width <= 2048 or not 1 <= source.height <= 1024:
                    return None
                image = source.convert("RGB")
                image.info.clear()
                return image
        except (OSError, ValueError, TypeError, Image.DecompressionBombError):
            return None

    def local_radar_frame(self, expected_sha256, elapsed_seconds):
        """Read a bounded fixed APNG generation; never a remotely supplied path.

        Decoded public frames are cached only for this file generation. Atomic
        cache replacement invalidates the stat identity before another frame.
        """
        import re
        from .radar import MAX_ANIMATION_BYTES, decode_cached, frame_index
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return None
        try:
            path = self.data_dir / "national-radar-loop.png"
            if path.is_symlink():
                return None
            def identity():
                stat = path.stat()
                return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_size
            current = identity()
            if not 0 < current[-1] <= MAX_ANIMATION_BYTES:
                return None
            cached = getattr(self, "_radar_animation_cache", None)
            if cached is None or cached[:2] != (expected_sha256, current):
                with path.open("rb") as stream:
                    raw = stream.read(MAX_ANIMATION_BYTES + 1)
                if (len(raw) > MAX_ANIMATION_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256
                        or identity() != current or path.is_symlink()):
                    self._radar_animation_cache = None
                    return None
                frames, durations = decode_cached(raw)
                metadata = {"sha256": expected_sha256, "frame_count": len(frames), "durations_ms": durations,
                            "total_duration_ms": sum(durations), "observed_at": [None] * len(frames)}
                cached = self._radar_animation_cache = (expected_sha256, current, frames, metadata)
            index = frame_index(cached[3], elapsed_seconds)
            image = cached[2][index].copy()
            image.info.clear()
            return image, index, len(cached[2])
        except (OSError, ValueError, TypeError, KeyError, OverflowError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            self._radar_animation_cache = None
            return None


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
    if (state.get("view") != "nearby" or not card_enabled("nearby", config)
            or config.get("demo") is True or state.get("demo", False) is not False
            or config.get("nearby_enabled") is not True):
        return frame
    left, top, width, height = display_content_bounds(state, now, timer_progress, ticker_progress)
    box = (left + 14 * width / 512, top + 56 * height / 256,
           297 * width / 512, 149 * height / 256)
    image = None
    if config.get("nearby_detailed_map", True) is True:
        try:
            point = connection.local_map_config()
            from .nearby_map import render_map
            _, _, dest_w, dest_h = display_viewport(screen_size, frame.size, config.get("display_scale_mode", "fit"))
            # Reuse the same geographic background during strip animation.
            # The presentation layer scales it into the current content inset.
            size = (max(256, min(2048, round(297 * dest_w / 512))),
                    max(128, min(1024, round(149 * dest_h / 256))))
            image = render_map({**config, **point}, size, rows=state.get("nearby", []), now=now)
        except (OSError, ValueError, TypeError, AttributeError, ImportError):
            pass
    if not isinstance(image, Image.Image):
        # Preserve the clear schematic while geography or its local point is unavailable.
        if "board_layer" in frame.info:
            image = frame.info["board_layer"][1].crop((14, 56, 311, 205))
        else:
            x, y, w, h = box
            image = frame.crop((round(x), round(y), round(x + w), round(y + h)))
    frame.info["geographic_patch"] = (box, image)
    return frame


def attach_flight_context(frame, state, config, connection, screen_size, now,
                          timer_progress=None, ticker_progress=None):
    """Present public geography/radar after LEDs, using the same live insets."""
    if (state.get("view") not in {"operating_map", "national_radar"}
            or not card_enabled(state.get("view"), config)):
        return frame
    if (state.get("backend_available", True) is not True or config.get("demo") is True
            or state.get("demo", False) is not False):
        return frame
    try:
        left, top, width, height = display_content_bounds(state, now, timer_progress, ticker_progress)
        radar = state["view"] == "national_radar"
        box = (left + 14 * width / 512, top + 56 * height / 256,
               (484 if radar else 297) * width / 512, (169 if radar else 149) * height / 256)
        _, _, dest_w, dest_h = display_viewport(screen_size, frame.size, config.get("display_scale_mode", "fit"))
        size = (max(256, min(2048, round(box[2] * dest_w / 512))),
                max(128, min(1024, round(box[3] * dest_h / 256))))
        if radar:
            from .flight_context_views import radar_ready
            meta = state.get("national_radar", {})
            image, index, count = None, None, None
            if radar_ready(state, config, now):
                animation = meta.get("animation")
                reader = getattr(connection, "local_radar_frame", None)
                if isinstance(animation, dict) and callable(reader):
                    value = reader(animation.get("sha256"), time.monotonic())
                    if (isinstance(value, tuple) and len(value) == 3 and isinstance(value[0], Image.Image)
                            and type(value[1]) is int and value[2] == animation.get("frame_count") and 0 <= value[1] < value[2]):
                        image, index, count = value
                if image is None:
                    image = connection.local_radar_image(meta.get("image_sha256"))
            if image is not None:
                from PIL import ImageOps
                image = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
                panel = Image.new("RGB", size, (5, 15, 21))
                panel.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
                from .render import MUTED, WHITE, _text
                draw = ImageDraw.Draw(panel)
                scale = max(1, min(2, size[1] // 180))
                _text(draw, (8, 8), f"OBS {index + 1}/{count}" if index is not None and count > 1 else "STATIC NOAA", WHITE, scale)
                _text(draw, (8, 8 + 12 * scale), "UTC IN IMAGE", MUTED, scale)
                times = (meta.get("animation") or {}).get("observed_at", [])
                observed = times[index if index is not None else -1] if isinstance(times, list) and times else None
                if observed is not None:
                    from .radar import stamp
                    from .render import _tz
                    from .time_format import format_time
                    local = stamp(observed).astimezone(_tz(config))
                    _text(draw, (8, 8 + 28 * scale), "OBS " + local.strftime("%m/%d"), MUTED, scale)
                    _text(draw, (8, 8 + 40 * scale), format_time(local, include_zone=True), WHITE, scale)
                image = panel
        else:
            from .operating_map import render_map
            image = render_map(state.get("operating_flight"), state.get("operating_status"),
                               state.get("operating_live"), size, now,
                               enabled=config.get("flightaware_enabled") is True,
                               demo=False, backend_available=True)
        if isinstance(image, Image.Image):
            frame.info["geographic_patch"] = (box, image)
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        pass
    return frame


def geographic_patch(frame, viewport):
    """Translate a trusted local image patch into display pixels, with bounds."""
    return _presentation_layer(frame, viewport, "geographic_patch")


def _presentation_layer(frame, viewport, name):
    try:
        box, image = frame.info[name]
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


def _present_frame(frame, config, size):
    """Use the same direct pixel path for the card, ticker and timer."""
    viewport = (0, 0, *size)
    board = _presentation_layer(frame, viewport, "board_layer")
    if board:
        image, (x, y, width, height) = board
        _, top, _, board_height = frame.info["board_layer"][0]
        bottom = top + board_height
        output = Image.new("RGB", size, frame.getpixel((0, top)))
        # Only present the strip pixels. The card covers most of the output
        # and needs no second render of the empty area behind it.
        if top and y:
            timer = frame.crop((0, 0, frame.width, top))
            output.paste(_render_led(timer, config, (size[0], y)), (0, 0))
        if bottom < frame.height and y + height < size[1]:
            ticker = frame.crop((0, bottom, frame.width, frame.height))
            output.paste(_render_led(ticker, config, (size[0], size[1] - y - height)), (0, y + height))
        output.paste(_render_led(image, config, (width, height)), (x, y))
    else:
        output = _render_led(frame, config, size)
    patch = geographic_patch(frame, viewport)
    if patch:
        image, (x, y, width, height) = patch
        output.paste(image.resize((width, height), Image.Resampling.NEAREST), (x, y))
    brightness = config.get("brightness", .7)
    return output if brightness == 1 else output.point([int(value * brightness) for value in range(256)] * 3)


class DisplayScreen:
    def __init__(self, pygame, size, fullscreen):
        self.pygame = pygame
        self.gl = None
        flags = pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE
        try:
            from OpenGL import GL
            pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 2)
            pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 1)
            self.screen = pygame.display.set_mode(size, flags | pygame.OPENGL | pygame.DOUBLEBUF)
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
        output = _present_frame(frame, config, (dest_w, dest_h)) if dest_w and dest_h else None
        if self.gl:
            GL = self.gl
            GL.glClearColor(0, 0, 0, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            if output is not None:
                GL.glViewport(x, height - y - dest_h, dest_w, dest_h)
                GL.glEnable(GL.GL_TEXTURE_2D)
                GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture)
                GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
                # Pixels already match the viewport. OpenGL only copies them.
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB, dest_w, dest_h, 0,
                                GL.GL_RGB, GL.GL_UNSIGNED_BYTE, output.tobytes())
                GL.glBegin(GL.GL_QUADS)
                for uv, point in (((0, 1), (-1, -1)), ((1, 1), (1, -1)), ((1, 0), (1, 1)), ((0, 0), (-1, 1))):
                    GL.glTexCoord2f(*uv)
                    GL.glVertex2f(*point)
                GL.glEnd()
                GL.glDisable(GL.GL_TEXTURE_2D)
        else:
            self.screen.fill((0, 0, 0))
            if output is not None:
                surface = pygame.image.frombytes(output.tobytes(), output.size, "RGB")
                self.screen.blit(surface, (x, y))
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
    screen = DisplayScreen(pygame, (1280, 720) if windowed else (0, 0), not windowed)
    status = connection.status()
    config = dict(status["config"])
    initial_status = fresh_household_status(status, config, datetime.now(timezone.utc))
    entries = build_rotation(initial_status, config, datetime.now(timezone.utc))
    cursor = RotationCursor(entries, time.monotonic())
    card_transition = CardTransition()
    analog_navigation = AnalogNavigation()
    automatic, settings_open, settings_row, help_open = True, False, 0, False
    settings_group = 0
    settings_rows = GROUPS[settings_group][1]
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
        value = next_value(config, name, direction)
        save_setting(name, value)

    def change_group(direction=1):
        nonlocal settings_group, settings_rows, settings_row
        settings_group = (settings_group + direction) % len(GROUPS)
        settings_rows, settings_row = GROUPS[settings_group][1], 0

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
                    analog_navigation.remove(event.instance_id)
                    if joystick:
                        joystick.quit()
                elif event.type == pygame.JOYBUTTONDOWN:
                    if event.button in {6, 8}:
                        running = False
                    elif event.button in {1, 2}:
                        settings_open = not settings_open
                    elif settings_open:
                        change_group(-1 if event.button == 4 else 1)
                    else:
                        cursor.advance(entries, time.monotonic())
                elif event.type == pygame.JOYHATMOTION:
                    if settings_open:
                        if event.value[1]:
                            settings_row = (settings_row - event.value[1]) % len(settings_rows)
                        if event.value[0]:
                            change_setting(event.value[0])
                    elif event.value[0]:
                        (cursor.advance if event.value[0] > 0 else cursor.retreat)(entries, time.monotonic())
                elif event.type == pygame.JOYAXISMOTION and event.axis == 0:
                    direction = analog_navigation.direction(getattr(event, "instance_id", getattr(event, "joy", 0)), event.value)
                    if direction and not settings_open:
                        (cursor.advance if direction > 0 else cursor.retreat)(entries, time.monotonic())
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
                        if settings_open:
                            change_group(-1 if getattr(event, "mod", 0) & pygame.KMOD_SHIFT else 1)
                        else:
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
                    elif event.key in {pygame.K_LEFT, pygame.K_RIGHT}:
                        (cursor.advance if event.key == pygame.K_RIGHT else cursor.retreat)(entries, time.monotonic())
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
            sleeping = is_sleeping(config, wall_now)
            if sleeping and not settings_open and not help_open:
                frame = Image.new("RGB", frame.size)
            animations_allowed = not (settings_open or help_open or sleeping or error
                                      or config.get("guest_mode") is True or not backend_available)
            guard = (transition_signature(current_status, config, entries, backend_available, wall_now)
                     if animations_allowed and state["view"] in ANIMATED_VIEWS else None)
            frame = card_transition.render(frame, entry["key"], guard, now, view=state["view"], enabled=animations_allowed)
            if settings_open or help_open:
                draw = ImageDraw.Draw(frame)
                draw.rectangle((36, 28, 476, 229), fill=PANEL, outline=CYAN)
                _text(draw, (52, 41), "FLIGHT WALL / " + ("SETTINGS" if settings_open else "CONTROLS"), CYAN)
                if settings_open:
                    draw_options(draw, config, settings_group, settings_row)
                else:
                    for i, line in enumerate(["LEFT/RIGHT BACK/NEXT / TAB NEXT", "SPACE PAUSE / 1-5 SELECT CARD", "S SETTINGS / L LED EFFECT ON-OFF", "G HIDE MESSAGES & PRIVATE CARDS", "R REFRESH CALENDAR / ESC EXIT", "GAMEPAD: D-PAD/STICK BACK/NEXT", "A NEXT / B SETTINGS / SELECT EXIT"]):
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
            frame = compose_display_overlays(frame, state, wall_now, timer_progress, ticker_progress, presentation=True)
            if not settings_open and not help_open and not sleeping and not card_transition.active:
                display_surface = getattr(screen, "screen", None)
                if display_surface is not None:
                    attach_nearby_map(frame, state, config, connection, display_surface.get_size(),
                                      wall_now, timer_progress, ticker_progress)
                    attach_flight_context(frame, state, config, connection, display_surface.get_size(),
                                          wall_now, timer_progress, ticker_progress)
            screen.draw(frame, config)
            if frames == 0 and on_healthy:
                on_healthy()
            frames += 1
            if max_frames and frames >= max_frames:
                break
            transitioning = (0 < timer_progress < 1 or timer_progress != timer_transition.target
                             or 0 < ticker_progress < 1 or ticker_progress != ticker_transition.target
                             or card_transition.active)
            animated_radar = (state.get("view") == "national_radar"
                              and card_enabled("national_radar", config)
                              and isinstance(state.get("national_radar", {}).get("animation"), dict))
            clock.tick((30 if screen.gl else 12) if transitioning or ticker_active else
                       (12 if screen.gl or animated_radar else 4))
    finally:
        pygame.quit()
    return {"frames": frames, "opengl": bool(screen.gl), "shader": False}
