"""Dated family-facing flight context; pure pixels, no network or file paths."""
from __future__ import annotations

from datetime import timedelta
import math
import re
import textwrap
from zoneinfo import ZoneInfo

from .time_format import format_time


def _text_value(value, limit=160):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _fresh(value, now, seconds):
    from .render import _dt
    stamp = _dt(value)
    return bool(stamp and 0 <= (now - stamp).total_seconds() < seconds)


def _clock(value, config, date=False):
    from .render import _dt, _tz
    stamp = _dt(value)
    return format_time(stamp.astimezone(_tz(config)), include_zone=True,
                       date_format="%m/%d" if date else None) if stamp else "UNKNOWN"


def _enabled(state, config, flag):
    return bool(config.get(flag) is True and config.get("demo", False) is False
                and state.get("demo", False) is False and state.get("backend_available", True) is True)


def radar_ready(state, config, now):
    meta = state.get("national_radar")
    ready = bool(_enabled(state, config, "national_radar_enabled") and config.get("radar_card_visible", True) is True and isinstance(meta, dict)
                and meta.get("status") == "ready" and meta.get("image_available") is True
                and _fresh(meta.get("fetched_at"), now, 1200)
                and _fresh(meta.get("issued_at") or meta.get("source_time"), now, 1800)
                and meta.get("scope", "national") == config.get("national_radar_scope", "national")
                and isinstance(meta.get("image_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", meta["image_sha256"]))
    if ready and isinstance(meta.get("animation"), dict):
        from .radar import validate_animation, stamp
        try:
            animation = validate_animation(meta["animation"])
            if any(value is not None and not 0 <= (now - stamp(value)).total_seconds() < 7200 for value in animation["observed_at"]):
                return False
        except (ValueError, TypeError, KeyError, OverflowError):
            return False
    return ready


def _lines(draw, xy, value, color, *, width=34, limit=2, max_width=220):
    from .render import _text
    for index, line in enumerate(textwrap.wrap(_text_value(value), width=width)[:limit]):
        _text(draw, (xy[0], xy[1] + index * 12), line, color, max_width=max_width)


def _footer(draw, state, text):
    from .render import LINE, MUTED, _text, deck_footer_width
    draw.line((14, 232, 498, 232), fill=LINE)
    _text(draw, (15, 241), text, MUTED, max_width=deck_footer_width(state))


def _operating_map(draw, image, state, config, now):
    from .operating_map import plan_view, render_map
    from .render import AMBER, CYAN, LINE, MUTED, PANEL, WHITE, _header, _text
    flight, status, live = (state.get(key) for key in ("operating_flight", "operating_status", "operating_live"))
    _header(draw, state, now, "MY FLIGHT / RECEIVED POSITION")
    gates = {"enabled": config.get("flightaware_enabled") is True,
             "demo": config.get("demo", False) is not False or state.get("demo", False) is not False,
             "backend_available": state.get("backend_available", True) is True}
    viewport = plan_view(flight, status, live, now, **gates)
    if viewport is None:
        _text(draw, (256, 105), "WAITING FOR A FRESH FLIGHT POSITION", AMBER, 2, align="center", max_width=472)
        _text(draw, (256, 146), "NO SCHEDULED OR EXTRAPOLATED AIRCRAFT FIX", MUTED, align="center", max_width=472)
        _footer(draw, state, "FLIGHTAWARE / POSITION UNAVAILABLE")
        return
    patch = render_map(flight, status, live, (594, 298), now, **gates)
    if patch is not None:
        image.paste(patch.resize((297, 149)), (14, 56))
    draw.rectangle((318, 56, 498, 204), fill=PANEL, outline=LINE)
    from .render import _flight_designator
    _text(draw, (326, 62), _flight_designator(flight, config), WHITE, 2, max_width=165)
    _text(draw, (326, 82), f"{flight['origin']} > {flight['destination']}", CYAN, 2, max_width=165)
    _text(draw, (326, 99), "SCHED " + _clock(flight.get("start"), config, True), MUTED, max_width=165)
    telemetry = viewport["telemetry"]
    for y, key, label, unit in ((108, "altitude_feet", "ALT", "FT"),
                                (129, "groundspeed_knots", "GS", "KT"),
                                (150, "heading_degrees", "HDG", "DEG")):
        value = telemetry.get(key)
        text = str(round(value)) if type(value) in (int, float) and math.isfinite(value) else "--"
        _text(draw, (326, y), label + " " + text + " " + unit, WHITE, 2, max_width=165)
    _text(draw, (326, 177), f"{viewport['remaining_miles']:.0f} MI TO ARRIVAL", CYAN, max_width=165)
    _text(draw, (326, 191), f"FIX AGE {viewport['position_age_seconds']:.0f} SEC", MUTED, max_width=165)
    _text(draw, (15, 213), "AUTO RANGE / " + viewport["mode"].upper(), CYAN)
    _text(draw, (498, 213), f"{viewport['radius_miles']:.0f} MI", MUTED, align="right")
    _footer(draw, state, "FLIGHTAWARE / FIX " + _clock(viewport["observed_at"], config, True))


def _report_ready(kind, report, station, now):
    from .render import _dt
    if (not isinstance(station, str) or not re.fullmatch(r"[A-Z0-9]{4}", station)
            or not isinstance(report, dict) or report.get("icao") != station or report.get("source") != "AviationWeather.gov"
            or report.get("status") != "ready" or report.get("stale") is not False
            or not _fresh(report.get("checked_at"), now, 7200)):
        return False
    if kind == "metar":
        return _fresh(report.get("observed_at"), now, 7200)
    start, end = _dt(report.get("valid_start")), _dt(report.get("valid_end"))
    return bool(_fresh(report.get("issued_at"), now, 43200) and start and end
                and timedelta(0) < end - start <= timedelta(hours=48) and now < end)


def _taf_groups(taf, now):
    """Select current prevailing and one explicit alternative; count all others.

    An initial BASE may span later FM groups. The latest active FM/BASE wins.
    A completed BECMG can supersede that group without pretending its omitted
    fields were repeated. In-progress BECMG/TEMPO/PROB remain explicit groups.
    """
    from .render import _dt
    valid_start, valid_end = _dt(taf.get("valid_start")), _dt(taf.get("valid_end"))
    if not valid_start or not valid_end:
        return [], 0
    periods = taf.get("periods")
    candidates = []
    for period in periods[:48] if isinstance(periods, list) else []:
        if not isinstance(period, dict):
            continue
        start, end = _dt(period.get("start")), _dt(period.get("end"))
        if (start and end and valid_start <= start < end <= valid_end and now < end
                and period.get("change") in {"BASE", "FM", "BECMG", "TEMPO", "PROB", "UNKNOWN"}):
            candidates.append((start, end, period))
    bases = [row for row in candidates if row[2]["change"] in {"BASE", "FM"} and row[0] <= now]
    prevailing = max(bases, key=lambda row: row[0]) if bases else None
    completed = [row for row in candidates if row[2]["change"] == "BECMG"
                 and _dt(row[2].get("becoming_at")) is not None
                 and row[0] <= _dt(row[2]["becoming_at"]) <= now
                 and (prevailing is None or _dt(row[2]["becoming_at"]) >= prevailing[0])]
    if completed:
        prevailing = max(completed, key=lambda row: _dt(row[2]["becoming_at"]))
    if prevailing is None:
        future = [row for row in candidates if row[2]["change"] in {"BASE", "FM"} and now < row[0]]
        prevailing = min(future, key=lambda row: row[0]) if future else None
    selected = [(prevailing[2], "UPCOMING" if now < prevailing[0] else "AFTER BECMG" if prevailing[2]["change"] == "BECMG" else "PREVAILING")] if prevailing else []
    effective = (_dt(prevailing[2].get("becoming_at")) or prevailing[0]) if prevailing else None
    superseded = [row for row in candidates if row[2]["change"] == "BECMG" and effective is not None
                  and _dt(row[2].get("becoming_at")) is not None and _dt(row[2]["becoming_at"]) < effective
                  and _dt(row[2]["becoming_at"]) <= now]
    alternatives = [row for row in candidates if row is not prevailing and row not in superseded
                    and row[2]["change"] not in {"BASE", "FM"}]
    alternatives.sort(key=lambda row: (row[0] > now, row[0], row[1], row[2]["change"]))
    if alternatives:
        selected.append((alternatives[0][2], "ALTERNATIVE"))
    elif prevailing:
        future = [row for row in candidates if row is not prevailing and row[0] > max(now, prevailing[0])]
        if future:
            selected.append((min(future, key=lambda row: row[0])[2], "UPCOMING"))
    # Superseded BASE/FM groups are not additional forecast scenarios. All
    # remaining alternatives and future changes are counted, never hidden.
    relevant = [row for row in candidates if row not in superseded
                and (row is prevailing or row[2]["change"] not in {"BASE", "FM"} or row[0] > now)]
    return selected[:2], max(0, len(relevant) - len(selected[:2]))


def _taf_label(period, role):
    change = period.get("change")
    if change in {"BASE", "FM"}:
        return role
    label = {"PROB": "POSSIBLE", "TEMPO": "TEMPORARY", "BECMG": "BECOMING", "UNKNOWN": "UNDECODED GROUP"}.get(change, "UNKNOWN GROUP")
    probability = period.get("probability")
    if type(probability) is int and 0 <= probability <= 100:
        label += f" {probability}%"
    return "AFTER BECMG" if role == "AFTER BECMG" else label


def _taf_conditions(period):
    # Put decoded weather first: a narrow display must not bury a thunderstorm
    # alternative behind a verbose wind string. Full groups stay in status.
    weather = period.get("weather")
    parts = [_text_value(value, 60) for value in weather[:4] if isinstance(value, str)] if isinstance(weather, list) else []
    ceiling = period.get("ceiling_ft")
    known_ceiling = type(ceiling) in (int, float) and math.isfinite(ceiling) and 0 <= ceiling <= 100000
    if not parts:
        parts.append(f"CEIL {ceiling:g} FT" if known_ceiling else _text_value(period.get("cloud_summary"), 60) or "Conditions not specified")
    if isinstance(period.get("visibility"), str):
        parts.append("VIS " + _text_value(period["visibility"], 25))
    if weather and known_ceiling:
        parts.append(f"CEIL {ceiling:g} FT")
    parts.append(_compact_wind(period))
    return " / ".join(parts)


def _compact_wind(report):
    speed, gust, direction = (report.get(key) for key in ("wind_speed_knots", "wind_gust_knots", "wind_direction"))
    if type(speed) not in (int, float) or not math.isfinite(speed) or not 0 <= speed <= 300:
        return "Wind not specified"
    gust_ok = type(gust) in (int, float) and math.isfinite(gust) and 0 <= gust <= 300
    if speed == 0 and (not gust_ok or gust == 0):
        return "CALM"
    heading = (f"{direction:03.0f} DEG" if type(direction) in (int, float) and math.isfinite(direction)
               and 0 <= direction <= 360 else "VRB" if direction == "VRB" else "DIR UNKNOWN")
    return heading + f" {speed:g}KT" + (f" G{gust:g}KT" if gust_ok else "")


def _period_span(period, config, now):
    from .render import _dt, _tz
    start, end = _dt(period.get("start")), _dt(period.get("end"))
    if not start or not end:
        return "TIME UNKNOWN"
    start, end = start.astimezone(_tz(config)), end.astimezone(_tz(config))
    left = format_time(start).replace(":00 ", "").replace(" ", "")
    right = format_time(end).replace(":00 ", "").replace(" ", "")
    if start.date() != now.astimezone(_tz(config)).date():
        left = start.strftime("%m/%d ") + left
    if end.date() != start.date():
        right = end.strftime("%m/%d ") + right
    if start.tzname() != end.tzname():
        left += " " + start.tzname()
        right += " " + end.tzname()
        if len(left + "-" + right) > 24:
            return start.strftime("%m/%d") + " DST CHANGE / SEE BRIEF"
    return left + "-" + right


def _trip_weather(draw, image, state, config, now):
    from .trip_weather import trip_weather_eligible
    if not trip_weather_eligible(state, config, now):
        from .render import MUTED, _header, _text
        _header(draw, state, now, "TRIP WEATHER")
        _text(draw, (256, 113), "AT HOME BEFORE TRAVEL OR DURING A VPS OVERNIGHT", MUTED, align="center", max_width=470)
        _footer(draw, state, "SCHEDULED TRAVEL / FRESH HOME REPORT REQUIRED")
        return
    from .render import AMBER, CYAN, LINE, MUTED, PANEL, WHITE, _header, _text, _tz
    source = state.get("trip_weather")
    source = source if isinstance(source, dict) else {}
    card = state.get("selected_weather_airport")
    if not isinstance(card, dict):
        cards = source.get("airports")
        card = next((c for c in cards if isinstance(c, dict)), {}) if isinstance(cards, list) else {}
    if isinstance(card.get("windows"), list):
        _preview_weather(draw, state, config, now, card)
        return
    iata = card.get("iata")
    iata = iata if isinstance(iata, str) and re.fullmatch(r"[A-Z]{3}", iata) else "---"
    station = card.get("icao")
    station = station if isinstance(station, str) and re.fullmatch(r"[A-Z0-9]{4}", station) else "----"
    _header(draw, state, now, "TRIP WEATHER / " + iata + " / " + station)
    enabled = _enabled(state, config, "trip_weather_enabled")
    draw.rectangle((14, 56, 250, 184), fill=PANEL, outline=LINE)
    draw.rectangle((262, 56, 498, 184), fill=PANEL, outline=LINE)
    metar = card.get("metar") if enabled else None
    valid = _report_ready("metar", metar, station, now)
    _text(draw, (22, 63), "METAR / OBSERVATION", CYAN)
    if valid:
        _text(draw, (22, 77), "AT " + _clock(metar.get("observed_at"), config, True), MUTED, max_width=220)
        temp = metar.get("temperature_f")
        label = f"{temp:.0f}F" if type(temp) in (int, float) and math.isfinite(temp) and -148 <= temp <= 149 else "TEMP UNKNOWN"
        category = metar.get("flight_category")
        _text(draw, (22, 91), label + " / " + _text_value(category or "CAT UNKNOWN", 12), _category_color(category), max_width=220)
        _text(draw, (22, 105), "WIND " + _compact_wind(metar), WHITE, max_width=220)
        ceiling = metar.get("ceiling_ft")
        ceil = f"{ceiling:g} FT" if type(ceiling) in (int, float) and math.isfinite(ceiling) else "NOT REPORTED"
        _lines(draw, (22, 119), "VIS " + _text_value(metar.get("visibility")), WHITE, limit=1)
        _lines(draw, (22, 133), "CEILING " + ceil, WHITE, limit=1)
        _lines(draw, (22, 147), metar.get("summary"), MUTED, limit=2)
        _text(draw, (22, 173), "CHECK " + _clock(metar.get("checked_at"), config), MUTED, max_width=220)
    else:
        _text(draw, (22, 92), "OBSERVATION UNAVAILABLE / STALE", AMBER, max_width=220)
        _text(draw, (22, 114), "CURRENT CONDITIONS UNKNOWN", MUTED, max_width=220)
    taf = card.get("taf") if enabled else None
    taf_ok = _report_ready("taf", taf, station, now)
    _text(draw, (270, 63), "TAF / FORECAST", CYAN)
    if taf_ok:
        _text(draw, (270, 77), "ISSUED " + _clock(taf.get("issued_at"), config), MUTED, max_width=220)
        groups, omitted = _taf_groups(taf, now)
        for index, (period, role) in enumerate(groups):
            y = 90 + index * 39
            _text(draw, (270, y), _taf_label(period, role) + " " + _period_span(period, config, now),
                  WHITE if index == 0 else AMBER, max_width=220)
            _lines(draw, (270, y + 12), _taf_conditions(period), WHITE, limit=2)
        if groups:
            _text(draw, (270, 169), f"+{omitted} GROUPS / OFFICIAL BRIEF" if omitted else "FULL DETAILS: OFFICIAL BRIEF", MUTED, max_width=220)
        else:
            _text(draw, (270, 105), "NO USABLE FORECAST PERIOD", AMBER, max_width=220)
    else:
        _text(draw, (270, 92), "FORECAST UNAVAILABLE / STALE", AMBER, max_width=220)
        _text(draw, (270, 114), "FUTURE CONDITIONS UNKNOWN", MUTED, max_width=220)
    forecast = card.get("forecast") if enabled else None
    try:
        forecast_zone = ZoneInfo(forecast.get("timezone")) if isinstance(forecast, dict) else None
    except (KeyError, TypeError, ValueError):
        forecast_zone = None
    for index, name in enumerate(("today", "tomorrow")):
        x = 14 + index * 248
        draw.rectangle((x, 189, x + 236, 229), fill=PANEL, outline=LINE)
        day = forecast.get(name) if isinstance(forecast, dict) else None
        local = now.astimezone(forecast_zone) if forecast_zone else None
        target = (local.date() + timedelta(days=index)).isoformat() if local else None
        good = (isinstance(day, dict) and day.get("date") == target and day.get("stale") is False
                and day.get("temperature_unit") == "F"
                and forecast_zone is not None and forecast.get("status") in {"ready", "partial"}
                and _fresh(forecast.get("checked_at"), now, 7200) and _fresh(forecast.get("issued_at"), now, 43200))
        date_label = target[5:].replace("-", "/") if target else "DATE UNKNOWN"
        zone_label = forecast_zone.key.rsplit("/", 1)[-1].replace("_", " ").upper() if forecast_zone else ""
        _text(draw, (x + 8, 194), name.upper() + " " + date_label + " / NWS " + zone_label, CYAN, max_width=220)
        if good:
            hi, lo = day.get("high"), day.get("low")
            hi = f"{hi:g}" if type(hi) in (int, float) and math.isfinite(hi) else "--"
            lo = f"{lo:g}" if type(lo) in (int, float) and math.isfinite(lo) else "--"
            label = "LOW " + lo + "F" if day.get("night_only") is True else "HI " + hi + " / LO " + lo + "F"
            _text(draw, (x + 8, 205), label, WHITE, max_width=220)
            _lines(draw, (x + 8, 217), day.get("summary"), WHITE, limit=1)
        else:
            _text(draw, (x + 8, 209), "FORECAST UNAVAILABLE", AMBER, max_width=220)
    _footer(draw, state, "AWC / NWS / TIMES " + now.astimezone(_tz(config)).tzname() + " / FAMILY SUMMARY")


def _category_color(category):
    from .render import MUTED
    return {"VFR": (65, 215, 105), "MVFR": (80, 140, 255),
            "IFR": (255, 75, 75), "LIFR": (245, 80, 235)}.get(category, MUTED)


def _nws_preview_day(forecast, scheduled_at, now):
    from .render import _dt
    if (not isinstance(forecast, dict) or forecast.get("source") != "National Weather Service"
            or forecast.get("status") not in {"ready", "partial"}
            or not _fresh(forecast.get("issued_at"), now, 43200)
            or not _fresh(forecast.get("checked_at"), now, 7200)):
        return None
    try:
        day = _dt(scheduled_at).astimezone(ZoneInfo(forecast["timezone"])).date().isoformat()
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    for key in ("today", "tomorrow"):
        candidate = forecast.get(key)
        if (isinstance(candidate, dict) and candidate.get("date") == day and candidate.get("stale") is False
                and candidate.get("temperature_unit") == "F"):
            return candidate
    return None


def _preview_weather(draw, state, config, now, card):
    from .extra_views import _weather_icon, _weather_icon_kind
    from .render import AMBER, BG, CYAN, LINE, MUTED, PANEL, WHITE, _dt, _header, _text
    from .trip_weather import forecast_category, forecast_window, preview_airports
    target = next((row for row in preview_airports(state.get("snapshot", state), config, now)
                   if row["iata"] == card.get("iata") and row["icao"] == card.get("icao")), None)
    title = "VPS OVERNIGHT" if target and target.get("vps_overnight") else "TOMORROW"
    _header(draw, state, now, title + " / " + str(card.get("iata", "---")) + " / AIRPORT WEATHER")
    if target is None:
        _text(draw, (256, 120), "SCHEDULED AIRPORT WINDOW UNAVAILABLE", MUTED, align="center")
        _footer(draw, state, "CALENDAR SCHEDULE / NO FORECAST WINDOW")
        return
    _text(draw, (15, 55), "FORECAST AT SCHEDULED ARRIVAL / DEPARTURE", CYAN)
    for index, category in enumerate(("VFR", "MVFR", "IFR", "LIFR")):
        x = 15 + index * 78
        draw.rectangle((x, 68, x + 5, 73), fill=_category_color(category))
        _text(draw, (x + 10, 67), category, _category_color(category))
    _text(draw, (497, 67), "GRAY = UNKNOWN", MUTED, align="right")
    windows = target["windows"]
    page = state.get("weather_page", 0)
    page = page if type(page) is int and 0 <= page < (len(windows) + 1) // 2 else 0
    taf = card.get("taf")
    taf_ready = _report_ready("taf", taf, target["icao"], now)
    used_daily = False
    for index, window in enumerate(windows[page * 2:page * 2 + 2]):
        y = 82 + index * 66
        draw.rectangle((14, y, 498, y + 61), fill=PANEL, outline=LINE)
        label = window["kind"].upper()
        if window.get("commute_role") in {"primary", "backup"}:
            label = window["commute_role"].upper() + " COMMUTE / " + label
        _text(draw, (22, y + 5), label + " / " + _clock(window["scheduled_at"], config, True), WHITE, max_width=427)
        projected = forecast_window(taf if taf_ready else {}, window, now)
        conditions = projected["conditions"]
        category = forecast_category(conditions)
        daily = _nws_preview_day(card.get("forecast"), window["scheduled_at"], now) if conditions is None else None
        if conditions:
            _weather_icon(draw, (466, y + 3), _weather_icon_kind(conditions.get("summary")))
        elif daily:
            used_daily = True
            _weather_icon(draw, (466, y + 3), _weather_icon_kind(daily.get("summary")))
        if daily:
            _text(draw, (22, y + 19), "NWS DAY / " + _text_value(daily.get("summary")), WHITE,
                  max_width=188 if projected["alternatives"] else 466)
        else:
            _text(draw, (22, y + 19), _compact_wind(conditions or {}), WHITE if conditions else MUTED, max_width=188)
        alternatives = projected["alternatives"]
        if alternatives:
            labels = [_taf_label(row, "ALTERNATIVE") + (" " + row["category"] if row["category"] else "") for row in alternatives]
            _text(draw, (212, y + 19), " / ".join(labels), AMBER, max_width=278)
        elif daily is None:
            _text(draw, (212, y + 19), ("SCHEDULED FORECAST / " + category) if category else "FORECAST CATEGORY UNKNOWN",
                  _category_color(category), max_width=278)
        start, end = _dt(window["start"]), _dt(window["end"])
        for segment in projected["segments"]:
            left = 22 + round((_dt(segment["start"]) - start) / (end - start) * 467)
            right = 22 + round((_dt(segment["end"]) - start) / (end - start) * 467)
            draw.rectangle((left, y + 33, max(left, right - 1), y + 43), fill=_category_color(segment["category"]))
            if segment["category"] and right - left >= 32:
                _text(draw, ((left + right) // 2, y + 35), segment["category"], BG, align="center", max_width=right - left - 4)
        if not projected["segments"] or projected["coverage"] == "unavailable":
            draw.rectangle((22, y + 33, 489, y + 43), fill=BG)
            label = "TAF CATEGORY NOT PROVIDED" if conditions else "TAF NOT YET AVAILABLE FOR THIS WINDOW"
            _text(draw, (256, y + 35), label, MUTED, align="center", max_width=460)
        elif projected["coverage"] == "partial":
            _text(draw, (489, y + 5), "PARTIAL", AMBER, align="right", max_width=56)
        draw.line((256, y + 30, 256, y + 46), fill=WHITE)
        _text(draw, (22, y + 49), _clock(window["start"], config), MUTED, max_width=190)
        _text(draw, (256, y + 49), "SCHED +/-1H", MUTED, align="center", max_width=110)
        _text(draw, (489, y + 49), _clock(window["end"], config), MUTED, align="right", max_width=190)
    note = ("NWS DAY ISSUED " + _clock(card["forecast"].get("issued_at"), config, True) if used_daily
            else "TAF PREVAILING / ALTERNATIVES LABELED")
    _text(draw, (15, 219), note + (f" / {page + 1} OF {(len(windows) + 1) // 2}" if len(windows) > 2 else ""), MUTED, max_width=481)
    _footer(draw, state, "AWC TAF / ISSUED " + _clock(taf.get("issued_at") if taf_ready else None, config, True))


def _radar(draw, image, state, config, now):
    from .render import AMBER, CYAN, MUTED, _header, _text
    from .radar import SCOPE_LABELS
    scope = config.get("national_radar_scope", "national")
    _header(draw, state, now, SCOPE_LABELS.get(scope, "UNKNOWN") + " RADAR / OBSERVATIONS")
    if radar_ready(state, config, now):
        _text(draw, (256, 116), "NOAA OBSERVED RADAR", CYAN, 2, align="center", max_width=472)
        _text(draw, (256, 144), "OBSERVATION LOOP / UTC CLOCK IN IMAGE", MUTED, align="center", max_width=472)
        meta = state["national_radar"]
        clock_label = "NOAA OBS " if meta.get("clock_basis") == "advertised_observation" else "NOAA FILE "
        _footer(draw, state, clock_label + _clock(meta.get("issued_at") or meta.get("source_time"), config)
                + " / CHECK " + _clock(meta.get("fetched_at"), config))
    else:
        _text(draw, (256, 115), "RADAR UNAVAILABLE / STALE", AMBER, 2, align="center", max_width=472)
        _text(draw, (256, 148), "NO CURRENT NATIONAL IMAGE", MUTED, align="center", max_width=472)
        _footer(draw, state, "NOAA / SOURCE TIME REQUIRED")


def _airport_alerts(draw, image, state, config, now):
    from .render import AMBER, MUTED, PANEL, WHITE, _header, _text
    _header(draw, state, now, "FAA / AIRPORT ADVISORIES")
    meta = state.get("airport_alerts")
    valid = (_enabled(state, config, "airport_alerts_enabled") and isinstance(meta, dict)
             and meta.get("status") == "ready" and _fresh(meta.get("fetched_at"), now, 600)
             and _fresh(meta.get("issued_at"), now, 900))
    events = meta.get("events") if valid else []
    labels = {"ground_stop": "GROUND STOP", "ground_delay": "GROUND DELAY", "arrival_delay": "ARRIVAL DELAY", "departure_delay": "DEPARTURE DELAY"}
    rows = [e for e in events if isinstance(e, dict) and e.get("kind") in labels
            and isinstance(e.get("airport"), str) and re.fullmatch(r"[A-Z]{3}", e["airport"])] if isinstance(events, list) else []
    for index, event in enumerate(rows[:3]):
        y = 58 + index * 53
        draw.rectangle((14, y, 498, y + 48), fill=PANEL)
        _text(draw, (22, y + 5), event["airport"], WHITE, 2)
        _text(draw, (80, y + 5), labels[event["kind"]], AMBER, 2, max_width=400)
        _lines(draw, (22, y + 25), event.get("reason"), WHITE, width=75, limit=1, max_width=466)
        _lines(draw, (22, y + 37), event.get("delay_text"), MUTED, width=75, limit=1, max_width=466)
    if not rows:
        _text(draw, (256, 115), "NO CURRENT MATCHED ADVISORY" if valid else "FAA ADVISORIES UNAVAILABLE",
              MUTED if valid else AMBER, 2, align="center", max_width=472)
    _text(draw, (15, 221), "AIRPORT PROGRAM / FLIGHT IMPACT NOT CONFIRMED", AMBER, max_width=482)
    _footer(draw, state, "FAA / UPDATE " + _clock(meta.get("issued_at") if isinstance(meta, dict) else None, config, True))


def render_context(draw, image, state, config, now):
    {"operating_map": _operating_map, "trip_weather": _trip_weather,
     "national_radar": _radar, "airport_alerts": _airport_alerts}[state["view"]](draw, image, state, config, now)
