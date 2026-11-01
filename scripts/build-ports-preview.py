#!/usr/bin/env python3
"""Render the silent 15-second Ports preview from fixed, fictional app states.

Run with the normal source dependencies and a developer FFmpeg executable:
  python scripts/build-ports-preview.py --ffmpeg /path/to/ffmpeg
No application DATA, credentials, calendars, phone positions or network read.
All time is derived from integer frames; an out-of-order frame is identical.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flightwall.display import led_image
from flightwall.outlook import trip_preview
from flightwall.render import AMBER, BG, LINE, MUTED, _text, compose_display_overlays, demo_snapshot, render_frame
from flightwall.transitions import compose

FPS, SECONDS = 30, 15
FRAMES = FPS * SECONDS
BASE = datetime(2026, 6, 15, 20, 30, tzinfo=timezone.utc)
STYLE = {"name": "Flight Wall crisp LED", "font": "app original 5x7 pixel alphabet",
         "logical_size": [512, 256], "output_size": [1024, 512], "glow": 0,
         "dot_gap": .1, "brightness": 1, "audio": "none",
         "exclusions": ["no real household data", "no home pin", "no paid/network feeds", "no glow/strobe"]}
SCENES = ((0, 45, "brand"), (45, 135, "details"), (135, 225, "trip_map"),
          (225, 315, "schedule"), (315, 450, "weather"))
CONFIG = {"timezone": "America/Chicago", "demo": False, "show_deadheads": True,
          "weather_enabled": True, "flight_preview_days": 7,
          "led_effect": True, "glow": 0, "dot_gap": .1, "brightness": 1}


def fictional_weather():
    day = {"date": "2026-06-15", "high": 82, "low": 70, "temperature_unit": "F",
           "summary": "Partly cloudy", "complete": True, "stale": False}
    return {"status": "ready", "source": "Fictional demo forecast",
            "hourly_fetched_at": BASE.isoformat(), "daily_fetched_at": BASE.isoformat(),
            "hourly_updated_at": BASE.isoformat(), "daily_updated_at": BASE.isoformat(),
            "today": day, "tomorrow": day | {"date": "2026-06-16", "high": 79, "low": 69, "summary": "Rain showers"},
            "overnight": {"start": "2026-06-15T18:00:00-05:00", "end": "2026-06-16T08:00:00-05:00",
                          "coverage_start": "2026-06-15T23:00:00+00:00", "max_precipitation_probability": 60,
                          "coverage_complete": True, "covered_hours": 14, "expected_hours": 14,
                          "rain_mentioned": True, "stale": False, "risk": "possible"}}


def state_for(view, frame):
    now = BASE + timedelta(seconds=frame / FPS)
    snapshot = demo_snapshot(BASE.isoformat())
    # Reuse only the app's generated sample legs, never the owner's calendars.
    preview = trip_preview(snapshot | {"demo": False}, CONFIG, BASE)
    return {**snapshot, "view": view, "now": now.isoformat(), "demo": view != "weather",
            "trip_preview": preview, "trip_page": 0, "weather": fictional_weather(),
            "defer_display_overlays": True}


def demo_mark(image):
    draw = ImageDraw.Draw(image)
    draw.rectangle((348, 9, 380, 22), fill=BG, outline=AMBER)
    _text(draw, (353, 12), "DEMO", AMBER)
    return image


def scene_frame(view, frame, logo):
    if view == "brand":
        image = Image.new("RGB", (512, 256), BG)
        image.paste(logo.resize((240, 240), Image.Resampling.LANCZOS), (136, 8))
        draw = ImageDraw.Draw(image)
        _text(draw, (15, 16), "DEMO", AMBER, 2)
        _text(draw, (15, 235), "FICTIONAL PREVIEW", MUTED)
        return image
    state = state_for(view, frame)
    image = render_frame(state, CONFIG)
    demo_mark(image)
    if view == "weather":
        # The app draws its normal weather card from a synthetic response.
        # Explicitly replace provider clocks: this is not an observed forecast.
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 228, 511, 255), fill=BG)
        draw.line((14, 228, 498, 228), fill=LINE)
        _text(draw, (15, 235), "SAMPLE WEATHER / FICTIONAL FORECAST", AMBER)
        _text(draw, (15, 247), "OFFLINE PREVIEW / NOT A LIVE PROVIDER RESULT", MUTED)
    return image


def frame_at(frame, logo):
    if type(frame) is not int or not 0 <= frame < FRAMES:
        raise ValueError("Preview frame is outside its fixed timeline")
    index = next(i for i, (start, end, _) in enumerate(SCENES) if start <= frame < end)
    start, _, view = SCENES[index]
    image = scene_frame(view, frame, logo)
    # One short native reveal between schedule views; weather starts cleanly.
    if index in {2, 3} and frame - start < 8:
        old = scene_frame(SCENES[index - 1][2], start - 1, logo)
        image = compose(old, image, (frame - start + 1) / 8, "wipe_left" if index == 2 else "barn_vertical", seed=11)
    if frame >= 360:
        state = state_for(view, frame)
        state["timers"] = [{"id": "sample-pasta", "label": "PASTA", "ends_at": (BASE + timedelta(seconds=72)).isoformat()},
                           {"id": "sample-laundry", "label": "LAUNDRY", "ends_at": (BASE + timedelta(seconds=132)).isoformat()}]
        progress = min(1, (frame - 360) / 12)
        progress = progress * progress * (3 - 2 * progress)
        image = compose_display_overlays(image, state, BASE + timedelta(seconds=frame / FPS), timer_progress=progress)
    return led_image(image, CONFIG, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "assets" / "ports")
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error("Provide a developer FFmpeg executable with --ffmpeg; Batocera does not need this tool")
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    with Image.open(ROOT / "assets/ports/flightwall-logo.png") as source:
        logo = source.convert("RGB")
    target = out / "flightwall-preview.mp4"
    command = [str(args.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-vcodec", "rawvideo", "-pixel_format", "rgb24", "-video_size", "1024x512",
               "-framerate", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-profile:v", "baseline", "-level:v", "3.1", "-pix_fmt", "yuv420p", "-threads", "1",
               "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-movflags", "+faststart", str(target)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for index in range(FRAMES):
            process.stdin.write(frame_at(index, logo).tobytes())
        process.stdin.close()
        error = process.stderr.read().decode("utf-8", "replace")
        if process.wait() != 0:
            raise RuntimeError("Preview encoding failed: " + error[:2000])
    except BaseException:
        process.kill()
        process.wait()
        raise
    frame_at(90, logo).save(out / "flightwall-image.png")
    result = {"style": STYLE, "fps": FPS, "duration_seconds": SECONDS, "frames": FRAMES,
              "timeline": [{"start_frame": start, "end_frame": end, "view": view} for start, end, view in SCENES],
              "timer_reveal_frame": 360, "input": "fixed app-generated fictional fixtures only",
              "video": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
              "codec": "H.264 baseline", "pixel_format": "yuv420p", "audio_streams": 0}
    (out / "preview-timeline.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
