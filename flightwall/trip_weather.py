"""Small, opt-in airport weather batches; no raw reports or private locations.

AWC's structured JSON is documented at https://aviationweather.gov/data/api/.
These are dated observation/forecast summaries, not a dispatch briefing. TAF
alternatives remain separate: a PROB/TEMPO group never becomes a certainty.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from . import __version__
from .config import private_json
from .weather import WeatherFeed

UTC = timezone.utc
POLL_SECONDS = 900
METAR_MAX_AGE = CACHE_MAX_AGE = 7200
TAF_MAX_AGE = 43200
MAX_RESPONSE = 2_000_000
SOURCE_URL = "https://aviationweather.gov/data/api/"
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "maps"
_ICAO = re.compile(r"[A-Z0-9]{4}")
_COVERS = {"SKC", "CLR", "NSC", "NCD", "FEW", "SCT", "BKN", "OVC", "VV"}
_PHENOMENA = {"DZ": "drizzle", "RA": "rain", "SN": "snow", "SG": "snow grains",
              "IC": "ice crystals", "PL": "ice pellets", "GR": "hail", "GS": "small hail",
              "UP": "unknown precipitation", "BR": "mist", "FG": "fog", "FU": "smoke",
              "VA": "volcanic ash", "DU": "dust", "SA": "sand", "HZ": "haze",
              "PY": "spray", "PO": "dust whirls", "SQ": "squalls", "FC": "funnel cloud",
              "SS": "sandstorm", "DS": "duststorm"}
_DESCRIPTORS = {"MI": "shallow", "PR": "partial", "BC": "patches of", "DR": "low drifting",
                "BL": "blowing", "SH": "showers of", "TS": "thunderstorms with", "FZ": "freezing"}


def _now(value=None):
    value = datetime.now(UTC) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Airport weather requires an aware timestamp")
    return value.astimezone(UTC)


def _stamp(value):
    if type(value) in (int, float) and math.isfinite(value):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str) and len(value) <= 40:
        return _now(datetime.fromisoformat(value.replace("Z", "+00:00")))
    raise ValueError("Invalid airport weather timestamp")


def _iso(value):
    return value.isoformat(timespec="seconds")


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


@lru_cache(maxsize=1)
def _references():
    airports = json.loads((ASSETS / "airports.json").read_text(encoding="utf-8"))
    stations = json.loads((ASSETS / "airport-weather-stations.json").read_text(encoding="utf-8"))
    return airports, stations


def select_airports(snapshot, now, timezone="UTC"):
    """Local today/tomorrow and active scheduled layovers, at most four airports.

    Current/next legs precede completed-today legs within the cap. A calendar
    date includes its whole local day, including DST changes; this is never
    evidence of a person's observed whereabouts.
    """
    now = _now(now)
    if not isinstance(snapshot, dict) or snapshot.get("demo", True):
        return []
    try:
        zone = ZoneInfo(timezone)
    except (KeyError, ValueError, TypeError):
        return []
    today = now.astimezone(zone).date()
    days = {today, today + timedelta(days=1)}
    airports, stations = _references()
    legs = []
    flights = snapshot.get("flights", [])
    if not isinstance(flights, list):
        return []
    for flight in flights[:2000]:
        try:
            start, end = _stamp(flight["start"]), _stamp(flight["end"])
            if 0 < (end - start).total_seconds() <= 86400:
                legs.append((start, end, flight))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
    legs.sort(key=lambda row: (row[0], row[1]))
    candidates = []
    # A matching consecutive gap is explicitly schedule context, not presence.
    # Deadheads interrupt the adjacency, so a later matching airport is not used.
    for previous, following in zip(legs, legs[1:]):
        _, arrival, inbound = previous
        departure, _, outbound = following
        if (not inbound.get("deadhead") and not outbound.get("deadhead")
                and inbound.get("destination") == outbound.get("origin")
                and arrival <= now < departure <= arrival + timedelta(hours=72)):
            candidates.append((inbound.get("destination"), "layover"))
    eligible = [(start, end, flight) for start, end, flight in legs if not flight.get("deadhead")
                and (start <= now < end or start.astimezone(zone).date() in days
                     or end.astimezone(zone).date() == today)]
    eligible.sort(key=lambda row: (0 if row[0] <= now < row[1] else 1 if row[0] > now else 2,
                                   row[0] if row[0] > now else -row[1].timestamp()))
    for _, _, flight in eligible:
        candidates.extend(((flight.get("origin"), "origin"), (flight.get("destination"), "destination")))
    result = []
    for code, role in candidates:
        if not isinstance(code, str) or code not in airports:
            continue
        station = stations.get(code)
        if not isinstance(station, str) or not _ICAO.fullmatch(station):
            continue
        existing = next((row for row in result if row["iata"] == code), None)
        if existing:
            if role not in existing["roles"]:
                existing["roles"].append(role)
        elif len(result) < 4:
            result.append({"iata": code, "icao": station, "roles": [role]})
    return result


def _preview_legs(snapshot, config, now):
    if not isinstance(snapshot, dict) or snapshot.get("demo") is not False:
        return []
    try:
        zone = ZoneInfo(config.get("timezone", "UTC"))
    except (KeyError, ValueError, TypeError):
        return []
    legs = []
    today = now.astimezone(zone).date()
    flights = snapshot.get("flights")
    for row in flights[:2000] if isinstance(flights, list) else []:
        if not isinstance(row, dict) or row.get("deadhead") and config.get("show_deadheads", True) is not True:
            continue
        try:
            start, end = _stamp(row.get("start")), _stamp(row.get("end"))
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if not timedelta(0) < end - start <= timedelta(hours=24):
            continue
        if (start <= now < end or start.astimezone(zone).date() == today
                or end <= now and end.astimezone(zone).date() == today):
            return []
        if now < start:
            legs.append((start, end, row))
    from .commute import preview_commute_legs
    legs.extend(preview_commute_legs(snapshot, config, now))
    legs.sort(key=lambda item: (item[0], item[1]))
    if not legs or legs[0][0].astimezone(zone).date() != today + timedelta(days=1):
        return []
    first_day = legs[0][0].astimezone(zone).date()
    return [row for row in legs if row[0].astimezone(zone).date() == first_day]


def trip_preview_eligible(status, config, now):
    """Day-before scheduled travel, with a fresh explicit Chris Home report."""
    from .household_policy import household_context
    now = _now(now)
    if (config.get("demo") is not False or config.get("trip_weather_enabled") is not True
            or config.get("presence_enabled") is not True or status.get("backend_available", True) is not True):
        return False
    presence = status.get("presence")
    chris = presence.get("chris") if isinstance(presence, dict) else None
    if not isinstance(chris, dict) or chris.get("stale") is not False:
        return False
    ttl = config.get("presence_ttl_minutes", 90)
    if not _number(ttl, 5, 1440):
        return False
    try:
        age = (now - _stamp(chris.get("last_seen") or chris.get("observed_at"))).total_seconds()
    except (ValueError, TypeError, OverflowError, OSError):
        return False
    if not -120 <= age < ttl * 60 or not household_context(status, config, now)["chris_home"]:
        return False
    snapshot = status.get("snapshot", status)
    return bool(_preview_legs(snapshot, config, now))


def preview_airports(snapshot, config, now):
    """First scheduled travel date, including only explicit timed commute legs.

    Roster rotation boundaries cannot be recovered from individual titles.
    All clocks remain schedule context and never prove physical presence.
    """
    now = _now(now)
    airports, stations = _references()
    result = []
    for start, end, leg in _preview_legs(snapshot, config, now):
        for code, kind, at in ((leg.get("origin"), "departure", start), (leg.get("destination"), "arrival", end)):
            if not isinstance(code, str) or code not in airports:
                continue
            station = stations.get(code)
            if not isinstance(station, str) or not _ICAO.fullmatch(station):
                continue
            target = next((row for row in result if row["iata"] == code), None)
            if target is None:
                if len(result) >= 4:
                    continue
                target = {"iata": code, "icao": station, "roles": [kind], "windows": []}
                result.append(target)
            elif kind not in target["roles"]:
                target["roles"].append(kind)
            window = {"kind": kind, "scheduled_at": _iso(at), "start": _iso(at - timedelta(hours=1)),
                      "end": _iso(at + timedelta(hours=1))}
            if leg.get("commute_role") in {"primary", "backup"}:
                window["commute_role"] = leg["commute_role"]
            if window not in target["windows"]:
                target["windows"].append(window)
    return result


def request_json(url):
    """Two allowlisted AWC paths, bounded station query, HTTPS, no redirects."""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    ids = query.get("ids", [""])[0].split(",")
    if (parsed.scheme != "https" or parsed.netloc != "aviationweather.gov"
            or parsed.path not in {"/api/data/metar", "/api/data/taf"} or parsed.fragment
            or set(query) != {"ids", "format"} or query.get("format") != ["json"]
            or len(query.get("ids", [])) != 1 or not 1 <= len(ids) <= 4
            or any(not _ICAO.fullmatch(code) for code in ids)):
        raise ValueError("Unexpected airport weather endpoint")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent":
        "FlightWall/" + __version__ + " (https://github.com/buzzman003/flightwall)"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
        if response.geturl() != url:
            raise ValueError("Unexpected airport weather redirect")
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("Airport weather response too large")
        result = json.loads(raw) if raw else []  # AWC 204 means no recent report.
    if not isinstance(result, list) or len(result) > 400:
        raise ValueError("Invalid airport weather response")
    return result


def _decode_token(token):
    if token == "NSW":
        return "no significant weather"
    if token in {"VCSH", "VCTS"}:
        return {"VCSH": "showers nearby", "VCTS": "thunderstorms nearby"}[token]
    if not isinstance(token, str) or not re.fullmatch(r"[-+]?[A-Z]{2,10}", token):
        return None
    prefix = "light " if token.startswith("-") else "heavy " if token.startswith("+") else ""
    token = token.lstrip("-+")
    vicinity = token.startswith("VC")
    token = token[2:] if vicinity else token
    descriptor = _DESCRIPTORS.get(token[:2])
    token = token[2:] if descriptor else token
    parts = [token[i:i + 2] for i in range(0, len(token), 2)]
    if not parts or any(part not in _PHENOMENA for part in parts):
        return None
    return prefix + (descriptor + " " if descriptor else "") + " and ".join(_PHENOMENA[p] for p in parts) + (" nearby" if vicinity else "")


def _conditions(row):
    direction = row.get("wdir")
    if direction != "VRB" and not _number(direction, 0, 360):
        direction = None
    speed = row.get("wspd") if _number(row.get("wspd"), 0, 300) else None
    gust = row.get("wgst") if _number(row.get("wgst"), 0, 300) else None
    vis = row.get("visib")
    greater = isinstance(vis, str) and vis.endswith("+")
    if isinstance(vis, str) and re.fullmatch(r"\d+(?:\.\d+)?\+?", vis):
        vis = float(vis.rstrip("+"))
    if not _number(vis, 0, 200):
        vis, greater = None, False
    clouds = []
    for cloud in row.get("clouds", [])[:12] if isinstance(row.get("clouds"), list) else []:
        if isinstance(cloud, dict) and cloud.get("cover") in _COVERS:
            base = cloud.get("base") if _number(cloud.get("base"), 0, 100000) else None
            clouds.append({"cover": cloud["cover"], "base_ft": base,
                           "type": cloud.get("type") if cloud.get("type") in {"CB", "TCU"} else None})
    codes, unknown = [], False
    text = row.get("wxString")
    if isinstance(text, str):
        for token in text[:120].split()[:12]:
            if _decode_token(token) is None:
                unknown = True
            else:
                codes.append(token)
        unknown = unknown or len(text) > 120
    elif text is not None:
        unknown = True
    temperature = row.get("temp")
    return {"temperature_f": round(temperature * 9 / 5 + 32, 1) if _number(temperature, -100, 65) else None,
            "wind_direction": direction, "wind_speed_knots": speed, "wind_gust_knots": gust,
            "visibility_miles": vis, "visibility_greater_than": greater, "clouds": clouds,
            "vertical_visibility_ft": row.get("vertVis") if _number(row.get("vertVis"), 0, 100000) else None,
            "weather_codes": codes, "unknown_weather": unknown,
            "flight_category": row.get("fltCat") if row.get("fltCat") in {"VFR", "MVFR", "IFR", "LIFR"} else None}


def _validate_conditions(data):
    """Rebuild cached parsed fields through the same bounds; no cached prose."""
    if (not isinstance(data, dict) or type(data.get("unknown_weather")) is not bool
            or type(data.get("visibility_greater_than")) is not bool
            or (data.get("temperature_f") is not None and not _number(data["temperature_f"], -148, 149))):
        raise ValueError("Invalid parsed airport conditions")
    rebuilt = _conditions({"temp": (data["temperature_f"] - 32) * 5 / 9 if data["temperature_f"] is not None else None,
        "wdir": data["wind_direction"], "wspd": data["wind_speed_knots"], "wgst": data["wind_gust_knots"],
        "visib": str(data["visibility_miles"]) + ("+" if data["visibility_greater_than"] else "") if data["visibility_miles"] is not None else None,
        "clouds": [{"cover": c["cover"], "base": c["base_ft"], "type": c["type"]} for c in data["clouds"]],
        "vertVis": data["vertical_visibility_ft"], "wxString": " ".join(data["weather_codes"]), "fltCat": data["flight_category"]})
    rebuilt["unknown_weather"] = data["unknown_weather"]
    if rebuilt != data:
        raise ValueError("Invalid parsed airport conditions")
    return rebuilt


def _present(data):
    result = copy.deepcopy(_validate_conditions(data))
    speed, gust, direction = data["wind_speed_knots"], data["wind_gust_knots"], data["wind_direction"]
    if speed == 0 and not gust:
        wind = "Calm"
    elif speed is not None:
        heading = "Variable" if direction == "VRB" else f"{direction:03.0f} degrees" if direction is not None else "Direction unknown"
        wind = f"{heading} {speed:g} kt" + (f", gusts {gust:g} kt" if gust is not None else "")
    else:
        wind = "Wind not specified"
    visibility = (f"{'>' if data['visibility_greater_than'] else ''}{data['visibility_miles']:g} mi"
                  if data["visibility_miles"] is not None else "Visibility not specified")
    ceilings = [c["base_ft"] for c in data["clouds"] if c["cover"] in {"BKN", "OVC", "VV"} and c["base_ft"] is not None]
    if data["vertical_visibility_ft"] is not None:
        ceilings.append(data["vertical_visibility_ft"])
    ceiling = min(ceilings) if ceilings else None
    weather = [_decode_token(code) for code in data["weather_codes"]]
    if data["unknown_weather"]:
        weather.append("additional conditions undecoded")
    clouds = [{"SKC": "Clear", "CLR": "Clear", "NSC": "No significant clouds", "NCD": "No cloud detected",
               "FEW": "Few clouds", "SCT": "Scattered clouds", "BKN": "Broken clouds", "OVC": "Overcast",
               "VV": "Obscured sky"}[c["cover"]] + (f" {c['base_ft']:g} ft" if c["base_ft"] is not None else "")
              + (f" {c['type']}" if c["type"] else "") for c in data["clouds"]]
    result.update(wind=wind, visibility=visibility, ceiling_ft=ceiling,
                  cloud_summary="; ".join(clouds) or "Clouds not specified", weather=weather,
                  summary="; ".join(weather + clouds) or "Conditions not specified")
    return result


def forecast_category(conditions):
    """FAA ceiling/visibility category, unknown when the parsed data is incomplete."""
    if not isinstance(conditions, dict):
        return None
    supplied = conditions.get("flight_category")
    if supplied in ("VFR", "MVFR", "IFR", "LIFR"):
        return supplied
    clouds = conditions.get("clouds")
    clouds = clouds if isinstance(clouds, list) else []
    ceilings = [row.get("base_ft") for row in clouds if isinstance(row, dict) and row.get("cover") in {"BKN", "OVC", "VV"}]
    vertical = conditions.get("vertical_visibility_ft")
    if vertical is not None:
        ceilings.append(vertical)
    known = [value for value in ceilings if _number(value, 0, 100000)]
    ceiling = min(known) if known else None
    sky_known = bool(clouds or vertical is not None) and len(known) == len(ceilings)
    visibility = conditions.get("visibility_miles")
    vis_known = _number(visibility, 0, 200)
    if vis_known and conditions.get("visibility_greater_than") is True:
        vis_known = visibility >= 5
        visibility = max(visibility, 6) if vis_known else None
    sky_rank = (3 if ceiling < 500 else 2 if ceiling < 1000 else 1 if ceiling <= 3000 else 0) if ceiling is not None else 0
    vis_rank = (3 if visibility < 1 else 2 if visibility < 3 else 1 if visibility <= 5 else 0) if vis_known else 0
    rank = max(sky_rank, vis_rank)
    if rank == 3 or sky_known and vis_known:
        return ("VFR", "MVFR", "IFR", "LIFR")[rank]
    return None


def forecast_window(taf, window, now):
    """All TAF changes within the scheduled +/-1h window, without extrapolation.

    The colored baseline is prevailing only. Temporary, probability and
    becoming groups remain separately labeled alternatives, never certainty.
    """
    now = _now(now)
    unavailable = {"coverage": "unavailable", "segments": [], "conditions": None, "alternatives": []}
    try:
        at, start, end = (_stamp(window[key]) for key in ("scheduled_at", "start", "end"))
        valid_start, valid_end = (_stamp(taf[key]) for key in ("valid_start", "valid_end"))
        checked, issued = (_stamp(taf[key]) for key in ("checked_at", "issued_at"))
        if (taf.get("source") != "AviationWeather.gov" or taf.get("status") != "ready" or taf.get("stale") is not False
                or not 0 <= (now - checked).total_seconds() < CACHE_MAX_AGE
                or not 0 <= (now - issued).total_seconds() < TAF_MAX_AGE
                or not timedelta(0) < valid_end - valid_start <= timedelta(hours=48)
                or now >= valid_end
                or not start == at - timedelta(hours=1) or not end == at + timedelta(hours=1)):
            return unavailable
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return unavailable
    periods = []
    for row in taf.get("periods", [])[:48] if isinstance(taf.get("periods"), list) else []:
        try:
            a, b = _stamp(row["start"]), _stamp(row["end"])
            becoming = _stamp(row["becoming_at"]) if row.get("becoming_at") is not None else None
            if (valid_start <= a < b <= valid_end and a < end and start < b
                    and row.get("change") in ("BASE", "FM", "BECMG", "TEMPO", "PROB", "UNKNOWN")
                    and (becoming is None or a <= becoming <= b)):
                periods.append((a, b, becoming, row))
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError, OSError):
            continue

    def prevailing(moment):
        bases = [row for row in periods if row[0] <= moment < row[1] and row[3]["change"] in ("BASE", "FM")]
        selected = max(bases, key=lambda row: row[0]) if bases else None
        changes = [row for row in periods if row[3]["change"] == "BECMG" and row[2] is not None
                   and row[2] <= moment < row[1] and (selected is None or row[2] >= selected[0])]
        if changes:
            selected = max(changes, key=lambda row: row[2])
        return selected[3] if selected else None

    boundaries = {start, end}
    for boundary in (valid_start, valid_end):
        if start < boundary < end:
            boundaries.add(boundary)
    for a, b, becoming, _ in periods:
        boundaries.update(point for point in (a, b, becoming) if point is not None and start < point < end)
    boundaries = sorted(boundaries)
    segments = []
    for a, b in zip(boundaries, boundaries[1:]):
        conditions = prevailing(a + (b - a) / 2)
        segments.append({"start": _iso(a), "end": _iso(b), "category": forecast_category(conditions)})
    alternatives = []
    for a, b, becoming, row in periods:
        change = row["change"]
        if change in ("BASE", "FM") or change == "BECMG" and becoming is not None and becoming <= start:
            continue
        item = {"change": change, "probability": row.get("probability"), "category": forecast_category(row)}
        if item not in alternatives:
            alternatives.append(item)
    covered = sum((_stamp(row["end"]) - _stamp(row["start"])).total_seconds() for row in segments if row["category"] is not None)
    return {"coverage": "full" if covered == (end - start).total_seconds() else "partial" if covered else "unavailable",
            "segments": segments, "conditions": prevailing(at), "alternatives": alternatives}


def parse_metar(row, station, now):
    now = _now(now)
    if not isinstance(row, dict) or row.get("icaoId") != station:
        raise ValueError("Unexpected METAR station")
    observed = _stamp(row.get("obsTime"))
    if observed > now + timedelta(minutes=5) or observed < now - timedelta(days=7):
        raise ValueError("Invalid METAR observation time")
    return {"icao": station, "observed_at": _iso(observed), "checked_at": _iso(now),
            "conditions": _conditions(row)}


def parse_taf(row, station, now):
    now = _now(now)
    if not isinstance(row, dict) or row.get("icaoId") != station:
        raise ValueError("Unexpected TAF station")
    issued, start, end = (_stamp(row.get(key)) for key in ("issueTime", "validTimeFrom", "validTimeTo"))
    if (issued > now + timedelta(minutes=5) or issued < now - timedelta(days=7)
            or not timedelta(0) < end - start <= timedelta(hours=48)
            or start > now + timedelta(hours=48) or end < now - timedelta(days=7)):
        raise ValueError("Invalid TAF times")
    raw = row.get("fcsts")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 48:
        raise ValueError("Invalid TAF periods")
    periods = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            a, b = _stamp(item.get("timeFrom")), _stamp(item.get("timeTo"))
            if not start <= a < b <= end:
                continue
            change = item.get("fcstChange")
            change = "BASE" if change is None else change if change in {"FM", "BECMG", "TEMPO", "PROB"} else "UNKNOWN"
            probability = item.get("probability") if type(item.get("probability")) is int and 0 <= item["probability"] <= 100 else None
            becoming = _stamp(item["timeBec"]) if item.get("timeBec") is not None else None
            if becoming is not None and not a <= becoming <= b:
                continue
            temperatures = []
            for entry in item.get("temp", [])[:12] if isinstance(item.get("temp"), list) else []:
                if not isinstance(entry, dict) or entry.get("maxOrMin") not in {"MAX", "MIN"}:
                    continue
                try:
                    at = _stamp(entry.get("validTime"))
                    value = entry.get("surfaceTemp")
                    if start <= at < end and _number(value, -100, 65):
                        temperatures.append({"at": _iso(at), "kind": "high" if entry["maxOrMin"] == "MAX" else "low",
                                             "temperature_f": round(value * 9 / 5 + 32, 1)})
                except (ValueError, TypeError, OverflowError, OSError):
                    continue
            periods.append({"start": _iso(a), "end": _iso(b), "becoming_at": _iso(becoming) if becoming else None,
                            "change": change, "probability": probability, "conditions": _conditions(item),
                            "temperatures": temperatures})
        except (ValueError, TypeError, OverflowError, OSError):
            continue
    if not periods:
        raise ValueError("No usable TAF periods")
    return {"icao": station, "issued_at": _iso(issued), "valid_start": _iso(start), "valid_end": _iso(end),
            "checked_at": _iso(now), "periods": sorted(periods, key=lambda p: (p["start"], p["end"], p["change"]))}


def _report_status(kind, data, station, now):
    if not data:
        return None
    allowed = ({"icao", "observed_at", "checked_at", "conditions"} if kind == "metar" else
               {"icao", "issued_at", "valid_start", "valid_end", "checked_at", "periods"})
    if not isinstance(data, dict) or set(data) != allowed:
        raise ValueError("Unexpected parsed report fields")
    if data.get("icao") != station:
        raise ValueError("Cached station mismatch")
    checked_age = (now - _stamp(data["checked_at"])).total_seconds()
    fresh = 0 <= checked_age < CACHE_MAX_AGE
    result = {"icao": station, "source": "AviationWeather.gov", "source_url": SOURCE_URL,
              "checked_at": data["checked_at"]}
    if kind == "metar":
        age = (now - _stamp(data["observed_at"])).total_seconds()
        result.update(_present(data["conditions"]), observed_at=data["observed_at"])
        fresh = fresh and 0 <= age < METAR_MAX_AGE
    else:
        issued, start, end = (_stamp(data[key]) for key in ("issued_at", "valid_start", "valid_end"))
        if not timedelta(0) < end - start <= timedelta(hours=48):
            raise ValueError("Invalid cached TAF validity")
        fresh = fresh and 0 <= (now - issued).total_seconds() < TAF_MAX_AGE and now < end
        if not isinstance(data["periods"], list) or not 1 <= len(data["periods"]) <= 48:
            raise ValueError("Invalid cached TAF periods")
        periods = []
        for period in data["periods"][:48]:
            if not isinstance(period, dict) or set(period) != {"start", "end", "becoming_at", "change", "probability", "conditions", "temperatures"}:
                raise ValueError("Unexpected parsed TAF period fields")
            a, b = _stamp(period["start"]), _stamp(period["end"])
            if not start <= a < b <= end or period["change"] not in {"BASE", "FM", "BECMG", "TEMPO", "PROB", "UNKNOWN"}:
                raise ValueError("Invalid cached TAF period")
            probability = period["probability"]
            if probability is not None and (type(probability) is not int or not 0 <= probability <= 100):
                raise ValueError("Invalid TAF probability")
            becoming = _stamp(period["becoming_at"]) if period["becoming_at"] else None
            if becoming is not None and not a <= becoming <= b:
                raise ValueError("Invalid TAF transition")
            label = {"BASE": "Forecast", "FM": "From", "BECMG": "Becoming", "TEMPO": "Temporary",
                     "PROB": "Possible", "UNKNOWN": "Unspecified change"}[period["change"]]
            if probability is not None:
                label += f" ({probability}% probability)"
            conditions = _present(period["conditions"])
            temperatures = period["temperatures"]
            if not isinstance(temperatures, list) or len(temperatures) > 12:
                raise ValueError("Invalid TAF temperatures")
            for temperature in temperatures:
                if (not isinstance(temperature, dict) or set(temperature) != {"at", "kind", "temperature_f"}
                        or temperature["kind"] not in {"high", "low"}
                        or not _number(temperature["temperature_f"], -148, 149)
                        or not start <= _stamp(temperature["at"]) < end):
                    raise ValueError("Invalid TAF temperature")
            periods.append({key: period[key] for key in ("start", "end", "becoming_at", "change", "probability")} |
                           conditions | {"change_label": label, "summary": label + ": " + conditions["summary"],
                                         "temperatures": copy.deepcopy(temperatures)})
        if not periods:
            raise ValueError("Empty TAF")
        result.update(issued_at=data["issued_at"], valid_start=data["valid_start"], valid_end=data["valid_end"], periods=periods)
    result.update(status="ready" if fresh else "stale", stale=not fresh)
    return result


class TripWeatherFeed:
    """Independent worker; status performs no network and returns no coordinates."""

    def __init__(self, data_dir, config_getter):
        self.data_dir, self.config_getter = Path(data_dir), config_getter
        self.path = self.data_dir / "trip-weather.json"
        self._lock, self._poll_lock = threading.RLock(), threading.Lock()
        self._last_attempt, self._identity = -math.inf, None
        self._targets, self._cache, self._forecasts, self._error = [], {}, {}, None
        try:
            if self.path.stat().st_size <= MAX_RESPONSE:
                document = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(document, dict) and document.get("schema") == 1 and isinstance(document.get("stations"), dict):
                    # Only recognized parsed fields survive a cache reload. A
                    # corrupt/old file cannot cause raw reports to be re-saved.
                    for station, reports in document["stations"].items():
                        if not isinstance(station, str) or not _ICAO.fullmatch(station) or not isinstance(reports, dict):
                            continue
                        for kind in ("metar", "taf"):
                            try:
                                if _report_status(kind, reports.get(kind), station, _now()) is not None:
                                    self._cache.setdefault(station, {})[kind] = reports[kind]
                            except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
                                pass
        except (OSError, ValueError):
            pass

    def _mode(self):
        config = self.config_getter()
        if config.get("demo", True):
            return "demo"
        return "enabled" if config.get("trip_weather_enabled") is True else "off"

    def _forecast(self, target):
        code, station = target["iata"], target["icao"]
        airports, _ = _references()
        airport = airports.get(code, {})
        lat, lon = airport.get("lat"), airport.get("lon")
        if not station.startswith("K") or not _number(lat, 24, 50) or not _number(lon, -125, -66):
            return None
        with self._lock:
            if station not in self._forecasts:
                config = {"demo": False, "weather_enabled": True, "weather_latitude": lat,
                          "weather_longitude": lon, "timezone": "UTC"}
                def settings():
                    current = self.config_getter()
                    return config | {"demo": self._mode() != "enabled", "timezone": current.get("timezone", "UTC")}
                self._forecasts[station] = WeatherFeed(self.data_dir / "trip-weather" / station, settings)
            return self._forecasts[station]

    def poll(self, snapshot, now=None, *, preview=False):
        now = _now(now)
        if not self._poll_lock.acquire(blocking=False):
            return self.status(now)
        try:
            mode = self._mode()
            config = self.config_getter()
            targets = select_airports(snapshot, now, timezone=config.get("timezone", "UTC")) if mode == "enabled" else []
            if mode == "enabled" and preview is True:
                planned = preview_airports(snapshot, config, now)
                codes = {row["iata"] for row in planned}
                targets = (planned + [row for row in targets if row["iata"] not in codes])[:4]
            identity = tuple((row["iata"], row["icao"]) for row in targets)
            with self._lock:
                self._targets = targets
                if identity != self._identity:
                    self._identity, self._last_attempt, self._error = identity, -math.inf, None
                tick = time.monotonic()
                if mode != "enabled" or not targets or tick - self._last_attempt < POLL_SECONDS:
                    return self.status(now)
                self._last_attempt = tick
                cache = copy.deepcopy(self._cache)
            errors, stations = [], sorted({row["icao"] for row in targets})
            for kind, parser in (("metar", parse_metar), ("taf", parse_taf)):
                if self._mode() != "enabled":
                    return self.status(now)
                try:
                    rows = request_json("https://aviationweather.gov/api/data/" + kind + "?" +
                                        urllib.parse.urlencode({"ids": ",".join(stations), "format": "json"}))
                    for station in stations:
                        matching = [row for row in rows if isinstance(row, dict) and row.get("icaoId") == station]
                        if len(matching) != 1:
                            errors.append(kind)
                            continue
                        try:
                            cache.setdefault(station, {})[kind] = parser(matching[0], station, now)
                        except (KeyError, TypeError, ValueError, OverflowError, OSError):
                            errors.append(kind)
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    errors.append(kind)
            for target in targets:
                if self._mode() != "enabled":
                    return self.status(now)
                child = self._forecast(target)
                if child is not None:
                    child.poll(now)
            if self._mode() != "enabled":
                return self.status(now)
            with self._lock:
                self._cache = {station: cache[station] for station in stations if station in cache}
                self._error = "Airport weather update incomplete; cached reports remain dated." if errors else None
                try:
                    private_json(self.path, {"schema": 1, "stations": self._cache})
                except OSError:
                    self._error = "Airport weather cache could not be saved."
            return self.status(now)
        finally:
            self._poll_lock.release()

    def status(self, now=None):
        now, mode = _now(now), self._mode()
        result = {"status": mode, "source": "AviationWeather.gov", "source_url": SOURCE_URL,
                  "checked_at": None, "airports": [], "error": None}
        if mode != "enabled":
            return result
        with self._lock:
            targets, cache, error = copy.deepcopy(self._targets), copy.deepcopy(self._cache), self._error
        if not targets:
            result["status"] = "idle"
            return result
        checks, fresh, missing, stale = [], 0, 0, 0
        for target in targets:
            item = target | {"source_url": SOURCE_URL, "checked_at": None, "metar": None, "taf": None, "forecast": None}
            for kind in ("metar", "taf"):
                try:
                    report = _report_status(kind, cache.get(target["icao"], {}).get(kind), target["icao"], now)
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
                    report = None
                item[kind] = report
                if report:
                    checks.append(report["checked_at"])
                    fresh += not report["stale"]
                    stale += report["stale"]
                else:
                    missing += 1
            child = self._forecast(target)
            if child is not None:
                weather = child.status(now)
                item["forecast"] = {key: weather.get(key) for key in ("status", "source", "source_url", "timezone",
                    "today", "tomorrow", "daily_updated_at", "daily_fetched_at", "error")}
                item["forecast"].update(issued_at=weather.get("daily_updated_at"), checked_at=weather.get("daily_fetched_at"))
            item_checks = [item[kind]["checked_at"] for kind in ("metar", "taf") if item[kind]]
            item["checked_at"] = min(item_checks, key=_stamp) if item_checks else None
            result["airports"].append(item)
        result.update(status="ready" if fresh and not missing and not stale and not error else
                      "partial" if fresh else "stale" if stale else "unavailable",
                      checked_at=min(checks, key=_stamp) if checks else None, error=error)
        return result
