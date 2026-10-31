"""Pixel-perfect flight board. No network, account data, or display drivers here.

All views are composed at the LED wall's native 512 x 256 resolution.  The
optional final resize is nearest-neighbour, so the same layout works on a TV.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import math
from pathlib import Path
import textwrap
import unicodedata
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw

from .timers import COMPLETED_RETENTION

BASE_SIZE = (512, 256)
TIMER_STRIP_HEIGHT = 36
SHOPPING_TICKER_HEIGHT = 18
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "maps"
BG = (3, 9, 13)
PANEL = (7, 17, 23)
LINE = (25, 53, 62)
WHITE = (233, 241, 230)
AMBER = (255, 181, 67)
CYAN = (83, 214, 222)
MUTED = (117, 151, 158)
GREEN = (120, 222, 155)
TRIP_COLORS = (CYAN, AMBER, GREEN, (156, 177, 255), (241, 153, 204))
TRIP_PAGE_SIZE = 3

# An original, fixed-width 5 x 7 alphabet: an actual pixel font, with no system
# font lookup and no antialiasing. Slashes separate the seven glyph rows.
_GLYPHS = {
    " ":"00000/00000/00000/00000/00000/00000/00000",
    "A":"01110/10001/10001/11111/10001/10001/10001",
    "B":"11110/10001/10001/11110/10001/10001/11110",
    "C":"01111/10000/10000/10000/10000/10000/01111",
    "D":"11110/10001/10001/10001/10001/10001/11110",
    "E":"11111/10000/10000/11110/10000/10000/11111",
    "F":"11111/10000/10000/11110/10000/10000/10000",
    "G":"01111/10000/10000/10111/10001/10001/01111",
    "H":"10001/10001/10001/11111/10001/10001/10001",
    "I":"11111/00100/00100/00100/00100/00100/11111",
    "J":"00111/00010/00010/00010/10010/10010/01100",
    "K":"10001/10010/10100/11000/10100/10010/10001",
    "L":"10000/10000/10000/10000/10000/10000/11111",
    "M":"10001/11011/10101/10101/10001/10001/10001",
    "N":"10001/11001/10101/10011/10001/10001/10001",
    "O":"01110/10001/10001/10001/10001/10001/01110",
    "P":"11110/10001/10001/11110/10000/10000/10000",
    "Q":"01110/10001/10001/10001/10101/10010/01101",
    "R":"11110/10001/10001/11110/10100/10010/10001",
    "S":"01111/10000/10000/01110/00001/00001/11110",
    "T":"11111/00100/00100/00100/00100/00100/00100",
    "U":"10001/10001/10001/10001/10001/10001/01110",
    "V":"10001/10001/10001/10001/10001/01010/00100",
    "W":"10001/10001/10001/10101/10101/10101/01010",
    "X":"10001/10001/01010/00100/01010/10001/10001",
    "Y":"10001/10001/01010/00100/00100/00100/00100",
    "Z":"11111/00001/00010/00100/01000/10000/11111",
    "0":"01110/10001/10011/10101/11001/10001/01110",
    "1":"00100/01100/00100/00100/00100/00100/01110",
    "2":"01110/10001/00001/00010/00100/01000/11111",
    "3":"11110/00001/00001/01110/00001/00001/11110",
    "4":"00010/00110/01010/10010/11111/00010/00010",
    "5":"11111/10000/10000/11110/00001/00001/11110",
    "6":"01110/10000/10000/11110/10001/10001/01110",
    "7":"11111/00001/00010/00100/01000/01000/01000",
    "8":"01110/10001/10001/01110/10001/10001/01110",
    "9":"01110/10001/10001/01111/00001/00001/01110",
    ":":"00000/00100/00100/00000/00100/00100/00000",
    ".":"00000/00000/00000/00000/00000/00100/00100",
    ",":"00000/00000/00000/00000/00100/00100/01000",
    "-":"00000/00000/00000/11111/00000/00000/00000",
    "/":"00001/00010/00010/00100/01000/01000/10000",
    "+":"00000/00100/00100/11111/00100/00100/00000",
    "(":"00010/00100/01000/01000/01000/00100/00010",
    ")":"01000/00100/00010/00010/00010/00100/01000",
    "?":"01110/10001/00001/00010/00100/00000/00100",
    "!":"00100/00100/00100/00100/00100/00000/00100",
    "|":"00100/00100/00100/00100/00100/00100/00100",
    "'":"00100/00100/00000/00000/00000/00000/00000",
    "<":"00010/00100/01000/10000/01000/00100/00010",
    ">":"01000/00100/00010/00001/00010/00100/01000",
    "=":"00000/00000/11111/00000/11111/00000/00000",
    "&":"01100/10010/10100/01000/10101/10010/01101",
    "@":"01110/10001/10111/10101/10111/10000/01111",
    "%":"11001/11010/00010/00100/01000/10110/00110",
}


def _clean(value):
    value = str(value or "").upper().replace("\u2013", "-").replace("\u2014", "-").replace("\u00b7", "/").replace("\u2019", "'")
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


def _width(value, scale=1, tracking=0):
    return max(0, len(_clean(value)) * (6 * scale + tracking) - scale - tracking)


def _text(draw, xy, value, color=WHITE, scale=1, max_width=None, align="left", tracking=0):
    value = _clean(value)
    if max_width is not None and _width(value, scale, tracking) > max_width:
        count = max(0, (max_width + scale + tracking) // (6 * scale + tracking) - 2)
        value = value[:count] + ".." if count else ""
    x, y = xy
    if align == "right":
        x -= _width(value, scale, tracking)
    elif align == "center":
        x -= _width(value, scale, tracking) // 2
    for char in value:
        rows = _GLYPHS.get(char, _GLYPHS["?"]).split("/")
        for dy, row in enumerate(rows):
            for dx, bit in enumerate(row):
                if bit == "1":
                    xx, yy = int(x + dx * scale), int(y + dy * scale)
                    draw.rectangle((xx, yy, xx + scale - 1, yy + scale - 1), fill=color)
        x += 6 * scale + tracking


def _dt(value, default=None):
    if isinstance(value, datetime):
        result = value
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return default
    else:
        return default
    if result.tzinfo is None:
        return default
    return result


def _tz(config):
    try:
        return ZoneInfo(config.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.utc


def _flights(state, config, now):
    flights = []
    for flight in state.get("flights") or []:
        if not isinstance(flight, dict):
            continue
        if flight.get("deadhead") and not config.get("show_deadheads", True):
            continue
        start, end = _dt(flight.get("start")), _dt(flight.get("end"))
        if start and end and end > now:
            flights.append(flight)
    return sorted(flights, key=lambda f: _dt(f["start"]))


def _selected(state, flights):
    selected = state.get("selected_flight")
    if isinstance(selected, dict):
        return selected
    if isinstance(selected, str):
        for flight in flights:
            if flight.get("id") == selected:
                return flight
    return flights[0] if flights else None


@lru_cache(maxsize=2)
def _asset(name):
    try:
        return json.loads((ASSETS / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _airport(code):
    return _asset("airports.json").get(_clean(code))


def _city(code):
    airport = _airport(code)
    return airport.get("city", code) if airport else code


def _clock(draw, state, now):
    zone = _tz({"timezone": state.get("display_timezone", "UTC")})
    _text(draw, (496, 13), now.astimezone(zone).strftime("%H:%M %Z"), MUTED, align="right")
    if state.get("demo", False):
        draw.rectangle((348, 9, 380, 22), outline=AMBER)
        _text(draw, (353, 12), "DEMO", AMBER)


def _header(draw, state, now, page, *, page_scale=1, page_tracking=0):
    for row in range(3):
        for col in range(3):
            draw.rectangle((15 + col * 4, 10 + row * 4, 16 + col * 4, 11 + row * 4), fill=AMBER if row + col == 2 else CYAN)
    _text(draw, (35, 10), "FLIGHTWALL", WHITE, 2)
    _clock(draw, state, now)
    draw.line((14, 31, 497, 31), fill=LINE)
    _text(draw, (15, 40), page, CYAN, page_scale, tracking=page_tracking)


def _footer(draw, state, view, now, tz, personal_item=None):
    draw.line((14, 231, 497, 231), fill=LINE)
    source = state.get("source") or "CALENDAR"
    if view == "custody_outlook":
        outlook = state.get("custody_outlook")
        outlook = outlook if isinstance(outlook, dict) else {}
        status, color = ("SCHEDULE UNAVAILABLE / UNKNOWN IS NOT AWAY", AMBER) if outlook.get("stale", True) else ("FAMILY PLAN / CALENDAR SCHEDULE", MUTED)
    elif view == "flight_watch":
        card = _selected_card(state, "flight_watch") or {}
        status, color = "PUBLIC REQUEST / " + str(card.get("date") or "DATE UNKNOWN")[:10], MUTED
        if card.get("status") == "stale":
            status, color = status + " / STALE", AMBER
    elif view in {"trip_summary", "trip_map"}:
        status, color = "PLANNED TRIP / NOT LIVE TRACKING", MUTED
    elif view == "home":
        status, color = "HOME BOARD / TODAY", MUTED
    elif view in {"message", "inspiration", "health", "layover", "local_visit", "setup"}:
        status = {"message": "PERSONAL MESSAGES", "inspiration": "A MOMENT FOR YOU", "health": "CURATED READING / CHECK ORIGINAL SOURCE",
                  "layover": "MY TRIP / USER-PROVIDED DETAILS", "local_visit": "CALENDAR SCHEDULE / NOT LIVE TRACKING",
                  "setup": "SETUP / YOUR SETTINGS SAVE LOCALLY"}[view]
        color = MUTED
        if state.get("demo"):
            status, color = "DEMONSTRATION / FICTIONAL CARD", AMBER
    elif view == "personal":
        if state.get("demo"):
            status, color = "SAMPLE CARD / FICTIONAL EXAMPLE", AMBER
        elif personal_item:
            status, color = "PERSONAL BOARD / " + str(personal_item.get("source") or "MANUAL"), MUTED
        else:
            status, color = "PERSONAL BOARD / NO ACTIVE CARDS", MUTED
    elif state.get("demo"):
        status, color = "SAMPLE FLIGHTS / CONNECT YOUR CALENDAR", AMBER
    elif state.get("error"):
        status, color = "OFFLINE / CACHED SCHEDULE", AMBER
    else:
        synced = _dt(state.get("last_sync"))
        if synced:
            minutes = max(0, int((now - synced).total_seconds() // 60))
            status, color = f"{source} / SYNC {minutes}M AGO", MUTED
        else:
            status, color = "CONNECT YOUR FLIGHT CALENDAR", MUTED
    _text(draw, (15, 241), status, color, max_width=360)
    names = ("details", "map", "schedule")
    if view in {"message", "inspiration", "health", "layover", "local_visit", "flight_watch", "trip_summary", "trip_map", "home", "setup", "custody_outlook"}:
        names += (view,)
    elif state.get("personal_items") or view == "personal":
        names += ("personal",)
    for i, name in enumerate(names):
        x = 486 - 13 * (len(names) - 1 - i)
        draw.rectangle((x, 241, x + 5, 246), fill=CYAN if view == name else LINE)


def _arrow(draw, x, y, width=45, color=AMBER):
    draw.line((x, y, x + width, y), fill=color, width=2)
    draw.line((x + width - 8, y - 7, x + width, y, x + width - 8, y + 7), fill=color, width=2)


def _duration(start, end):
    minutes = max(0, int((end - start).total_seconds() // 60))
    return f"{minutes // 60}H {minutes % 60:02d}M"


def _details(draw, state, config, now, flight, flights, tz):
    if not flight:
        _idle(draw, state, config, now, flights, tz)
        return
    start, end = _dt(flight.get("start")), _dt(flight.get("end"))
    if start is None or end is None:
        _idle(draw, state, config, now, flights, tz)
        return
    active = start <= now < end
    past = now >= end
    _header(draw, state, now, "MY CURRENT LEG / SCHEDULE" if active else "MY RECENT LEG / SCHEDULE" if past else "MY NEXT LEG / SCHEDULE")
    number = str(flight.get("number") or "FLIGHT")
    if flight.get("deadhead"):
        number += " / DEADHEAD"
    _text(draw, (497, 40), number, AMBER, align="right", max_width=245)
    _text(draw, (20, 64), flight.get("origin", "---"), WHITE, 7, max_width=140)
    _text(draw, (492, 64), flight.get("destination", "---"), WHITE, 7, max_width=140, align="right")
    _arrow(draw, 232, 87, 47)
    _text(draw, (256, 107), _duration(start, end), MUTED, align="center")
    _text(draw, (21, 121), _city(flight.get("origin", "")), MUTED, max_width=203)
    _text(draw, (491, 121), _city(flight.get("destination", "")), MUTED, max_width=203, align="right")
    draw.rectangle((15, 141, 496, 194), fill=PANEL)
    draw.line((256, 149, 256, 186), fill=LINE)
    local_start, local_end = start.astimezone(tz), end.astimezone(tz)
    _text(draw, (26, 150), "DEPART / " + local_start.strftime("%Z"), CYAN)
    _text(draw, (270, 150), "ARRIVE / " + local_end.strftime("%Z"), CYAN)
    _text(draw, (25, 166), local_start.strftime("%H:%M"), AMBER, 3)
    _text(draw, (270, 166), local_end.strftime("%H:%M"), AMBER, 3)
    _text(draw, (239, 175), local_start.strftime("%d %b"), MUTED, align="right")
    _text(draw, (484, 175), local_end.strftime("%d %b"), MUTED, align="right")
    report = _dt(flight.get("report"))
    leave = _dt(flight.get("leave_at")) if config.get("brief_leave_times_enabled") is True else None
    leave_conflict = flight.get("leave_conflict") is True and config.get("brief_leave_times_enabled") is True
    if past:
        text = "SCHEDULED LEG ENDED"
    elif leave:
        text = "BRIEF LEAVE " + leave.astimezone(tz).strftime("%d %b %H:%M %Z")
    elif leave_conflict:
        text = "BRIEF CONFLICT / CHECK LEAVE TIME"
    elif report:
        text = "REPORT " + report.astimezone(tz).strftime("%H:%M %Z")
    elif active:
        text = "SCHEDULED LEG IN PROGRESS"
    else:
        minutes = max(0, int((start - now).total_seconds() // 60))
        text = f"DEPARTS IN {minutes // 60}H {minutes % 60:02d}M"
    _text(draw, (16, 208), text, WHITE, max_width=300)
    remaining = sum(1 for f in flights if _dt(f.get("start")) and _dt(f["start"]) > start)
    _text(draw, (496, 208), "VERIFY REPORT" if leave_conflict else f"+{remaining} UPCOMING" if remaining else "FINAL SCHEDULED LEG", AMBER if leave_conflict else MUTED, align="right", max_width=170)


def great_circle(origin, destination, points=81):
    """Return (longitude, latitude) samples along the shortest spherical route."""
    lat1, lon1 = math.radians(origin[1]), math.radians(origin[0])
    lat2, lon2 = math.radians(destination[1]), math.radians(destination[0])
    a = (math.cos(lat1) * math.cos(lon1), math.cos(lat1) * math.sin(lon1), math.sin(lat1))
    b = (math.cos(lat2) * math.cos(lon2), math.cos(lat2) * math.sin(lon2), math.sin(lat2))
    angle = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)))))
    if abs(math.sin(angle)) < 1e-9:
        return [origin, destination]
    result = []
    for i in range(points):
        f = i / (points - 1)
        u, v = math.sin((1 - f) * angle) / math.sin(angle), math.sin(f * angle) / math.sin(angle)
        x, y, z = (u * x + v * y for x, y in zip(a, b))
        result.append((math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y)))))
    return result


def _live_position(state):
    live = state.get("live")
    if not isinstance(live, dict) or not isinstance(live.get("source"), str) or not live["source"].strip():
        return None
    try:
        lat = float(live.get("latitude", live.get("lat")))
        lon = float(live.get("longitude", live.get("lon")))
    except (ValueError, TypeError):
        return None
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return lon, lat


def _map(draw, image, state, config, now, flight, tz, page="ROUTE MAP / GREAT CIRCLE"):
    _header(draw, state, now, page)
    if not flight:
        _text(draw, (256, 116), "NO UPCOMING FLIGHT", WHITE, 2, align="center")
        _text(draw, (256, 146), "YOUR NEXT ROUTE WILL APPEAR HERE", MUTED, align="center")
        return
    origin_code, dest_code = flight.get("origin", "---"), flight.get("destination", "---")
    _text(draw, (497, 40), f"{origin_code} > {dest_code}", AMBER, align="right")
    a, b = _airport(origin_code), _airport(dest_code)
    if not a or not b:
        _text(draw, (256, 108), f"{origin_code} > {dest_code}", WHITE, 3, align="center")
        _text(draw, (256, 152), "MAP COORDINATES UNAVAILABLE", MUTED, align="center")
        return
    route = great_circle((a["lon"], a["lat"]), (b["lon"], b["lat"]))
    center_lon = route[len(route) // 2][0]
    center_lat = (min(lat for _, lat in route) + max(lat for _, lat in route)) / 2
    coslat = max(0.2, math.cos(math.radians(center_lat)))

    def wrap(lon):
        return ((lon - center_lon + 180) % 360) - 180

    xx = [wrap(lon) * coslat for lon, lat in route]
    yy = [lat - center_lat for lon, lat in route]
    # Regional routes retain geographic context; city pairs stay in the header.
    span_x, span_y = max(4.8, max(xx) - min(xx)), max(2.4, max(yy) - min(yy))
    zoom = min(348 / (span_x * 1.35), 118 / (span_y * 1.5))
    cx, cy = 255, 133

    def project(point):
        lon, lat = point
        return (int(round(cx + wrap(lon) * coslat * zoom)), int(round(cy - (lat - center_lat) * zoom)))

    map_image = Image.new("RGB", BASE_SIZE, BG)
    md = ImageDraw.Draw(map_image)
    md.rectangle((15, 57, 496, 201), fill=(5, 15, 21))
    # The grid is latitude/longitude, not decorative straight route segments.
    grid_step = 5 if zoom > 10 else 10
    for lat in range(-80, 90, grid_step):
        y = project((center_lon, lat))[1]
        if 57 <= y <= 201:
            md.line((15, y, 496, y), fill=(10, 26, 34))
    for lon in range(-180, 181, grid_step):
        x = project((lon, center_lat))[0]
        if 15 <= x <= 496:
            md.line((x, 57, x, 201), fill=(10, 26, 34))
    world = _asset("world.json")
    for ring in world.get("land", []):
        projected = [project(p) for p in ring]
        # Avoid a dateline-closing polygon spanning an entire map.
        if not projected or max(x for x, y in projected) < 15 or min(x for x, y in projected) > 496 or max(y for x, y in projected) < 57 or min(y for x, y in projected) > 201:
            continue
        if max(x for x, y in projected) - min(x for x, y in projected) < 360 * coslat * zoom * 0.85:
            md.polygon(projected, fill=(10, 27, 33), outline=(38, 79, 88))
    for line in world.get("states", []):
        projected = [project(p) for p in line]
        if len(projected) > 1:
            for p1, p2 in zip(projected, projected[1:]):
                if abs(p2[0] - p1[0]) < 200:
                    md.line((p1, p2), fill=(29, 60, 68))
    route_pixels = [project(p) for p in route]
    md.line(route_pixels, fill=(68, 53, 30), width=5)
    md.line(route_pixels, fill=AMBER, width=2)
    for p in (route_pixels[0], route_pixels[-1]):
        x, y = p
        md.ellipse((x - 4, y - 4, x + 4, y + 4), fill=BG, outline=AMBER)
        md.rectangle((x - 1, y - 1, x + 1, y + 1), fill=WHITE)
    live_pos = _live_position(state)
    if live_pos:
        x, y = project(live_pos)
        if 23 <= x <= 488 and 65 <= y <= 193:
            md.polygon(((x, y - 6), (x - 4, y + 4), (x, y + 2), (x + 4, y + 4)), fill=CYAN)
    image.paste(map_image.crop((15, 57, 497, 202)), (15, 57))
    if live_pos:
        _text(draw, (16, 213), "LIVE POSITION / " + str(state["live"]["source"]), CYAN, max_width=475)
    else:
        _text(draw, (16, 213), "PLANNED ROUTE / NOT LIVE TRACKING", MUTED)
    # Small compass is a map reference, not an aircraft.
    _text(draw, (484, 68), "N", CYAN)
    draw.line((486, 81, 486, 95), fill=CYAN)
    draw.line((482, 85, 486, 81, 490, 85), fill=CYAN)


def _schedule(draw, state, config, now, flights, tz):
    _header(draw, state, now, "MY UPCOMING FLIGHTS")
    _text(draw, (497, 40), now.astimezone(tz).strftime("TIMES %Z"), AMBER, align="right")
    if not flights:
        _text(draw, (256, 115), "YOUR BOARD IS CLEAR", WHITE, 2, align="center")
        _text(draw, (256, 147), "NEW CALENDAR FLIGHTS APPEAR AUTOMATICALLY", MUTED, align="center")
        return
    _text(draw, (22, 63), "DATE / FLIGHT", MUTED)
    _text(draw, (174, 63), "ROUTE", MUTED)
    _text(draw, (376, 63), "DEPART", MUTED)
    _text(draw, (489, 63), "ARRIVE", MUTED, align="right")
    for index, flight in enumerate(flights[:4]):
        y = 80 + index * 35
        start, end = _dt(flight["start"]).astimezone(tz), _dt(flight["end"]).astimezone(tz)
        if index == 0:
            draw.rectangle((15, y - 4, 496, y + 26), fill=PANEL)
            draw.rectangle((15, y - 4, 17, y + 26), fill=AMBER)
        _text(draw, (24, y), start.strftime("%d %b"), CYAN)
        _text(draw, (24, y + 13), str(flight.get("number", "")) + (" DH" if flight.get("deadhead") else ""), MUTED, max_width=132)
        _text(draw, (174, y + 4), f"{flight.get('origin', '---')} > {flight.get('destination', '---')}", WHITE, 2, max_width=181)
        _text(draw, (376, y + 6), start.strftime("%H:%M"), AMBER)
        _text(draw, (489, y + 6), end.strftime("%H:%M"), WHITE, align="right")
    _text(draw, (16, 223), f"{len(flights)} UPCOMING / CALENDAR SCHEDULE", MUTED)


def _idle(draw, state, config, now, flights, tz):
    _header(draw, state, now, "OFF DUTY / FLIGHT CLOCK")
    local = now.astimezone(tz)
    _text(draw, (256, 73), local.strftime("%H:%M"), WHITE, 7, align="center")
    _text(draw, (256, 134), local.strftime("%A / %d %B / %Z"), CYAN, align="center")
    draw.line((116, 157, 396, 157), fill=LINE)
    if flights:
        f = flights[0]
        _text(draw, (256, 175), "NEXT " + f"{f.get('origin', '---')} > {f.get('destination', '---')}", AMBER, 2, align="center")
        _text(draw, (256, 202), _dt(f["start"]).astimezone(tz).strftime("%a %d %b / %H:%M %Z"), MUTED, align="center")
    else:
        _text(draw, (256, 177), "NO UPCOMING FLIGHTS", AMBER, 2, align="center")
        _text(draw, (256, 204), "CONNECT YOUR CALENDAR TO FILL THE BOARD", MUTED, align="center")


def _nearby(draw, state, config, now, flights, tz):
    nearby = state.get("nearby") or []
    if not nearby:
        _idle(draw, state, config, now, flights, tz)
        return
    provider = str(nearby[0].get("source") or "PROVIDER DATA")[:18]
    _header(draw, state, now, "NEARBY TRAFFIC / " + provider)
    for i, aircraft in enumerate(nearby[:4]):
        y = 70 + i * 36
        draw.line((16, y + 27, 495, y + 27), fill=LINE)
        _text(draw, (20, y), aircraft.get("callsign") or aircraft.get("number") or aircraft.get("flight") or "UNKNOWN", WHITE, 2, max_width=190)
        route = f"{aircraft.get('origin') or '---'} > {aircraft.get('destination') or '---'}"
        _text(draw, (224, y + 4), route, AMBER, max_width=130)
        altitude = aircraft.get("altitude")
        _text(draw, (492, y + 4), f"{altitude} FT" if altitude is not None else "ALT --", MUTED, align="right", max_width=125)
    _text(draw, (16, 219), "YOUR FLIGHT SCHEDULE TAKES PRIORITY", CYAN)


def _visible_personal(items, now):
    """Defend against a stale backend frame leaving an expired card onscreen."""
    visible = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in {"appointment", "dinner"}:
            continue
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            continue
        start = _dt(item.get("display_start") or item.get("start"))
        end = _dt(item.get("display_end") or item.get("end"))
        if start and end and start <= now < end:
            visible.append(item)
    return visible


def _selected_personal(state, now):
    selected = state.get("selected_personal_item")
    if isinstance(selected, dict):
        return next(iter(_visible_personal([selected], now)), None)
    items = _visible_personal(state.get("personal_items"), now)
    if isinstance(selected, str):
        return next((item for item in items if item.get("id") == selected), None)
    return items[0] if items else None


def _draw_title(draw, title, xy, width, height, scales=(4, 3, 2), color=WHITE):
    """Fit a complete short title, wrapping words before reducing its size."""
    title = " ".join(_clean(title).split())
    for scale in scales:
        chars = max(1, (width + scale) // (6 * scale))
        lines = textwrap.wrap(title, width=chars, break_long_words=True, break_on_hyphens=False)
        line_height = 7 * scale + 9
        capacity = max(1, (height + 9) // line_height)
        if len(lines) <= capacity:
            break
    if len(lines) > capacity:
        lines = lines[:capacity]
        lines[-1] = lines[-1][:max(0, chars - 2)].rstrip() + ".."
    text_height = len(lines) * line_height - 9
    x, y = xy
    y += max(0, (height - text_height) // 2)
    for index, line in enumerate(lines):
        _text(draw, (x, y + index * line_height), line, color, scale, max_width=width)


def _personal(draw, state, config, now, item):
    if item is None:
        _header(draw, state, now, "PERSONAL BOARD")
        _text(draw, (256, 108), "YOUR HOME BOARD IS CLEAR", WHITE, 2, align="center")
        _text(draw, (256, 143), "NEW CARDS APPEAR ON THEIR SCHEDULE", MUTED, align="center")
        return
    tz = _tz({"timezone": config.get("timezone") or item.get("timezone") or "UTC"})
    local_now = now.astimezone(tz)
    start, end = _dt(item.get("start")), _dt(item.get("end"))
    shown_date = start.astimezone(tz) if start else local_now
    date_label = ("TODAY / " if shown_date.date() == local_now.date() else "") + shown_date.strftime("%a %d %b")
    dinner = item["kind"] == "dinner"
    _header(draw, state, now, "HOME BOARD" if dinner else "MY APPOINTMENT")
    _text(draw, (497, 40), date_label, AMBER, align="right", max_width=265)
    if dinner:
        _text(draw, (25, 67), "TONIGHT'S DINNER", CYAN, 2)
        # Small place setting, drawn as pixels just like the rest of the wall.
        draw.ellipse((462, 62, 482, 82), outline=LINE, width=2)
        draw.ellipse((467, 67, 477, 77), outline=CYAN)
        draw.line((454, 63, 454, 81), fill=CYAN)
        draw.line((451, 62, 451, 68, 457, 68, 457, 62), fill=CYAN)
        draw.line((489, 62, 489, 81), fill=AMBER)
        _draw_title(draw, item["title"], (25, 96), 464, 62)
        # Compute on every frame, even when the backend card is cached/paused.
        # The TV's household zone governs the rule, not a stored card's zone.
        snacks_closed = local_now.hour >= 17
        snack_color = (244, 92, 92) if snacks_closed else GREEN
        snack_fill = (43, 11, 16) if snacks_closed else (8, 28, 21)
        draw.rectangle((25, 171, 486, 196), fill=snack_fill, outline=snack_color, width=2)
        _text(draw, (256, 177), "NO MORE SNACKS" if snacks_closed else "SNACKS OK",
              snack_color, 2, align="center", max_width=440)
        until = _dt(item.get("display_end")) or end
        if until:
            _text(draw, (25, 209), "CLEARS AT " + until.astimezone(tz).strftime("%I:%M %p %Z").lstrip("0"), MUTED, max_width=300)
        else:
            _text(draw, (25, 209), "TONIGHT'S MEAL", MUTED)
    else:
        _draw_title(draw, item["title"], (25, 63), 464, 72, scales=(3, 2))
        draw.rectangle((15, 145, 496, 196), fill=PANEL)
        draw.line((256, 153, 256, 187), fill=LINE)
        for x, label, value in ((26, "START", start), (270, "ENDS", end)):
            local = value.astimezone(tz) if value else None
            _text(draw, (x, 153), label + (" / " + local.strftime("%Z") if local else ""), CYAN)
            _text(draw, (x - 1, 170), local.strftime("%I:%M %p").lstrip("0") if local else "--:--", AMBER, 3, max_width=215)
        if start and end and start <= now < end:
            status = "APPOINTMENT IN PROGRESS"
        elif start and start > now:
            minutes = max(0, int((start - now).total_seconds() // 60))
            status = f"STARTS IN {minutes // 60}H {minutes % 60:02d}M"
        else:
            status = "PERSONAL APPOINTMENT"
        _text(draw, (16, 209), status, WHITE, max_width=300)
    source = _clean(item.get("source") or "MANUAL")
    _text(draw, (496, 209), "VIA " + source, MUTED, align="right", max_width=165)


def _paragraph(draw, value, xy, width, *, scale=1, lines=4, color=WHITE, gap=6):
    """Bound all external card text to its allotted rectangle."""
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(line) for line in value[:lines])
    value = _clean(value)
    chars = max(1, (width + scale) // (6 * scale))
    wrapped = []
    for paragraph in value.splitlines() or [""]:
        wrapped.extend(textwrap.wrap(paragraph, chars, break_on_hyphens=False) or [""])
    clipped = wrapped[:lines]
    if len(wrapped) > lines and clipped:
        clipped[-1] = clipped[-1][:max(0, chars - 2)].rstrip() + ".."
    for index, line in enumerate(clipped):
        _text(draw, (xy[0], xy[1] + index * (7 * scale + gap)), line, color, scale, max_width=width)


def _selected_card(state, view):
    value = state.get("selected_" + view) or state.get("selected_card")
    return value if isinstance(value, dict) else None


def _source_info(card):
    source = card.get("source")
    name = source.get("name", "") if isinstance(source, dict) else str(source or "")
    url = card.get("source_url") or card.get("url") or (source.get("url") if isinstance(source, dict) else "")
    try:
        parsed = urlsplit(str(url or ""))
        host = parsed.hostname or ""
    except ValueError:
        host = ""
    published = card.get("published_at") or card.get("source_date")
    date = _dt(published)
    date_label = date.strftime("%d %b %Y") if date else str(published or "")[:10]
    return name, host, date_label


def _empty_card(draw, state, now, view):
    page = {"message": "PERSONAL MESSAGES", "inspiration": "A MOMENT FOR YOU", "health": "CURATED HEALTH READING", "layover": "MY TRIP / LAYOVER"}[view]
    _header(draw, state, now, page)
    _text(draw, (256, 108), "NO CARD TO DISPLAY", WHITE, 2, align="center")
    _text(draw, (256, 145), "YOUR DISPLAY FOLLOWS YOUR PRIVACY SETTINGS", MUTED, align="center")


def _message(draw, state, config, now, card):
    profile = str(card.get("profile") or card.get("owner") or card.get("recipient") or "").upper()
    _header(draw, state, now, "MESSAGE FOR " + profile)
    timestamp = _dt(card.get("received_at") or card.get("date") or card.get("observed_at"))
    label = timestamp.astimezone(_tz(config)).strftime("%d %b %H:%M %Z") if timestamp else "FILTERED EMAIL"
    _text(draw, (497, 40), label, AMBER, align="right", max_width=265)
    sender = card.get("sender_name") or card.get("sender") or card.get("source") or "YOUR INBOX"
    if isinstance(sender, dict):
        sender = sender.get("name") or "YOUR INBOX"
    _text(draw, (24, 65), "FROM / " + str(sender), CYAN, max_width=464)
    _draw_title(draw, card.get("title") or card.get("subject") or "MESSAGE", (24, 83), 464, 59, scales=(3, 2))
    draw.line((24, 155, 487, 155), fill=LINE)
    _paragraph(draw, card.get("body") or card.get("summary") or "Open your email to read the message.", (24, 169), 464, lines=4, color=MUTED, gap=5)
    needs_reply = card.get("needs_reply") is True
    reply_label = (card.get("reply_label") or "REPLY NEEDED") if needs_reply else "OPEN YOUR EMAIL TO READ / REPLY"
    _text(draw, (24, 221), reply_label, AMBER if needs_reply else MUTED, max_width=464)


def _abstract_art(image, state, card):
    """Original offline pixel artwork, or a decoded image supplied by the app.

    Only an in-memory PIL image is accepted; no card can open a file path or URL.
    """
    artwork = state.get("artwork_image")
    if isinstance(artwork, Image.Image):
        artwork = artwork.convert("RGB")
        ratio = max(144 / artwork.width, 139 / artwork.height)
        artwork = artwork.resize((max(144, round(artwork.width * ratio)), max(139, round(artwork.height * ratio))), Image.Resampling.LANCZOS)
        x, y = (artwork.width - 144) // 2, (artwork.height - 139) // 2
        artwork = artwork.crop((x, y, x + 144, y + 139))
    else:
        artwork = Image.new("RGB", (144, 139), (11, 28, 35))
        art = ImageDraw.Draw(artwork)
        for y in range(0, 139, 4):
            shade = y / 139
            art.rectangle((0, y, 143, y + 3), fill=(int(11 + shade * 4), int(28 + shade * 18), int(35 + shade * 16)))
        art.ellipse((82, 20, 113, 51), fill=AMBER)
        art.polygon(((0, 110), (37, 49), (78, 110)), fill=(30, 86, 94))
        art.polygon(((49, 113), (98, 62), (143, 109), (143, 139), (0, 139)), fill=(24, 64, 73))
        art.polygon(((14, 139), (78, 93), (65, 112), (101, 139)), fill=(94, 148, 144))
        art.rectangle((0, 130, 143, 138), fill=(8, 28, 35))
        for x, y in ((15, 18), (42, 28), (66, 15)):
            art.rectangle((x, y, x + 1, y + 1), fill=CYAN)
    image.paste(artwork, (15, 63))


def _inspiration(draw, image, state, config, now, card):
    _header(draw, state, now, "A MOMENT FOR YOU")
    _abstract_art(image, state, card)
    draw.rectangle((14, 62, 159, 202), outline=LINE)
    _draw_title(draw, card.get("title") or "A LITTLE PERSPECTIVE", (180, 65), 313, 54, scales=(2,))
    _paragraph(draw, card.get("body") or card.get("quote") or card.get("summary") or "", (180, 132), 313, lines=5, color=MUTED, gap=7)
    source = card.get("attribution") or _source_info(card)[0]
    if source:
        _text(draw, (496, 215), source, CYAN, align="right", max_width=468)


def _health(draw, state, config, now, card):
    _header(draw, state, now, "CURATED HEALTH READING")
    _text(draw, (497, 40), "SOURCE SUMMARY", AMBER, align="right")
    _draw_title(draw, card.get("title") or "SAVED READING", (24, 61), 464, 56, scales=(2,))
    _paragraph(draw, card.get("body") or card.get("summary") or "Open the original source for the full article.", (24, 128), 464, lines=4, color=WHITE, gap=7)
    draw.line((24, 187, 487, 187), fill=LINE)
    name, host, date = _source_info(card)
    _text(draw, (24, 199), "SOURCE / " + name, CYAN, max_width=298)
    _text(draw, (488, 199), date, AMBER, align="right", max_width=158)
    _text(draw, (24, 215), host, MUTED, max_width=464)


def _layover(draw, state, config, now, card):
    _header(draw, state, now, "MY TRIP / LAYOVER")
    airport = card.get("airport") or card.get("destination") or "---"
    city = card.get("city") or _city(airport)
    _text(draw, (24, 66), airport, WHITE, 5, max_width=140)
    _text(draw, (165, 70), city, CYAN, 2, max_width=325)
    end = _dt(card.get("end") or card.get("display_end"))
    if end:
        zone = _tz({"timezone": config.get("timezone") or card.get("timezone") or "UTC"})
        _text(draw, (165, 94), "UNTIL " + end.astimezone(zone).strftime("%d %b %I:%M %p %Z").replace(" 0", " "), MUTED, max_width=325)
    draw.rectangle((15, 124, 496, 196), fill=PANEL)
    hotel = card.get("hotel") if card.get("hotel_user_provided") is True or config.get("hotel_details_enabled") is True else None
    if isinstance(hotel, dict):
        name, address = hotel.get("name"), hotel.get("address")
    else:
        name, address = hotel, None
    if name:
        _text(draw, (26, 135), "HOTEL / PROVIDED BY YOU", CYAN)
        _paragraph(draw, name, (26, 153), 454, scale=2, lines=2, color=WHITE, gap=6)
        if address:
            _text(draw, (26, 207), address, MUTED, max_width=454)
    else:
        _text(draw, (26, 139), "YOUR NEXT LEG", CYAN)
        _paragraph(draw, card.get("next_flight") or card.get("body") or "Your flight schedule stays on the board while you are away.", (26, 159), 454, lines=3, color=WHITE, gap=6)
        _text(draw, (26, 209), "HOTEL DETAILS APPEAR ONLY WHEN YOU ADD THEM", MUTED, max_width=454)


def _setup(draw, state, config, now):
    card = state.get("setup") or _selected_card(state, "setup") or {}
    if not isinstance(card, dict):
        card = {}
    _header(draw, state, now, "WELCOME / SETUP")
    _draw_title(draw, card.get("title") or "SET UP YOUR FLIGHT WALL", (24, 62), 464, 63, scales=(3, 2))
    _paragraph(draw, card.get("body") or "Open the setup page on your phone or computer connected to this network.", (24, 139), 464, lines=3, color=MUTED, gap=6)
    draw.rectangle((15, 187, 496, 222), fill=PANEL, outline=LINE)
    url = card.get("url") or state.get("setup_url")
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        _paragraph(draw, url, (25, 196), 464, lines=2, color=AMBER, gap=5)
    else:
        _text(draw, (25, 201), "OPEN FLIGHT WALL SETUP IN THE PORTS MENU", CYAN, max_width=464)


def _local_visit(draw, state, config, now):
    _header(draw, state, now, "MY LOCAL VISIT / SCHEDULE")
    card = _selected_card(state, "local_visit") or {}
    start, end = _dt(card.get("display_start")), _dt(card.get("display_end"))
    if not start or not end or not start <= now < end:
        _text(draw, (256, 115), "NO LOCAL VISIT TO DISPLAY", MUTED, 2, align="center", max_width=464)
        return
    airport = card.get("airport") or "---"
    _text(draw, (24, 62), airport, WHITE, 5, max_width=140)
    _text(draw, (165, 70), _city(airport), CYAN, 2, max_width=325)
    zone = _tz({"timezone": config.get("timezone") or card.get("timezone") or "UTC"})
    arrival, departure = _dt(card.get("arrival")), _dt(card.get("departure"))
    draw.rectangle((15, 111, 496, 222), fill=PANEL)
    draw.line((261, 121, 261, 197), fill=LINE)
    _text(draw, (26, 122), "SCHEDULED ARRIVAL", CYAN)
    if arrival:
        local = arrival.astimezone(zone)
        _text(draw, (26, 142), local.strftime("%I:%M %p").lstrip("0"), WHITE, 4, max_width=224)
        _text(draw, (26, 180), local.strftime("%a %d %b / %Z"), MUTED, max_width=224)
    else:
        _text(draw, (26, 145), "TIME UNKNOWN", MUTED, 2, max_width=224)
    _text(draw, (279, 122), "NEXT DEPARTURE", CYAN)
    if departure:
        local = departure.astimezone(zone)
        _text(draw, (279, 145), local.strftime("%I:%M %p").lstrip("0"), WHITE, 3, max_width=206)
        _text(draw, (279, 177), local.strftime("%a %d %b / %Z"), MUTED, max_width=206)
    else:
        _text(draw, (279, 146), "UNKNOWN", AMBER, 2, max_width=206)
        _text(draw, (279, 177), "NO MATCHING OUTBOUND", MUTED, max_width=206)
    minutes = card.get("duration_minutes")
    leave = _dt(card.get("leave_at")) if config.get("brief_leave_times_enabled") is True else None
    conflict = card.get("leave_conflict") is True and config.get("brief_leave_times_enabled") is True
    if leave:
        if departure and isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 0:
            _text(draw, (26, 192), f"STAY / {minutes // 60}H {minutes % 60:02d}M", MUTED, max_width=224)
        _text(draw, (26, 204), "BRIEF LEAVE / " + leave.astimezone(zone).strftime("%a %d %b %H:%M %Z"), AMBER, max_width=354)
        if conflict:
            _text(draw, (484, 204), "VERIFY", AMBER, align="right")
    elif conflict:
        _text(draw, (26, 204), "BRIEF CONFLICT / CHECK LEAVE TIME AND REPORT", AMBER, max_width=454)
    elif departure and isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 0:
        _text(draw, (26, 204), f"PLANNED STAY / {minutes // 60}H {minutes % 60:02d}M", AMBER, max_width=454)
    else:
        _text(draw, (26, 204), "SCHEDULE ONLY / LOCATION NOT CONFIRMED", MUTED, max_width=454)


def _flight_watch(draw, image, state, config, now, tz):
    """A public request never supplies position or selection for an owned leg."""
    card = _selected_card(state, "flight_watch") or {}
    ident = _clean(card.get("ident") or "PUBLIC FLIGHT")[:24]
    status = card.get("status", "pending")
    flight = card.get("flight")
    if status in {"scheduled", "tracking", "live", "stale"} and isinstance(flight, dict):
        live = card.get("live")
        observed = _dt(live.get("observed_at")) if isinstance(live, dict) else None
        fresh = (status in {"tracking", "live"} and observed and 0 <= (now - observed).total_seconds() <= 300
                 and flight.get("id") and live.get("flight_id") == flight.get("id") and _live_position({"live": live}))
        watch_state = dict(state, live=live if fresh else None, selected_flight=flight)
        _map(draw, image, watch_state, config, now, flight, tz, page="PUBLIC REQUEST / " + ident)
        return
    _header(draw, state, now, "PUBLIC FLIGHT REQUEST")
    _text(draw, (256, 72), ident, WHITE, 4, align="center", max_width=464)
    _text(draw, (256, 117), "REQUESTED DATE / " + str(card.get("date") or "UNKNOWN")[:10], CYAN, align="center", max_width=464)
    title, detail = {
        "unconfigured": ("FLIGHT LOOKUP NOT CONFIGURED", "CONNECT A PUBLIC FLIGHT DATA PROVIDER"),
        "pending": ("LOOKUP PENDING", "WAITING FOR A PROVIDER RESPONSE"),
        "ambiguous": ("MORE THAN ONE FLIGHT MATCH", "CHECK THE FLIGHT NUMBER AND DATE"),
        "not_found": ("NO MATCH FOUND", "CHECK THE FLIGHT NUMBER AND DATE"),
        "unavailable": ("FLIGHT DATA UNAVAILABLE", "THIS REQUEST DOES NOT HAVE A LIVE POSITION"),
        "error": ("FLIGHT LOOKUP UNAVAILABLE", "TRY REFRESHING THIS PUBLIC REQUEST"),
    }.get(status, ("FLIGHT DATA UNAVAILABLE", "THIS REQUEST DOES NOT HAVE A LIVE POSITION"))
    draw.rectangle((15, 144, 496, 219), fill=PANEL)
    _text(draw, (256, 158), title, AMBER, 2, align="center", max_width=464)
    _text(draw, (256, 195), detail, MUTED, align="center", max_width=464)


def _trip_legs(state, config):
    preview = state.get("trip_preview")
    preview = preview if isinstance(preview, dict) else {}
    zone = _tz({"timezone": config.get("timezone") or preview.get("timezone") or "UTC"})
    days = preview.get("days")
    rows = []
    if isinstance(days, list):
        for day_index, day in enumerate(days):
            if not isinstance(day, dict) or not isinstance(day.get("legs"), list):
                continue
            color = day.get("color")
            if not isinstance(color, (list, tuple)) or len(color) != 3 or not all(isinstance(c, int) and 0 <= c <= 255 for c in color):
                color = TRIP_COLORS[day_index % len(TRIP_COLORS)]
            for index, leg in enumerate(day["legs"]):
                if isinstance(leg, dict):
                    rows.append(dict(leg, _trip_date=day.get("date"), _trip_color=tuple(color),
                                     _leg_number=leg.get("leg_number", index + 1)))
    if not rows and isinstance(preview.get("flights"), list):
        dates = []
        counts = {}
        for leg in preview["flights"]:
            if not isinstance(leg, dict):
                continue
            start = _dt(leg.get("start"))
            day = start.astimezone(zone).date().isoformat() if start else "UNKNOWN"
            if day not in dates:
                dates.append(day)
            counts[day] = counts.get(day, 0) + 1
            rows.append(dict(leg, _trip_date=day, _trip_color=TRIP_COLORS[dates.index(day) % len(TRIP_COLORS)],
                             _leg_number=leg.get("leg_number", counts[day])))
    return preview, rows, zone


def _trip_page(state, count):
    pages = max(1, math.ceil(count / TRIP_PAGE_SIZE))
    try:
        page = max(0, int(state.get("trip_page", 0))) % pages
    except (ValueError, TypeError, OverflowError):
        page = 0
    return page, pages


def _trip_date(value, pattern="%a %m/%d"):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime(pattern)
    except ValueError:
        return "DATE UNKNOWN"


def _trip_range(preview):
    return _trip_date(preview.get("start_date"), "%d %b") + " - " + _trip_date(preview.get("end_date"), "%d %b")


def _trip_summary(draw, state, config, now):
    preview, legs, zone = _trip_legs(state, config)
    page, pages = _trip_page(state, len(legs))
    _header(draw, state, now, "UPCOMING TRIP / SCHEDULE")
    _text(draw, (497, 40), f"PAGE {page + 1}/{pages}", AMBER, align="right")
    if not legs:
        _text(draw, (256, 115), "NO UPCOMING TRIP TO DISPLAY", MUTED, 2, align="center", max_width=464)
        return
    for index, leg in enumerate(legs[page * TRIP_PAGE_SIZE:(page + 1) * TRIP_PAGE_SIZE]):
        y = 60 + index * 48
        color = leg["_trip_color"]
        draw.rectangle((15, y, 496, y + 43), fill=PANEL)
        draw.rectangle((22, y + 9, 47, y + 32), outline=color)
        _text(draw, (35, y + 14), leg["_leg_number"], color, 2, align="center", max_width=22)
        _text(draw, (56, y + 8), _trip_date(leg["_trip_date"]), color, max_width=70)
        visits = preview.get("local_visits")
        visit = next((item for item in visits if isinstance(item, dict) and item.get("airport") == leg.get("destination")
                      and _dt(item.get("arrival")) == _dt(leg.get("end"))), None) if isinstance(visits, list) else None
        kind = "DEADHEAD" if leg.get("deadhead") else str(leg.get("destination")) + " VISIT" if visit else "OPERATING"
        _text(draw, (56, y + 26), kind, GREEN if visit else MUTED, max_width=70)
        origin, destination = leg.get("origin", "---"), leg.get("destination", "---")
        _text(draw, (139, y + 7), f"{origin} > {destination}", WHITE, 2, max_width=180)
        _text(draw, (139, y + 28), f"{_city(origin)} > {_city(destination)}", MUTED, max_width=180)
        start, end = _dt(leg.get("start")), _dt(leg.get("end"))
        times = start.astimezone(zone).strftime("%H:%M") + " > " + end.astimezone(zone).strftime("%H:%M") if start and end else "TIME UNKNOWN"
        if start and end:
            overnight = (end.astimezone(zone).date() - start.astimezone(zone).date()).days
            if overnight:
                times += f" +{overnight}D"
        _text(draw, (333, y + 10), times, color, max_width=154)
        if start and end:
            first_zone, last_zone = start.astimezone(zone).strftime("%Z"), end.astimezone(zone).strftime("%Z")
            label = first_zone if first_zone == last_zone else first_zone + " > " + last_zone
            minutes = visit.get("duration_minutes") if visit else None
            if isinstance(minutes, int) and minutes >= 0:
                label += f" / {minutes // 60}H{minutes % 60:02d}M STAY"
            _text(draw, (333, y + 28), label, MUTED, max_width=154)
        departure = _dt(visit.get("departure")) if visit else None
        if departure:
            # The visit may end on a different day. Keep its full departure
            # date beside the inbound leg rather than only a stay duration.
            draw.rectangle((137, y + 22, 328, y + 42), fill=PANEL)
            _text(draw, (139, y + 23), "DEP " + departure.astimezone(zone).strftime("%m/%d %H:%M %Z"), CYAN, max_width=188)
            if config.get("brief_leave_times_enabled") is True:
                leave = _dt(visit.get("leave_at"))
                conflict = visit.get("leave_conflict") is True
                if leave:
                    _text(draw, (139, y + 34), "LEAVE " + leave.astimezone(zone).strftime("%m/%d %H:%M %Z"), AMBER, max_width=188)
                elif conflict:
                    _text(draw, (139, y + 34), "LEAVE TIME CONFLICT", AMBER, max_width=188)
                if conflict:
                    draw.rectangle((331, y + 35, 492, y + 42), fill=PANEL)
                    _text(draw, (333, y + 36), "BRIEF / VERIFY REPORT", AMBER, max_width=154)
    _text(draw, (16, 211), _trip_range(preview), CYAN)
    first, last = page * TRIP_PAGE_SIZE + 1, min((page + 1) * TRIP_PAGE_SIZE, len(legs))
    _text(draw, (497, 211), f"LEGS {first}-{last} OF {len(legs)}", MUTED, align="right")


def _trip_map(draw, image, state, config, now):
    preview, legs, zone = _trip_legs(state, config)
    page, pages = _trip_page(state, len(legs))
    selected_indexes = set(range(page * TRIP_PAGE_SIZE, min((page + 1) * TRIP_PAGE_SIZE, len(legs))))
    page_only = len(legs) > 12
    _header(draw, state, now, "TRIP MAP / " + _trip_range(preview))
    _text(draw, (497, 40), f"PAGE {page + 1}/{pages}", AMBER, align="right")
    layer = Image.new("RGB", BASE_SIZE, BG)
    md = ImageDraw.Draw(layer)
    md.rectangle((15, 59, 324, 201), fill=(5, 15, 21))
    routes = []
    for index, leg in enumerate(legs):
        if page_only and index not in selected_indexes:
            continue
        origin, destination = _airport(leg.get("origin")), _airport(leg.get("destination"))
        if origin and destination:
            route = great_circle((origin["lon"], origin["lat"]), (destination["lon"], destination["lat"]))
            routes.append((index, leg, route))
    if routes:
        center_lon = routes[0][2][0][0]
        points = [point for _, _, route in routes for point in route]
        def wrap(lon):
            return ((lon - center_lon + 180) % 360) - 180
        center_lat = (min(p[1] for p in points) + max(p[1] for p in points)) / 2
        coslat = max(.2, math.cos(math.radians(center_lat)))
        longitudes = [wrap(point[0]) * coslat for point in points]
        center_x = (min(longitudes) + max(longitudes)) / 2
        span_x = max(4.8, max(longitudes) - min(longitudes))
        span_y = max(2.4, max(point[1] for point in points) - min(point[1] for point in points))
        zoom = min(244 / (span_x * 1.2), 100 / (span_y * 1.2))
        def project(point):
            return (round(170 + (wrap(point[0]) * coslat - center_x) * zoom), round(130 - (point[1] - center_lat) * zoom))
        world = _asset("world.json")
        for ring in world.get("land", []):
            projected = [project(point) for point in ring]
            if projected and max(x for x, y in projected) - min(x for x, y in projected) < 360 * coslat * zoom * .85:
                md.polygon(projected, fill=(10, 27, 33), outline=(38, 79, 88))
        for line in world.get("states", []):
            projected = [project(point) for point in line]
            for a, b in zip(projected, projected[1:]):
                if abs(a[0] - b[0]) < 310:
                    md.line((a, b), fill=(29, 60, 68))
        # Dim context routes first, then the current legend page's routes.
        routes.sort(key=lambda row: row[0] in selected_indexes)
        for index, leg, route in routes:
            selected = index in selected_indexes
            color = leg["_trip_color"] if selected else tuple(round(c * .38) for c in leg["_trip_color"])
            pixels = [project(point) for point in route]
            md.line(pixels, fill=color, width=2 if selected else 1)
            for point in (pixels[0], pixels[-1]):
                x, y = point
                md.ellipse((x - 2, y - 2, x + 2, y + 2), fill=BG, outline=color)
        # Keep the geographic layer clear. Dates and city pairs identify
        # routes in the legend instead of placing text over the route lines.
    else:
        _text(md, (170, 119), "MAP COORDINATES", MUTED, align="center")
        _text(md, (170, 133), "UNAVAILABLE", MUTED, align="center")
    image.paste(layer.crop((15, 59, 325, 202)), (15, 59))
    for index, leg in enumerate(legs[page * TRIP_PAGE_SIZE:(page + 1) * TRIP_PAGE_SIZE]):
        y, color = 62 + index * 48, leg["_trip_color"]
        _text(draw, (339, y), _trip_date(leg["_trip_date"]), color, max_width=158, tracking=1)
        origin, destination = leg.get("origin", "---"), leg.get("destination", "---")
        _text(draw, (339, y + 13), f"{origin} > {destination}", WHITE, 2, max_width=158)
        detail = ("DH / " if leg.get("deadhead") else "") + f"{_city(origin)} > {_city(destination)}"
        if not _airport(origin) or not _airport(destination):
            detail = "MAP COORDS UNAVAILABLE"
        _text(draw, (339, y + 32), detail, MUTED, max_width=158)
    first, last = page * TRIP_PAGE_SIZE + 1, min((page + 1) * TRIP_PAGE_SIZE, len(legs))
    _text(draw, (16, 213), "PAGE ROUTES" if page_only else "ALL ROUTES / DATE COLORS", CYAN, tracking=1)
    _text(draw, (497, 213), f"LEGEND {first if legs else 0}-{last} OF {len(legs)}", MUTED, align="right")


def _home(draw, state, config, now):
    _header(draw, state, now, "HOME / TODAY")
    local = now.astimezone(_tz(config))
    _text(draw, (26, 73), local.strftime("%H:%M"), WHITE, 6)
    _text(draw, (26, 127), local.strftime("%Z"), CYAN, 2)
    _text(draw, (26, 161), local.strftime("%A"), WHITE, 2, max_width=204)
    _text(draw, (26, 188), local.strftime("%d %b %Y"), MUTED, max_width=204)
    items = _visible_personal(state.get("personal_items"), now) if config.get("personal_enabled", True) else []
    context = state.get("household_context")
    if config.get("kids_filter_enabled") is True and (not isinstance(context, dict) or context.get("kids_home") is not True):
        items = [item for item in items if item.get("kind") != "dinner"]
    items.sort(key=lambda item: _dt(item.get("start")) or now)
    if not items:
        _text(draw, (369, 108), "YOUR HOME BOARD", MUTED, 2, align="center", max_width=238)
        _text(draw, (369, 136), "IS CLEAR", CYAN, 2, align="center", max_width=238)
    for index, item in enumerate(items[:2]):
        y = 62 + index * 75
        draw.rectangle((245, y, 496, y + 65), fill=PANEL)
        start = _dt(item.get("start"))
        label = str(item.get("kind", "APPOINTMENT")).upper()
        if start:
            label += " / " + start.astimezone(_tz(config)).strftime("%H:%M %Z")
        _text(draw, (254, y + 8), label, CYAN, max_width=234)
        _draw_title(draw, item["title"], (254, y + 24), 234, 33, scales=(2,))
    if len(items) > 2:
        _text(draw, (497, 215), f"+{len(items) - 2} MORE ON YOUR BOARD", MUTED, align="right", max_width=242)


def _custody_outlook(draw, state, config, now):
    """Display only derived daily intervals; never guess a missing day's house."""
    _header(draw, state, now, "FAMILY / NEXT 10 DAYS", page_scale=2)
    outlook = state.get("custody_outlook")
    outlook = outlook if isinstance(outlook, dict) else {}
    zone = _tz({"timezone": config.get("timezone") or outlook.get("timezone") or "UTC"})
    first = now.astimezone(zone).date()
    rows = outlook.get("days")
    days = {row["date"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("date"), str)} if isinstance(rows, list) else {}
    stale = outlook.get("stale") is not False
    _text(draw, (497, 40), zone.key, MUTED, align="right", max_width=196)
    names = {"home": "MOM'S HOUSE", "away": "DAD'S HOUSE", "unknown": "UNKNOWN"}
    colors = {"home": CYAN, "away": AMBER, "unknown": MUTED, "mixed": WHITE}
    for index in range(10):
        date = first + timedelta(days=index)
        row = days.get(date.isoformat(), {})
        kind = row.get("state") if not stale else "unknown"
        kind = kind if kind in colors else "unknown"
        x, y = 15 + (index % 5) * 98, 60 + (index // 5) * 83
        draw.rectangle((x, y, x + 89, y + 74), fill=PANEL, outline=CYAN if index == 0 else LINE)
        _text(draw, (x + 6, y + 7), "TODAY" if index == 0 else date.strftime("%a"), WHITE)
        _text(draw, (x + 83, y + 7), date.strftime("%m/%d"), MUTED, align="right")
        if kind != "mixed":
            _text(draw, (x + 45, y + 30), names[kind], colors[kind], align="center", max_width=82, tracking=1)
            _text(draw, (x + 45, y + 47), "ALL DAY" if kind != "unknown" else "NO ASSUMPTION", MUTED, align="center", max_width=82)
        else:
            _text(draw, (x + 45, y + 22), "CHANGE DAY", WHITE, align="center")
            segments = row.get("segments")
            segments = [part for part in segments if isinstance(part, dict)] if isinstance(segments, list) else []
            if not segments:
                _text(draw, (x + 45, y + 42), "MIXED / CHECK", MUTED, align="center", max_width=82)
            for part_index, part in enumerate(segments[:3]):
                part_kind = part.get("state", "unknown")
                part_kind = part_kind if part_kind in names else "unknown"
                start = _dt(part.get("start"))
                label = {"home": "MOM", "away": "DAD", "unknown": "?"}[part_kind]
                when = start.astimezone(zone).strftime("%H:%M") if start else "--:--"
                _text(draw, (x + 6, y + 36 + part_index * 11), when + " " + label, colors[part_kind], max_width=78)
            if len(segments) > 3:
                _text(draw, (x + 83, y + 64), "+", MUTED, align="right")


def visible_timers(state, now):
    """Bound display input and derive each countdown from its absolute deadline."""
    source = state.get("timers")
    if not isinstance(source, list):
        return []
    result = []
    for row in source:
        if not isinstance(row, dict) or row.get("status") == "cancelled":
            continue
        end = _dt(row.get("ends_at"))
        if end is None or now - end >= COMPLETED_RETENTION:
            continue
        remaining = max(0, math.ceil((end - now).total_seconds()))
        result.append({"label": str(row.get("label") or "TIMER"), "remaining_seconds": remaining})
        if len(result) == 5:
            break
    return result


def _timer_countdown(seconds):
    if seconds <= 0:
        return "DONE"
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    if seconds < 10 * 3600:
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    if seconds < 100 * 3600:
        return f"{seconds // 3600}H{seconds // 60 % 60:02d}M"
    return f"{seconds // 86400}D {seconds // 3600 % 24:02d}H"


def shopping_ticker_names(state):
    """Accept only bounded display names, never arbitrary objects or markup."""
    shopping = state.get("shopping_list")
    if not isinstance(shopping, dict) or shopping.get("enabled") is False or shopping.get("visible") is False:
        return []
    items = shopping.get("items")
    if not isinstance(items, list):
        return []
    names = []
    for item in items[:100]:
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str):
            name = _clean(" ".join(name.split())[:80]).strip()
            if name:
                names.append(name)
    return names


def _overlay_progress(value, active):
    if value is None:
        return float(bool(active))
    try:
        return max(0.0, min(1.0, float(value)))
    except (ValueError, TypeError):
        return float(bool(active))


def _shopping_ticker(names, offset):
    ticker = Image.new("RGB", (BASE_SIZE[0], SHOPPING_TICKER_HEIGHT), PANEL)
    draw = ImageDraw.Draw(ticker)
    draw.line((0, 0, 511, 0), fill=LINE)
    _text(draw, (10, 6), "SHOPPING", GREEN)
    draw.line((65, 4, 65, 14), fill=LINE)
    # Draw just the characters currently crossing the viewport, not the entire
    # list every frame. The repeated separator makes the wrap continuous.
    cycle = "   /   ".join(names) + "   /   "
    try:
        offset = float(offset)
        offset = offset if math.isfinite(offset) else 0.0
    except (ValueError, TypeError):
        offset = 0.0
    pixels = int(offset) % (len(cycle) * 6)
    index, partial = divmod(pixels, 6)
    viewport = Image.new("RGB", (427, 13), PANEL)
    letters = "".join(cycle[(index + i) % len(cycle)] for i in range(74))
    _text(ImageDraw.Draw(viewport), (-partial, 2), letters, WHITE)
    ticker.paste(viewport, (75, 4))
    return ticker


def display_content_bounds(state, now, timer_progress=None, ticker_progress=None):
    """Logical rectangle occupied by the board during strip transitions."""
    top = round(TIMER_STRIP_HEIGHT * _overlay_progress(timer_progress, visible_timers(state, now)))
    bottom = round(SHOPPING_TICKER_HEIGHT * _overlay_progress(ticker_progress, shopping_ticker_names(state)))
    height = BASE_SIZE[1] - top - bottom
    width = height * 2
    return ((BASE_SIZE[0] - width) // 2, top, width, height)


def compose_display_overlays(image, state, now, timer_progress=None, ticker_progress=None):
    """Fit the entire 2:1 content between the timer strip and shopping ticker.

    Progress is supplied by the GUI's monotonic transitions; snapshots use
    the fully open/closed state. Countdown deadlines always use wall time.
    """
    timers = visible_timers(state, now)
    names = shopping_ticker_names(state)
    left, top, width, height = display_content_bounds(state, now, timer_progress, ticker_progress)
    bottom = BASE_SIZE[1] - top - height
    if not top and not bottom:
        return image
    output = Image.new("RGB", BASE_SIZE, BG)
    # Preserve thin font strokes while reducing the board. Nearest-neighbour
    # drops entire one-pixel strokes at this fractional scale.
    content = image.resize((width, height), Image.Resampling.LANCZOS)
    output.paste(content, (left, top))
    if timers and top:
        strip = Image.new("RGB", (BASE_SIZE[0], TIMER_STRIP_HEIGHT), BG)
        draw = ImageDraw.Draw(strip)
        cell_width, gap = 94, 5
        start = (BASE_SIZE[0] - (len(timers) * cell_width + (len(timers) - 1) * gap)) // 2
        for index, timer in enumerate(timers):
            x = start + index * (cell_width + gap)
            color = AMBER if timer["remaining_seconds"] == 0 else CYAN
            draw.rectangle((x, 1, x + cell_width - 1, 32), fill=PANEL, outline=LINE)
            _text(draw, (x + cell_width // 2, 4), timer["label"], WHITE, align="center", max_width=cell_width - 8)
            _text(draw, (x + cell_width // 2, 15), _timer_countdown(timer["remaining_seconds"]), color, scale=2, align="center", max_width=cell_width - 6)
        output.paste(strip.crop((0, TIMER_STRIP_HEIGHT - top, BASE_SIZE[0], TIMER_STRIP_HEIGHT)), (0, 0))
    if names and bottom:
        ticker = _shopping_ticker(names, state.get("shopping_ticker_offset", 0))
        output.paste(ticker.crop((0, 0, BASE_SIZE[0], bottom)), (0, BASE_SIZE[1] - bottom))
    return output


def compose_timer_strip(image, state, now, progress=None):
    """Compatibility helper for callers composing the timer overlay alone."""
    return compose_display_overlays(image, state, now, progress)


def render_frame(state, config, size=BASE_SIZE):
    """Render a snapshot; explicit ``state['now']`` makes output deterministic.

    Schedule times and the header clock use the configured timezone, with its
    abbreviation beside each time group. Missing map
    coordinates are reported rather than guessed. A calendar never becomes
    claimed live tracking: the map stays a planned great-circle route.
    """
    state, config = state or {}, config or {}
    state = dict(state)
    state["display_timezone"] = config.get("timezone", "UTC")
    state.setdefault("demo", bool(config.get("demo", False)))
    now = _dt(state.get("now")) or datetime.now(timezone.utc)
    tz = _tz(config)
    flights = _flights(state, config, now)
    flight = _selected(state, flights)
    image = Image.new("RGB", BASE_SIZE, BG)
    draw = ImageDraw.Draw(image)
    view = state.get("view", "details")
    personal_item = None
    if view == "map":
        _map(draw, image, state, config, now, flight, tz)
    elif view == "schedule":
        _schedule(draw, state, config, now, flights, tz)
    elif view == "nearby":
        from .extra_views import render_nearby
        render_nearby(draw, image, state, config, now)
    elif view == "weather":
        from .extra_views import render_weather
        render_weather(draw, image, state, config, now)
    elif view == "personal":
        personal_item = _selected_personal(state, now)
        if personal_item and personal_item.get("kind") == "dinner" and config.get("kids_filter_enabled") is True:
            context = state.get("household_context") or {}
            if not isinstance(context, dict) or context.get("kids_home") is not True:
                personal_item = None
        _personal(draw, state, config, now, personal_item)
    elif view == "setup":
        _setup(draw, state, config, now)
    elif view == "custody_outlook":
        _custody_outlook(draw, state, config, now)
    elif view == "local_visit":
        _local_visit(draw, state, config, now)
    elif view == "flight_watch":
        _flight_watch(draw, image, state, config, now, tz)
    elif view == "trip_summary":
        _trip_summary(draw, state, config, now)
    elif view == "trip_map":
        _trip_map(draw, image, state, config, now)
    elif view == "home":
        _home(draw, state, config, now)
    elif view in {"message", "inspiration", "health", "layover"}:
        from .household_policy import card_visible
        card = _selected_card(state, view)
        if not card_visible(view, card, state.get("household_context"), config, now):
            _empty_card(draw, state, now, view)
        elif view == "message":
            _message(draw, state, config, now, card)
        elif view == "inspiration":
            _inspiration(draw, image, state, config, now, card)
        elif view == "health":
            _health(draw, state, config, now, card)
        else:
            _layover(draw, state, config, now, card)
    else:
        _details(draw, state, config, now, flight, flights, tz)
    if view not in {"weather", "nearby"}:
        _footer(draw, state, view, now, tz, personal_item)
    if not state.get("defer_display_overlays") and not state.get("defer_timer_overlay"):
        image = compose_display_overlays(image, state, now, state.get("timer_strip_progress"), state.get("shopping_ticker_progress"))
    if tuple(size) != BASE_SIZE:
        image = image.resize(tuple(size), Image.Resampling.NEAREST)
    return image


def demo_snapshot(now=None):
    """Fictional examples relative to now, never copied from a real calendar."""
    now = _dt(now) or datetime.now(timezone.utc)
    start = now.replace(second=0, microsecond=0) + timedelta(hours=2)
    legs = [
        ("DEMO 104", "ATL", "MEM", start, 80, False),
        ("DEMO 105", "MEM", "ATL", start + timedelta(hours=3), 85, False),
        ("DEMO 208", "ATL", "LAX", start + timedelta(days=1), 285, False),
        ("DEMO 209", "LAX", "ATL", start + timedelta(days=1, hours=7), 255, True),
    ]
    flights = [{"id":f"demo-{i}", "number":number, "origin":origin, "destination":destination,
                "start":departure.isoformat(), "end":(departure + timedelta(minutes=duration)).isoformat(),
                "deadhead":deadhead, "source":"SAMPLE CALENDAR", "report":(departure - timedelta(minutes=55)).isoformat()}
               for i, (number, origin, destination, departure, duration, deadhead) in enumerate(legs)]
    return {"flights":flights, "source":"SAMPLE CALENDAR", "last_sync":now.isoformat(),
            "error":None, "demo":True, "now":now.isoformat(), "view":"details"}
