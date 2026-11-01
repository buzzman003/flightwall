"""Bounded retro card reveals; pure pixels with no retained frames or I/O.

The GUI must cancel a transition immediately when privacy eligibility changes.
This module cannot determine eligibility: callers supply the freshly rendered
current card, keep timers/tickers outside this effect, and own old-frame life.
Every intermediate pixel comes from a supplied image; there is no flash frame,
global brightness modulation, interpolation, network, filesystem or wall clock.
"""
from __future__ import annotations

import math
import random

from PIL import Image, ImageDraw


EFFECTS = (
    "wipe_left", "wipe_right", "wipe_up", "wipe_down",
    "diagonal_tl", "diagonal_tr", "diagonal_bl", "diagonal_br",
    "blinds_horizontal", "blinds_vertical", "checker",
    "dissolve_fine", "dissolve_blocks", "iris", "diamond",
    "barn_horizontal", "barn_vertical",
    "slide_left", "slide_right", "slide_up", "slide_down",
    "tiles_diagonal", "scanlines_horizontal", "scanlines_vertical",
)
MAX_SIZE = (512, 256)


def _copy(image):
    result = image.copy()
    # Presentation layers/metadata belong to the current GUI frame. An old
    # high-resolution map patch must not bypass this composition via .info.
    result.info.clear()
    return result


def _slide(previous, current, progress, direction):
    width, height = current.size
    result = Image.new("RGB", current.size)
    if direction in {"left", "right"}:
        distance = round(width * progress)
        if direction == "left":
            result.paste(previous.crop((distance, 0, width, height)), (0, 0))
            result.paste(current.crop((0, 0, distance, height)), (width - distance, 0))
        else:
            result.paste(previous.crop((0, 0, width - distance, height)), (distance, 0))
            result.paste(current.crop((width - distance, 0, width, height)), (0, 0))
    else:
        distance = round(height * progress)
        if direction == "up":
            result.paste(previous.crop((0, distance, width, height)), (0, 0))
            result.paste(current.crop((0, 0, width, distance)), (0, height - distance))
        else:
            result.paste(previous.crop((0, 0, width, height - distance)), (0, distance))
            result.paste(current.crop((0, height - distance, width, height)), (0, 0))
    return result


def _mask(size, progress, effect, seed):
    width, height = size
    mask = Image.new("L", size)
    draw = ImageDraw.Draw(mask)
    if effect.startswith("wipe_"):
        direction = effect.removeprefix("wipe_")
        if direction == "left":
            start = width - round(width * progress)
            if start < width:
                draw.rectangle((start, 0, width - 1, height - 1), fill=255)
        elif direction == "right":
            end = round(width * progress)
            if end:
                draw.rectangle((0, 0, end - 1, height - 1), fill=255)
        elif direction == "up":
            start = height - round(height * progress)
            if start < height:
                draw.rectangle((0, start, width - 1, height - 1), fill=255)
        else:
            end = round(height * progress)
            if end:
                draw.rectangle((0, 0, width - 1, end - 1), fill=255)
    elif effect.startswith("diagonal_"):
        draw.polygon(((0, 0), (2 * width * progress, 0), (0, 2 * height * progress)), fill=255)
        corner = effect.removeprefix("diagonal_")
        if corner in {"tr", "br"}:
            mask = mask.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if corner in {"bl", "br"}:
            mask = mask.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    elif effect.startswith("blinds_"):
        band = 16
        if effect == "blinds_horizontal":
            for y in range(0, height, band):
                amount = round(min(band, height - y) * progress)
                if amount:
                    draw.rectangle((0, y, width - 1, y + amount - 1), fill=255)
        else:
            for x in range(0, width, band):
                amount = round(min(band, width - x) * progress)
                if amount:
                    draw.rectangle((x, 0, x + amount - 1, height - 1), fill=255)
    elif effect == "checker":
        tile = 16
        for row, y in enumerate(range(0, height, tile)):
            for column, x in enumerate(range(0, width, tile)):
                phase = max(0.0, min(1.0, progress * 2 - ((row + column) % 2)))
                amount = round(min(tile, height - y) * phase)
                if amount:
                    draw.rectangle((x, y, min(width - 1, x + tile - 1), y + amount - 1), fill=255)
    elif effect.startswith("dissolve_"):
        rng = random.Random(seed)
        noise_size = size if effect == "dissolve_fine" else (math.ceil(width / 16), math.ceil(height / 16))
        noise = Image.frombytes("L", noise_size, rng.randbytes(noise_size[0] * noise_size[1]))
        mask = noise.point(lambda value: 255 if value < round(256 * progress) else 0)
        if noise_size != size:
            mask = mask.resize(size, Image.Resampling.NEAREST)
    elif effect == "iris":
        rx, ry = width * progress / math.sqrt(2), height * progress / math.sqrt(2)
        draw.ellipse((width / 2 - rx, height / 2 - ry, width / 2 + rx, height / 2 + ry), fill=255)
    elif effect == "diamond":
        cx, cy = width / 2, height / 2
        rx, ry = width * progress, height * progress
        draw.polygon(((cx - rx, cy), (cx, cy - ry), (cx + rx, cy), (cx, cy + ry)), fill=255)
    elif effect.startswith("barn_"):
        if effect == "barn_horizontal":
            amount = round(width * progress)
            if amount:
                left = (width - amount) // 2
                draw.rectangle((left, 0, left + amount - 1, height - 1), fill=255)
        else:
            amount = round(height * progress)
            if amount:
                top = (height - amount) // 2
                draw.rectangle((0, top, width - 1, top + amount - 1), fill=255)
    elif effect == "tiles_diagonal":
        tile = 16
        rng = random.Random(seed)
        tiles = [(row + column, rng.random(), x, y)
                 for row, y in enumerate(range(0, height, tile))
                 for column, x in enumerate(range(0, width, tile))]
        tiles.sort()
        for _, _, x, y in tiles[:round(len(tiles) * progress)]:
            draw.rectangle((x, y, min(width - 1, x + tile - 1), min(height - 1, y + tile - 1)), fill=255)
    elif effect.startswith("scanlines_"):
        horizontal = effect == "scanlines_horizontal"
        length = height if horizontal else width
        order = tuple(range(0, length, 2)) + tuple(range(1, length, 2))
        for coordinate in order[:round(length * progress)]:
            if horizontal:
                draw.line((0, coordinate, width - 1, coordinate), fill=255)
            else:
                draw.line((coordinate, 0, coordinate, height - 1), fill=255)
    return mask


def compose(previous, current, progress, effect, *, seed=0):
    """Compose one frame; no source image, metadata or random state is retained.

    Input is an RGB logical board no larger than 512x256. Endpoints reproduce
    exact source pixels. Bad old frames, effect names or progress values fail
    closed to a fresh current copy. If current itself is invalid, raise rather
    than allocating an unbounded image or guessing a replacement.
    """
    if (not isinstance(current, Image.Image) or current.mode != "RGB"
            or not 1 <= current.width <= MAX_SIZE[0] or not 1 <= current.height <= MAX_SIZE[1]):
        raise ValueError("Transitions require a bounded RGB current board")
    if (not isinstance(previous, Image.Image) or previous.mode != "RGB" or previous.size != current.size
            or effect not in EFFECTS or isinstance(progress, bool)
            or not isinstance(progress, (int, float)) or not math.isfinite(progress)):
        return _copy(current)
    if progress <= 0:
        return _copy(previous)
    if progress >= 1:
        return _copy(current)
    seed = (seed & ((1 << 64) - 1)) if type(seed) is int else 0
    if effect.startswith("slide_"):
        return _slide(previous, current, progress, effect.removeprefix("slide_"))
    result = Image.composite(current, previous, _mask(current.size, progress, effect, seed))
    result.info.clear()
    return result
