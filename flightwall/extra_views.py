"""Pure native-pixel weather and nearby-position views; no network or storage.

Render helpers are imported lazily: render_frame dispatches here only after
render.py has initialized. Nearby vectors originate in the feed's private
home-relative projection; the TV never needs the saved home coordinates.
"""
from datetime import timedelta, timezone
import math
import re
import textwrap

from .weather import CACHE_MAX_AGE, FORECAST_MAX_AGE, overnight_bounds
from .time_format import format_time


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _fresh(value, now, seconds):
    from .render import _dt
    stamp = _dt(value)
    return stamp is not None and -300 <= (now - stamp).total_seconds() < seconds


def _temperature(value):
    return str(round(value)) if _number(value) and -150 <= value <= 150 else "--"


def _section_fresh(weather, section, now):
    return (_fresh(weather.get(section + "_fetched_at"), now, CACHE_MAX_AGE)
            and _fresh(weather.get(section + "_updated_at"), now, FORECAST_MAX_AGE))


def _weather_icon_kind(summary, *, night=False):
    """A small visual aid for the supplied summary, never a new forecast."""
    text = summary.casefold() if isinstance(summary, str) else ""
    if re.search(r"\bthunderstorms?\b", text):
        return "thunder"
    if re.search(r"\b(rain|showers?|drizzle)\b", text):
        return "rain"
    if re.search(r"\b(snow|sleet|wintry|flurries)\b", text):
        return "snow"
    if re.search(r"\b(fog|mist|haze)\b", text):
        return "fog"
    if re.search(r"\bpartly\s+(cloudy|sunny)\b", text):
        return "night_partly" if night else "partly"
    if re.search(r"\b(cloudy|overcast)\b", text):
        return "cloud"
    if re.search(r"\b(clear|sunny)\b", text):
        return "moon" if night else "sun"
    return "unknown"


def _weather_icon(draw, xy, kind):
    """Original crisp 24x17 pixel symbols; no glow, font or raster assets."""
    from .render import AMBER, CYAN, MUTED, PANEL, WHITE, _text
    x, y = xy

    def sun():
        draw.ellipse((x + 3, y + 3, x + 9, y + 9), fill=AMBER)
        for a, b in (((6, 0), (6, 1)), ((6, 11), (6, 12)), ((0, 6), (1, 6)), ((11, 6), (12, 6)),
                     ((1, 1), (2, 2)), ((10, 10), (11, 11)), ((1, 11), (2, 10)), ((10, 2), (11, 1))):
            draw.line((x + a[0], y + a[1], x + b[0], y + b[1]), fill=AMBER)

    if kind in {"sun", "partly"}:
        sun()
    elif kind in {"moon", "night_partly"}:
        draw.ellipse((x + 2, y + 1, x + 12, y + 11), fill=AMBER)
        draw.ellipse((x + 6, y - 1, x + 14, y + 8), fill=PANEL)
    if kind in {"cloud", "rain", "thunder", "snow", "fog", "partly", "night_partly"}:
        cloud = WHITE if kind in {"partly", "night_partly"} else MUTED
        draw.ellipse((x + 3, y + 5, x + 12, y + 11), fill=cloud)
        draw.ellipse((x + 8, y + 2, x + 17, y + 10), fill=cloud)
        draw.ellipse((x + 14, y + 5, x + 22, y + 11), fill=cloud)
        draw.rectangle((x + 7, y + 8, x + 19, y + 11), fill=cloud)
    if kind == "rain":
        for dx in (6, 12, 18):
            draw.line((x + dx, y + 13, x + dx - 1, y + 15), fill=CYAN)
    elif kind == "thunder":
        draw.polygon(((x + 12, y + 10), (x + 8, y + 14), (x + 12, y + 14),
                      (x + 10, y + 17), (x + 17, y + 12), (x + 13, y + 12)), fill=AMBER)
    elif kind == "snow":
        for dx in (7, 16):
            draw.line((x + dx - 1, y + 14, x + dx + 1, y + 14), fill=CYAN)
            draw.line((x + dx, y + 13, x + dx, y + 15), fill=CYAN)
    elif kind == "fog":
        draw.line((x + 4, y + 13, x + 20, y + 13), fill=MUTED)
        draw.line((x + 7, y + 16, x + 23, y + 16), fill=MUTED)
    elif kind == "unknown":
        _text(draw, (x + 9, y + 5), "?", MUTED)


def _weather_timing_label(weather, suffix, zone, now):
    """Keep NWS issuance and successful retrieval clocks separate on the TV.

    Rain and daytime summaries are fetched independently. If their clocks
    differ, name the section instead of re-dating old data after a partial
    refresh. Rare cross-date/DST cases show an explicitly conservative oldest
    clock so the complete label still fits beside the deck indicator.
    """
    from .render import _dt
    prefix = "NWS ISSUED" if suffix == "updated_at" else "CHECKED"
    stamps = {key: _dt(weather.get(key + "_" + suffix)) for key in ("hourly", "daily")}
    stamps = {key: stamp for key, stamp in stamps.items() if stamp is not None and stamp <= now + timedelta(minutes=5)}
    if not stamps:
        return prefix + " TIME UNAVAILABLE"
    if len(stamps) == 1:
        key, stamp = next(iter(stamps.items()))
        section = "RAIN" if key == "hourly" else "DAY"
        return prefix + " " + section + " " + format_time(stamp.astimezone(zone), date_format="%m/%d", include_zone=True)
    hourly, daily = stamps["hourly"].astimezone(zone), stamps["daily"].astimezone(zone)
    if stamps["hourly"] == stamps["daily"]:
        return prefix + " " + format_time(hourly, date_format="%m/%d", include_zone=True)
    if hourly.date() == daily.date() and hourly.strftime("%Z") == daily.strftime("%Z"):
        return (prefix + " " + hourly.strftime("%m/%d") + " RAIN " + format_time(hourly).replace(" ", "")
                + " DAY " + format_time(daily).replace(" ", "") + " " + hourly.strftime("%Z"))
    prefix = "NWS OLDEST ISSUE" if suffix == "updated_at" else "OLDEST CHECK"
    return prefix + " " + format_time(min(stamps.values()).astimezone(zone), date_format="%m/%d", include_zone=True)


def render_weather(draw, image, state, config, now):
    from .render import AMBER, CYAN, GREEN, LINE, MUTED, PANEL, WHITE, _dt, _header, _text, _tz, deck_footer_width
    zone = _tz(config)
    local_date = now.astimezone(zone).date()
    _header(draw, state, now, "WEATHER / TODAY + TOMORROW")
    weather = state.get("weather")
    weather = weather if isinstance(weather, dict) else {}
    forced_stale = weather.get("stale") is True or state.get("weather_stale") is True
    enabled = config.get("weather_enabled") is True and not state.get("demo", config.get("demo", False))
    usable = enabled and weather.get("status") not in {"disabled", "demo", "unconfigured", "unavailable", None}
    daily_fresh = usable and not forced_stale and _section_fresh(weather, "daily", now)
    for offset, name, x in ((0, "today", 14), (1, "tomorrow", 262)):
        date = local_date + timedelta(days=offset)
        card = weather.get(name)
        card = card if usable and isinstance(card, dict) else {}
        valid = card.get("date") == date.isoformat()
        stale = valid and (not daily_fresh or card.get("stale") is True)
        low_start, low_end = _dt(card.get("low_start")), _dt(card.get("low_end"))
        night_only = (name == "today" and valid and not stale and card.get("night_only") is True
                      and card.get("high") is None and _number(card.get("low"))
                      and low_start is not None and low_end is not None and low_start <= now < low_end
                      and card.get("temperature_unit") == "F")
        draw.rectangle((x, 56, x + 236, 137), fill=PANEL, outline=LINE)
        _text(draw, (x + 8, 62), "TONIGHT" if night_only else name.upper(), CYAN, 2)
        _text(draw, (x + 228, 65), date.strftime("%m/%d"), MUTED, align="right")
        _weather_icon(draw, (x + 108, 60), _weather_icon_kind(card.get("summary"), night=night_only) if valid and not stale else "unknown")
        color = MUTED if stale else WHITE
        high = _temperature(card.get("high")) if valid else "--"
        low = _temperature(card.get("low")) if valid else "--"
        # Values are already normalized by WeatherFeed. Unknown units never
        # silently turn into Fahrenheit on the TV.
        if card.get("temperature_unit") != "F":
            high = low = "--"
        if night_only:
            _text(draw, (x + 8, 92), "LOW", MUTED)
            _text(draw, (x + 35, 83), low, color, 3, max_width=78)
            _text(draw, (x + 101, 92), "F", MUTED)
        else:
            _text(draw, (x + 8, 92), "HI", MUTED)
            _text(draw, (x + 28, 83), high, color, 3, max_width=78)
            _text(draw, (x + 116, 92), "LO", MUTED)
            _text(draw, (x + 136, 83), low, color, 3, max_width=78)
            _text(draw, (x + 222, 92), "F", MUTED)
        summary = ("STALE FORECAST / CHECK UPDATE" if stale else
                   str(card.get("summary") or "Forecast unavailable") if valid else "Forecast unavailable")
        note = None
        if valid and not stale and not night_only:
            if high == "--" and low == "--":
                note = "TEMPERATURE DATA UNAVAILABLE"
            elif high == "--":
                note = "DAY HIGH NOT PROVIDED"
            elif low == "--":
                note = "NIGHT LOW NOT PROVIDED"
        lines = textwrap.wrap(summary[:160], width=36)[:1 if note else 2]
        if note:
            lines.append(note)
        for line, text in enumerate(lines):
            _text(draw, (x + 8, 113 + line * 12), text, AMBER if stale else WHITE, max_width=220)

    night = weather.get("overnight")
    night = night if usable and isinstance(night, dict) else {}
    expected_start, expected_end = overnight_bounds(now, zone)
    start, end = _dt(night.get("start")), _dt(night.get("end"))
    # Daily data is independent: missing high/low cards must not suppress a
    # fresh hourly rain warning for the active night.
    correct_window = start == expected_start and end == expected_end
    coverage_start = _dt(night.get("coverage_start"))
    correct_coverage_start = (coverage_start is not None
                              and expected_start <= coverage_start < expected_end
                              and coverage_start <= max(expected_start, now) + timedelta(seconds=5))
    expected_hours = (round((expected_end.astimezone(timezone.utc) - coverage_start.astimezone(timezone.utc)).total_seconds() / 3600, 2)
                      if correct_coverage_start else None)
    hourly_fresh = usable and not forced_stale and _section_fresh(weather, "hourly", now) and night.get("stale") is not True
    peak = night.get("max_precipitation_probability")
    peak = peak if _number(peak) and 0 <= peak <= 100 else None
    complete = (correct_coverage_start and night.get("coverage_complete") is True and _number(night.get("covered_hours"))
                and _number(night.get("expected_hours")) and night["covered_hours"] == expected_hours
                and night["expected_hours"] == expected_hours)
    risk = "unknown"
    if correct_window and not hourly_fresh:
        risk = "stale"
    elif correct_window and hourly_fresh:
        if night.get("rain_mentioned") is True or (peak is not None and peak >= 20):
            risk = "possible"
        elif complete and peak is not None:
            risk = "low"
    color = GREEN if risk == "low" else AMBER
    draw.rectangle((14, 145, 498, 224), fill=PANEL, outline=color)
    _text(draw, (22, 153), "OVERNIGHT", CYAN, 2)
    window_label = "NOW-8 AM LOCAL" if now >= expected_start else "6 PM-8 AM LOCAL"
    _text(draw, (490, 156), window_label, MUTED, align="right")
    title = {"possible": "RAIN POSSIBLE", "low": "LOW PRECIP CHANCE", "unknown": "RAIN RISK UNKNOWN", "stale": "FORECAST STALE"}[risk]
    _text(draw, (22, 177), title, color, 2, max_width=296)
    if correct_window and peak is not None:
        _text(draw, (490, 180), "PEAK HOUR " + str(round(peak)) + "%", WHITE, align="right")
    advice = {"possible": "PUT CUSHIONS IN", "low": "NOT A DRY-WEATHER GUARANTEE",
              "unknown": "CHECK UPDATED FORECAST", "stale": "CHECK UPDATED FORECAST"}[risk]
    _text(draw, (22, 201), advice, WHITE, 2 if risk == "possible" else 1, max_width=350)
    if correct_window and not complete:
        _text(draw, (490, 207), "PARTIAL DATA", AMBER, align="right")
    if not usable:
        reason = "WEATHER OFF" if not enabled else "FORECAST UNAVAILABLE"
        _text(draw, (490, 42), reason, AMBER, align="right")
    draw.line((14, 228, 498, 228), fill=LINE)
    timing = weather if usable else {}
    _text(draw, (15, 232), _weather_timing_label(timing, "updated_at", zone, now), MUTED, max_width=deck_footer_width(state))
    _text(draw, (15, 245), _weather_timing_label(timing, "fetched_at", zone, now), MUTED, max_width=deck_footer_width(state))


def _nearby_rows(state, config, now):
    radius = config.get("nearby_radius_miles")
    if radius is None:
        nautical = config.get("nearby_radius_nm")
        radius = nautical * 1.15078 if _number(nautical) else None
    if not _number(radius) or not 0 < radius <= 300:
        return [], None
    if config.get("nearby_enabled") is not True or state.get("demo", config.get("demo", False)):
        return [], radius
    from .render import _dt
    valid = {}
    rows = state.get("nearby")
    for row in rows[:5000] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        stamp = _dt(row.get("observed_at"))
        if stamp is None or not 0 <= (now - stamp).total_seconds() <= 120:
            continue
        east, north = row.get("east_miles"), row.get("north_miles")
        lat, lon = row.get("latitude", row.get("lat")), row.get("longitude", row.get("lon"))
        ident = row.get("hex")
        if (not all(_number(v) for v in (east, north, lat, lon)) or not -90 <= lat <= 90 or not -180 <= lon <= 180
                or not isinstance(ident, str) or not re.fullmatch(r"~?[a-fA-F0-9]{6}", ident)):
            continue
        distance = math.hypot(east, north)
        if distance > radius + 1e-7:
            continue
        previous = valid.get(ident.lower())
        if previous is None or stamp > previous[0]:
            valid[ident.lower()] = (stamp, distance, row)
    return [(item[1], item[2]) for item in sorted(valid.values(), key=lambda item: (item[1], item[2]["hex"]))[:40]], radius


def _metadata(value, maximum=16):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{0,15}", value):
        return None
    return value[:maximum].upper()


def render_nearby(draw, image, state, config, now):
    from .render import AMBER, CYAN, LINE, MUTED, PANEL, WHITE, _header, _text, deck_footer_width
    from .flight_alerts import fresh_nearby_route, nearby_commercial_ident
    _header(draw, state, now, "NEARBY AIRCRAFT")
    rows, radius = _nearby_rows(state, config, now)
    draw.rectangle((14, 56, 310, 204), fill=PANEL, outline=LINE)
    draw.rectangle((318, 56, 498, 204), fill=PANEL, outline=LINE)
    cx, cy, pixels = 162, 131, 64
    for fraction in (.5, 1):
        r = pixels * fraction
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=LINE)
    draw.line((cx - pixels, cy, cx + pixels, cy), fill=LINE)
    draw.line((cx, cy - pixels, cx, cy + pixels), fill=LINE)
    _text(draw, (cx, 59), "N", CYAN, align="center")
    _text(draw, (23, 66), (f"{radius:g} MI" if radius else "RADIUS ?"), MUTED)
    _text(draw, (23, 187), "HOME +", MUTED)
    draw.line((cx - 4, cy, cx + 4, cy), fill=WHITE)
    draw.line((cx, cy - 4, cx, cy + 4), fill=WHITE)
    for index, (distance, row) in reversed(list(enumerate(rows))):
        x = cx + row["east_miles"] / radius * pixels
        y = cy - row["north_miles"] / radius * pixels
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=CYAN if index < 3 else MUTED)
        if index < 3:
            # A number is attached to the received point; no extrapolation,
            # heading vector, inferred airport or invented geography is drawn.
            label_x = min(293, max(20, x + 6))
            label_y = min(189, max(72, y - 8))
            _text(draw, (label_x, label_y), str(index + 1), AMBER, 2)
    route_shown = False
    for index, (distance, row) in enumerate(rows[:3]):
        y = 60 + index * 49
        registration = _metadata(row.get("registration"))
        callsign = _metadata(row.get("callsign")) or _metadata(row.get("flight"))
        aircraft = _metadata(row.get("aircraft_type"), 8)
        label = nearby_commercial_ident(row) or registration or callsign or "ID UNAVAIL"
        type_width = 6 * len(aircraft) + 5 if aircraft else 0
        _text(draw, (326, y), str(index + 1) + " " + label, WHITE, 2, max_width=166 - type_width)
        if aircraft:
            _text(draw, (492, y + 4), aircraft, MUTED, align="right")
        route = fresh_nearby_route(row, now) if config.get("nearby_enrichment_enabled") is True else None
        route_shown = route_shown or route is not None
        route_label = f"{distance:.1f} MI"
        if route:
            route_label = f"{route['origin']} > {route['destination']} / {route_label}"
        _text(draw, (326, y + 18), route_label, CYAN, max_width=166)
        altitude, speed = row.get("altitude"), row.get("groundspeed")
        altitude = str(round(altitude)) + " FT" if _number(altitude) and -2000 <= altitude <= 100000 else "ALT N/A"
        speed = str(round(speed)) + " KT GS" if _number(speed) and 0 <= speed <= 2000 else "GS N/A"
        _text(draw, (326, y + 29), altitude + " / " + speed, WHITE, max_width=166)
        track = row.get("track", row.get("heading"))
        detail = f"TRK {round(track) % 360:03d}" if _number(track) and 0 <= track <= 360 else "TRK N/A"
        if registration and registration != label:
            detail += " / " + registration
        _text(draw, (326, y + 39), detail, MUTED, max_width=166)
    if not rows:
        status = state.get("nearby_status")
        title, subtitle, reason = {
            "ready": ("NO AIRCRAFT", "REPORTED", "IN THIS RADIUS"),
            "stale": ("FEED STALE", "", "WAITING FOR UPDATE"),
            "unavailable": ("FEED", "UNAVAILABLE", "WAITING FOR UPDATE"),
            "off": ("FEED OFF", "", ""),
        }.get(status, ("NO FRESH", "AIRCRAFT", "IN THIS RADIUS"))
        _text(draw, (326, 82), title, AMBER, 2, max_width=166)
        if subtitle:
            _text(draw, (326, 104), subtitle, AMBER, 2, max_width=166)
        if radius is None:
            reason = "SET LOCATION/RADIUS"
        _text(draw, (326, 135), reason, MUTED, max_width=166)
    draw.line((14, 232, 498, 232), fill=LINE)
    sources = "ADSB.LOL / FLIGHTAWARE" if route_shown else "ADSB.LOL"
    _text(draw, (15, 241), f"{len(rows)} FRESH / {sources}", MUTED,
          max_width=deck_footer_width(state))
