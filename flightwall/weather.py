"""Opt-in NWS forecasts; private cache, two days, and an explicit overnight window.

Provider contract: https://www.weather.gov/documentation/services-web-api and
https://api.weather.gov/openapi.json. Precipitation percentages describe each
hour, not the combined probability of rain during the entire night. Forecast
high/low values are NWS daytime/nighttime periods, not observed temperatures.
"""
from __future__ import annotations

import copy
from datetime import datetime, time as daytime, timedelta, timezone
import hashlib
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

UTC = timezone.utc
POLL_SECONDS = 900
CACHE_MAX_AGE = 7200
FORECAST_MAX_AGE = 43200
MAX_RESPONSE = 2_000_000
SOURCE_URL = "https://www.weather.gov/"
_RAIN = re.compile(r"\b(rain|showers?|drizzle|thunderstorms?)\b", re.I)
_GRID = re.compile(r"/gridpoints/[A-Z]{3}/[0-9]+,[0-9]+/forecast(?:/hourly)?")


def _now(value=None):
    value = datetime.now(UTC) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Weather requires an aware timestamp")
    return value.astimezone(UTC)


def _stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid forecast timestamp")
    return _now(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _iso(value):
    return value.isoformat(timespec="seconds")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _text(value):
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"<[^>]*>", "", value[:2000]).split())[:100]


def _grid_url(value, *, hourly):
    if not isinstance(value, str):
        raise ValueError("Forecast endpoint missing")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "api.weather.gov" or
            parsed.query or parsed.fragment or not _GRID.fullmatch(parsed.path) or
            parsed.path.endswith("/hourly") != hourly):
        raise ValueError("Unexpected forecast endpoint")
    return value


def request_json(url):
    """Fixed HTTPS host, no redirects, bounded response and timeout; no logging."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.weather.gov" or parsed.fragment or parsed.query:
        raise ValueError("Weather requires the official HTTPS endpoint")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = urllib.request.Request(url, headers={
        "User-Agent": "FlightWall/" + __version__ + " (https://github.com/buzzman003/flightwall)",
        "Accept": "application/geo+json",
    })
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
        if response.geturl() != url:
            raise ValueError("Unexpected weather redirect")
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("Weather response too large")
        result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Invalid weather response")
    return result


def _temperature(period):
    value, unit = period.get("temperature"), period.get("temperatureUnit")
    if isinstance(value, dict):
        unit = {"wmoUnit:degC": "C", "wmoUnit:degF": "F"}.get(value.get("unitCode"))
        value = value.get("value")
    if not _number(value) or unit not in {"C", "F"}:
        return None
    fahrenheit = value * 9 / 5 + 32 if unit == "C" else value
    return round(fahrenheit, 1) if -150 <= fahrenheit <= 150 else None


def _parse_feed(document, now, *, hourly):
    props = document.get("properties", {})
    if not isinstance(props, dict):
        raise ValueError("Invalid forecast properties")
    updated = _stamp(props.get("updateTime") or props.get("updated"))
    if (updated - now).total_seconds() > 300:
        raise ValueError("Forecast timestamp is in the future")
    raw = props.get("periods")
    if not isinstance(raw, list) or len(raw) > 200:
        raise ValueError("Invalid forecast periods")
    rows = []
    for period in raw:
        if not isinstance(period, dict):
            continue
        try:
            start, end = _stamp(period.get("startTime")), _stamp(period.get("endTime"))
        except (ValueError, TypeError, OverflowError):
            continue
        duration = (end - start).total_seconds()
        if not 0 < duration <= (3600 if hourly else 86400):
            continue
        if end <= now - timedelta(days=1) or start >= now + timedelta(days=3):
            continue
        probability = period.get("probabilityOfPrecipitation")
        probability = (probability.get("value") if isinstance(probability, dict)
                       and probability.get("unitCode") == "wmoUnit:percent" else None)
        if not _number(probability) or not 0 <= probability <= 100:
            probability = None
        rows.append({"start": _iso(start), "end": _iso(end),
                     "is_daytime": period.get("isDaytime") if type(period.get("isDaytime")) is bool else None,
                     "temperature": _temperature(period), "precipitation_probability": probability,
                     "summary": _text(period.get("shortForecast"))})
    if not rows:
        raise ValueError("No usable forecast periods")
    return {"fetched_at": _iso(now), "updated_at": _iso(updated),
            "periods": sorted(rows, key=lambda row: row["start"])}


def _fresh(feed, now):
    try:
        fetched_age = (now - _stamp(feed["fetched_at"])).total_seconds()
        issued_age = (now - _stamp(feed["updated_at"])).total_seconds()
        return -300 <= fetched_age < CACHE_MAX_AGE and -300 <= issued_age < FORECAST_MAX_AGE
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _validate_cache_feed(feed, *, hourly):
    if not feed:
        return
    _stamp(feed["fetched_at"])
    _stamp(feed["updated_at"])
    rows = feed["periods"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise ValueError("Invalid cached periods")
    for row in rows:
        duration = (_stamp(row["end"]) - _stamp(row["start"])).total_seconds()
        probability, temperature = row["precipitation_probability"], row["temperature"]
        if (not 0 < duration <= (3600 if hourly else 86400)
                or (probability is not None and (not _number(probability) or not 0 <= probability <= 100))
                or (temperature is not None and (not _number(temperature) or not -150 <= temperature <= 150))
                or (row["is_daytime"] is not None and type(row["is_daytime"]) is not bool)
                or not isinstance(row["summary"], str) or row["summary"] != _text(row["summary"])):
            raise ValueError("Invalid cached forecast")


def _daily(feed, date, zone, now):
    rows = [row for row in feed.get("periods", []) if _stamp(row["start"]).astimezone(zone).date() == date]
    days = [row for row in rows if row["is_daytime"] is True]
    nights = [row for row in rows if row["is_daytime"] is False]
    # Ambiguous duplicate daytime/nighttime periods do not invent a high/low.
    day = days[0] if len(days) == 1 else {}
    night = nights[0] if len(nights) == 1 else {}
    has_high, has_low = day.get("temperature") is not None, night.get("temperature") is not None
    # The 12-hour endpoint is a forecast of upcoming periods. Once its daytime
    # period has expired, it may legitimately return only Tonight. Do not turn
    # the remaining hourly temperatures or an older cache into an observed high.
    night_only = (not days and bool(night) and _stamp(night["start"]) <= now < _stamp(night["end"]))
    return {"date": date.isoformat(), "high": day.get("temperature"), "low": night.get("temperature"),
            "temperature_unit": "F", "high_label": "day high", "low_label": "night low",
            "high_status": "available" if has_high else "ambiguous" if len(days) > 1 else "not_provided" if not days else "unknown",
            "low_status": "available" if has_low else "ambiguous" if len(nights) > 1 else "not_provided" if not nights else "unknown",
            "night_only": night_only, "remaining_complete": (has_high and has_low) or (night_only and has_low),
            "summary": day.get("summary") or night.get("summary") or "Forecast unavailable",
            "high_start": day.get("start"), "high_end": day.get("end"),
            "low_start": night.get("start"), "low_end": night.get("end"),
            "stale": not _fresh(feed, now), "complete": has_high and has_low}


def overnight_bounds(now, zone):
    """Use the active night before 08:00, otherwise the next 18:00-08:00 night."""
    local = _now(now).astimezone(zone)
    date = local.date() - timedelta(days=1) if local.hour < 8 else local.date()
    return (datetime.combine(date, daytime(18), zone),
            datetime.combine(date + timedelta(days=1), daytime(8), zone))


def _overnight(feed, zone, now):
    start, end = overnight_bounds(now, zone)
    # Forecast APIs omit elapsed periods. Missing hours earlier tonight do not
    # make the remaining forecast incomplete. Second precision matches the
    # reported coverage_start used by a renderer that can keep aging a frame.
    left = max(start.astimezone(UTC), now.astimezone(UTC).replace(microsecond=0))
    right = end.astimezone(UTC)
    spans, probabilities, rain = [], [], False
    for row in feed.get("periods", []):
        a, b = max(left, _stamp(row["start"])), min(right, _stamp(row["end"]))
        if b <= a:
            continue
        rain = rain or bool(_RAIN.search(row["summary"]))
        if row["precipitation_probability"] is not None:
            spans.append((a, b))
            probabilities.append(row["precipitation_probability"])
    cursor, covered, overlap = left, 0.0, False
    for a, b in sorted(spans):
        if a < cursor:
            overlap = True
        uncovered_start = max(a, cursor)
        if b > uncovered_start:
            covered += (b - uncovered_start).total_seconds()
        cursor = max(cursor, b)
    expected = (right - left).total_seconds()
    complete = covered == expected and not overlap
    peak = max(probabilities) if probabilities else None
    fresh = _fresh(feed, now)
    risk = ("stale" if feed and not fresh else "possible" if rain or (peak is not None and peak >= 20)
            else "low" if complete and fresh else "unknown")
    return {"start": _iso(start), "end": _iso(end), "timezone": str(zone),
            "coverage_start": _iso(left),
            "max_precipitation_probability": peak, "probability_label": "Peak hourly precipitation chance",
            "coverage_complete": complete, "covered_hours": round(covered / 3600, 2),
            "expected_hours": round(expected / 3600, 2), "rain_mentioned": rain,
            "risk": risk, "stale": bool(feed) and not fresh}


class WeatherFeed:
    """One backend poller; status is coordinate-free and performs no network I/O."""

    def __init__(self, data_dir, config_getter):
        self.path = Path(data_dir) / "weather.json"
        self.config_getter = config_getter
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._last_attempt = -math.inf
        self._identity = None
        self._cache = {}
        self._points = None
        self._points_at = -math.inf
        self._error = None
        try:
            if self.path.stat().st_size <= MAX_RESPONSE:
                candidate = json.loads(self.path.read_text())
                if isinstance(candidate, dict) and candidate.get("schema") == 1:
                    self._cache = candidate
        except (OSError, ValueError):
            pass

    def _settings(self):
        config = self.config_getter()
        if config.get("demo", True):
            return "demo", None, None
        if config.get("weather_enabled") is not True:
            return "disabled", None, None
        lat, lon = config.get("weather_latitude"), config.get("weather_longitude")
        try:
            zone = ZoneInfo(config.get("timezone", "UTC"))
        except (KeyError, ValueError, TypeError):
            return "unconfigured", None, None
        if not _number(lat) or not _number(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            return "unconfigured", None, None
        location = f"{lat:.4f},{lon:.4f}"
        identity = hashlib.sha256((location + "|" + str(zone)).encode()).hexdigest()
        return "enabled", (location, identity), zone

    def poll(self, now=None):
        now = _now(now)
        if not self._poll_lock.acquire(blocking=False):
            return self.status(now)
        try:
            mode, settings, zone = self._settings()
            if mode != "enabled":
                return self.status(now)
            location, identity = settings
            tick = time.monotonic()
            with self._lock:
                if identity != self._identity:
                    self._identity, self._last_attempt = identity, -math.inf
                    self._points, self._points_at, self._error = None, -math.inf, None
                if tick - self._last_attempt < POLL_SECONDS:
                    return self.status(now)
                self._last_attempt = tick
                cache = copy.deepcopy(self._cache) if self._cache.get("identity") == identity else {}
            errors = []
            try:
                if self._points is None or tick - self._points_at >= 86400:
                    props = request_json("https://api.weather.gov/points/" + location).get("properties", {})
                    self._points = {"hourly": _grid_url(props.get("forecastHourly"), hourly=True),
                                    "daily": _grid_url(props.get("forecast"), hourly=False)}
                    self._points_at = tick
                for key in ("hourly", "daily"):
                    try:
                        cache[key] = _parse_feed(request_json(self._points[key]), now, hourly=key == "hourly")
                    except (OSError, ValueError, TypeError, KeyError, OverflowError):
                        errors.append(key)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                errors.append("location")
            # A setting change during the bounded network request must not expose
            # the old location's data through the newly selected location.
            current = self._settings()
            if current[0] != "enabled" or current[1] != settings:
                return self.status(now)
            with self._lock:
                self._error = "NWS update unavailable; any cached forecast is dated." if errors else None
                if "hourly" in cache or "daily" in cache:
                    cache.update(schema=1, identity=identity)
                    self._cache = cache
                    try:
                        private_json(self.path, cache)
                    except OSError:
                        self._error = "Weather cache could not be saved."
            return self.status(now)
        finally:
            self._poll_lock.release()

    def status(self, now=None):
        now = _now(now)
        mode, settings, zone = self._settings()
        result = {"status": mode, "source": "National Weather Service", "source_url": SOURCE_URL,
                  "timezone": str(zone) if zone else None, "today": None, "tomorrow": None,
                  "overnight": None, "fetched_at": None, "hourly_updated_at": None,
                  "daily_updated_at": None, "hourly_fetched_at": None, "daily_fetched_at": None, "error": None}
        if mode != "enabled":
            return result
        with self._lock:
            cache = copy.deepcopy(self._cache) if self._cache.get("identity") == settings[1] else {}
            result["error"] = self._error if self._identity == settings[1] else None
        try:
            feeds = {}
            for name in ("hourly", "daily"):
                feed = cache.get(name, {})
                try:
                    if not isinstance(feed, dict):
                        raise ValueError("Invalid cached section")
                    _validate_cache_feed(feed, hourly=name == "hourly")
                    feeds[name] = feed
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    feeds[name] = {}
                    result["error"] = "Part of the stored weather forecast is unavailable."
            hourly, daily = feeds["hourly"], feeds["daily"]
            date = now.astimezone(zone).date()
            result.update(today=_daily(daily, date, zone, now), tomorrow=_daily(daily, date + timedelta(days=1), zone, now),
                          overnight=_overnight(hourly, zone, now),
                          hourly_updated_at=hourly.get("updated_at"), daily_updated_at=daily.get("updated_at"),
                          hourly_fetched_at=hourly.get("fetched_at"), daily_fetched_at=daily.get("fetched_at"))
            stamps = [feed["fetched_at"] for feed in (hourly, daily) if feed]
            result["fetched_at"] = min(stamps, key=_stamp) if stamps else None
            stale = any(feed and not _fresh(feed, now) for feed in (hourly, daily))
            complete = (result["today"]["remaining_complete"] and result["tomorrow"]["complete"]
                        and result["overnight"]["coverage_complete"])
            result["status"] = ("unavailable" if not stamps else "stale" if stale else
                                "ready" if complete and not result["error"] else "partial")
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            # Corrupt caches never become a dry-weather assurance or reveal raw
            # external documents. A successful future poll replaces them.
            result.update(status="unavailable", today=None, tomorrow=None, overnight=None,
                          fetched_at=None, hourly_updated_at=None, daily_updated_at=None,
                          hourly_fetched_at=None, daily_fetched_at=None,
                          error="Stored weather data is unavailable.")
        return result
