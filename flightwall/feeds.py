"""Optional network feeds. Calendar-only operation never calls these services."""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .config import private_json
from .flight_watches import FlightWatchStore, day_bounds, tracking_available
from .flight_alerts import (SOURCE as FLIGHT_SOURCE, choose_tracking_flight, flight_identity, fresh_status,
                            fresh_nearby_route, match_nearby_route, nearby_lookup_identity,
                            match_operating_flight, normalize_operating_status, operating_ident,
                            stamp as status_stamp, valid_us_registration, within_tracking_window)
from .personal import PersonalError, PersonalStore
from .personal_tracking import PersonalFlightTracker


PRICING_CHECKED = datetime(2026, 10, 31, tzinfo=timezone.utc).date()
PRICING_MAX_AGE_DAYS = 90


class PaidLimitError(ValueError):
    """Only fixed reason codes, without key, URL or provider error contents."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__({"pricing_expired": "FlightAware pricing needs review before paid requests can resume",
                          "monthly_limit": "FlightAware monthly app cost limit reached",
                          "daily_limit": "FlightAware daily request limit reached",
                          "nearby_limit": "Nearby metadata allowance reserved for operating flights",
                          "usage_unknown": "Cannot verify the local FlightAware usage limit"}[reason])


def _paid_path(path, *, allow_registration=False):
    """Only the two priced single-result-set routes may reach the network."""
    if not isinstance(path, str) or len(path) > 4096:
        raise ValueError("Unsupported paid endpoint")
    split = urllib.parse.urlsplit(path)
    match = re.fullmatch(r"/flights/([^/]+)(/position)?", split.path)
    if split.scheme or split.netloc or split.fragment or not match:
        raise ValueError("Unsupported paid endpoint")
    ident = urllib.parse.unquote(match[1])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", ident):
        raise ValueError("Unsupported paid flight identity")
    if match[2]:
        if split.query:
            raise ValueError("Unsupported position query")
        return split.path, 10  # $0.010, integer milli-USD.
    parameters = urllib.parse.parse_qs(split.query, keep_blank_values=True, strict_parsing=True)
    if set(parameters) - {"start", "end", "ident_type", "max_pages"} or any(len(value) != 1 for value in parameters.values()):
        raise ValueError("Unsupported paid flight query")
    if parameters.get("max_pages", ["1"]) != ["1"]:
        raise ValueError("Paid queries must request exactly one result set")
    ident_type = parameters.get("ident_type", ["designator"])[0]
    if ident_type not in ({"designator", "registration"} if allow_registration else {"designator"}):
        raise ValueError("Unsupported paid identity type")
    # Static routes such as /flights/search have a different, higher fee.
    # Permit only exact designators and nearby U.S. registrations.
    if ident_type == "registration":
        if not valid_us_registration(ident):
            raise ValueError("Unsupported paid aircraft registration")
    elif not re.fullmatch(r"(?:[A-Z][A-Z0-9]|[0-9][A-Z]|[A-Z]{3})[0-9]{1,5}[A-Z]?", ident):
        raise ValueError("Unsupported paid flight designator")
    for key in ("start", "end"):
        if key in parameters:
            status_stamp(parameters[key][0])
    parameters["max_pages"] = ["1"]
    return split.path + "?" + urllib.parse.urlencode(parameters, doseq=True), 5  # $0.005.


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp needs a timezone")
    return parsed


def request_json(url, headers=None):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Feeds require HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "FlightWall/0.1.0", **(headers or {})})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        if response.geturl().split(":", 1)[0] != "https":
            raise ValueError("Feed redirected outside HTTPS")
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("Feed response is too large")
        return json.loads(raw)


def _is_number(value):
    """JSON booleans and non-finite values are not measurements."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def coordinates(latitude, longitude):
    return _is_number(latitude) and _is_number(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180


NEARBY_MAX_AGE_SECONDS = 120
NEARBY_MAX_ROWS = 40
NEARBY_OUTER_RADIUS_MILES = 30
NEARBY_OUTER_MAX_ROWS = 10
# The deployed OpenAPI (reviewed 2026-11-17) limits unkeyed /v2 requests to
# 500 per provider 24-hour bucket. One shared query every three minutes uses at
# most 480/day; faster polling needs a separately configured source/tier.
# https://api.adsb.lol/api/openapi.json
NEARBY_POLL_SECONDS = 180
NEARBY_BACKOFF_MAX_SECONDS = 1800
METRES_PER_MILE = 1609.344
METRES_PER_NAUTICAL_MILE = 1852


def nearby_distance_miles(latitude, longitude, other_latitude, other_longitude):
    """Horizontal great-circle distance; altitude is not a home-relative height."""
    lat1, lat2 = math.radians(latitude), math.radians(other_latitude)
    dlat = lat2 - lat1
    dlon = math.radians(other_longitude - longitude)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_008.8 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a)))) / METRES_PER_MILE


def _nearby_offset_miles(latitude, longitude, other_latitude, other_longitude, distance):
    """North-up azimuthal offsets without copying the private center into a row."""
    lat1, lat2 = math.radians(latitude), math.radians(other_latitude)
    dlon = math.radians(other_longitude - longitude)
    bearing = math.atan2(math.sin(dlon) * math.cos(lat2),
                         math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return distance * math.sin(bearing), distance * math.cos(bearing)


def _nearby_text(value, pattern):
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if re.fullmatch(pattern, value) else None


def _nearby_number(value, low, high):
    return value if _is_number(value) and low <= value <= high else None


def _nearby_fresh(row, now):
    try:
        return 0 <= (now - parse_time(row["observed_at"])).total_seconds() <= NEARBY_MAX_AGE_SECONDS
    except (KeyError, TypeError, ValueError):
        return False


def _nearby_radius(config):
    radius_miles = config.get("nearby_radius_miles")
    request_radius_nm = None
    if radius_miles is None:
        request_radius_nm = config.get("nearby_radius_nm")
        radius_miles = (request_radius_nm * METRES_PER_NAUTICAL_MILE / METRES_PER_MILE
                        if _is_number(request_radius_nm) else None)
    if not _is_number(radius_miles) or not 0 < radius_miles <= 250 * METRES_PER_NAUTICAL_MILE / METRES_PER_MILE:
        raise ValueError("Nearby traffic radius is invalid.")
    if request_radius_nm is None:
        request_radius_nm = min(250, math.ceil(radius_miles * METRES_PER_MILE / METRES_PER_NAUTICAL_MILE))
    return radius_miles, request_radius_nm


def _nearby_snapshot_time(data, now):
    """Use the provider's clock even for an empty or repeated snapshot."""
    if not isinstance(data, dict) or not isinstance(data.get("ac"), list) or len(data["ac"]) > 5000:
        raise ValueError("Invalid nearby traffic response")
    # v2 `now` is epoch milliseconds. seen_pos refers to that snapshot, not our
    # receipt time; re-dating a cached response would make old aircraft look live.
    stamp = data.get("now")
    if not _is_number(stamp):
        raise ValueError("Missing nearby traffic timestamp")
    try:
        observed_snapshot = datetime.fromtimestamp(stamp / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("Invalid nearby traffic timestamp") from None
    snapshot_age = (now - observed_snapshot).total_seconds()
    if not 0 <= snapshot_age <= NEARBY_MAX_AGE_SECONDS:
        raise ValueError("Nearby traffic snapshot is stale")
    return observed_snapshot


def _nearby_aircraft(data, latitude, longitude, radius_miles, now):
    """Normalize one bounded v2 snapshot, without guessing flight-route metadata."""
    observed_snapshot = _nearby_snapshot_time(data, now)
    snapshot_age = (now - observed_snapshot).total_seconds()
    aircraft = {}
    for item in data["ac"]:
        if not isinstance(item, dict):
            continue
        la, lo = item.get("lat"), item.get("lon")
        ident = _nearby_text(item.get("hex"), r"~?[0-9A-F]{6}")
        if not ident or not coordinates(la, lo) or item.get("alt_baro") == "ground":
            continue
        # `seen` only dates the latest message, which need not contain a position.
        age = item.get("seen_pos")
        if not _is_number(age) or not 0 <= age <= NEARBY_MAX_AGE_SECONDS - snapshot_age:
            continue
        distance = nearby_distance_miles(latitude, longitude, la, lo)
        if distance > radius_miles + 1e-7:
            continue
        east, north = _nearby_offset_miles(latitude, longitude, la, lo, distance)
        callsign = _nearby_text(item.get("flight"), r"[A-Z0-9][A-Z0-9 -]{0,11}")
        registration = _nearby_text(item.get("r"), r"[A-Z0-9][A-Z0-9-]{0,15}")
        observed = observed_snapshot - timedelta(seconds=age)
        row = {"latitude": la, "longitude": lo, "lat": la, "lon": lo,
               "hex": ident.lower(), "callsign": callsign, "registration": registration,
               "aircraft_type": _nearby_text(item.get("t"), r"[A-Z0-9]{1,8}"),
               "flight": callsign or registration or ident,
               "altitude": _nearby_number(item.get("alt_baro"), -2000, 100000),
               "groundspeed": _nearby_number(item.get("gs"), 0, 2000),
               "track": _nearby_number(item.get("track"), 0, 360),
               "source": "ADSB.lol", "observed_at": observed.isoformat(),
               "distance_miles": distance, "east_miles": east, "north_miles": north}
        previous = aircraft.get(ident)
        if previous is None or (observed, -distance) > (previous[0], -previous[1]["distance_miles"]):
            aircraft[ident] = (observed, row)
    # Limit only after exact-radius filtering, deduplication and nearest sorting.
    return sorted((entry[1] for entry in aircraft.values()),
                  key=lambda row: (row["distance_miles"], row["hex"]))[:NEARBY_MAX_ROWS]


def _nearby_retry_seconds(error, now):
    """Honor delta-seconds or HTTP-date Retry-After without logging a URL."""
    if not isinstance(error, urllib.error.HTTPError):
        return 0
    value = error.headers.get("Retry-After") if error.headers else None
    if not isinstance(value, str) or len(value) > 128:
        return 0
    try:
        if re.fullmatch(r"[0-9]{1,10}", value.strip()):
            return min(int(value), 7 * 86400)
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is not None:
            return min(max(0, (stamp - now).total_seconds()), 7 * 86400)
    except (ValueError, OverflowError, TypeError):
        pass
    return 0


class FlightFeeds:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.live = None
        self.nearby = []
        self.error = None
        self.watch_results = {}
        self._watch_cache = {}
        self._watch_cursor = 0
        self.personal_tracking = PersonalFlightTracker(lambda path, config: self._flightaware(path, config), self.data_dir)
        self._last_nearby = -math.inf
        self._nearby_query = None
        self._nearby_completed = (None, [])
        self._nearby_outer_completed = (None, [])
        self._nearby_receipt = (None, None, False)
        self._nearby_retry_at = -math.inf
        self._nearby_failures = 0
        self._nearby_usage_path = self.data_dir / "adsblol-usage.json"
        self._nearby_usage_storage = PersonalStore(self.data_dir, lambda: {})
        self._nearby_usage_storage.lock_path = self.data_dir / "adsblol-usage.lock"
        self._last_live = -math.inf
        self._live_id = None
        self._selected = None
        self._lookup_at = -math.inf
        self._budget_path = self.data_dir / "flightaware-usage.json"
        self._budget_storage = PersonalStore(self.data_dir, lambda: {})
        self._budget_storage.lock_path = self.data_dir / "flightaware-usage.lock"
        self.operating_status = {"status": "idle", "source": FLIGHT_SOURCE}
        self._operating_completed = (None, self.operating_status)
        self._operating_next = "status"
        self._operating_terminal = None
        self._nearby_route_cache = {}
        self._last_nearby_route = -math.inf
        self._nearby_lock = threading.Lock()

    def _allow_paid_request(self, config, path, *, purpose="operating"):
        from decimal import Decimal, InvalidOperation
        _, fee = _paid_path(path, allow_registration=purpose == "nearby")
        if purpose not in {"operating", "nearby"} or purpose == "nearby" and fee != 5:
            raise ValueError("Unsupported paid request purpose")
        today = utcnow().astimezone(timezone.utc).date()
        if not PRICING_CHECKED <= today <= PRICING_CHECKED + timedelta(days=PRICING_MAX_AGE_DAYS):
            raise PaidLimitError("pricing_expired")
        day, month = today.isoformat(), today.strftime("%Y-%m")
        try:
            limit = Decimal(str(config.get("flightaware_monthly_limit_usd", 5)))
            if not limit.is_finite() or not 0 <= limit <= 5 or isinstance(config.get("flightaware_monthly_limit_usd"), bool):
                raise ValueError("Invalid monthly app budget")
            limit_milliusd = int(limit * 1000)
            nearby_limit = Decimal(str(config.get("nearby_enrichment_monthly_limit_usd", 1)))
            if (not nearby_limit.is_finite() or not 0 <= nearby_limit <= 1
                    or isinstance(config.get("nearby_enrichment_monthly_limit_usd"), bool)):
                raise ValueError("Invalid nearby metadata budget")
            nearby_cap = min(int(nearby_limit * 1000), limit_milliusd // 5)
            daily = config["flightaware_daily_limit"]
            if type(daily) is not int or daily < 0:
                raise ValueError("Invalid daily app budget")
            with self._budget_storage._locked():
                try:
                    with self._budget_path.open(encoding="utf-8") as stream:
                        raw = stream.read(8193)
                    if len(raw) > 8192:
                        raise ValueError("Invalid usage file")
                    usage = json.loads(raw)
                except FileNotFoundError:
                    usage = None
                if usage is None:
                    usage = {"schema_version": 2, "day": day, "count": 0, "month": month,
                             "reserved_milliusd": 0, "pricing_checked": PRICING_CHECKED.isoformat()}
                elif isinstance(usage, dict) and set(usage) == {"day", "count"}:
                    # The old daily counter cannot establish earlier spending
                    # in its month. Consume that whole month conservatively.
                    legacy_day, count = usage["day"], usage["count"]
                    if (not isinstance(legacy_day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", legacy_day)
                            or datetime.fromisoformat(legacy_day).date() > today or type(count) is not int or count < 0):
                        raise ValueError("Invalid old usage counter")
                    usage = {"schema_version": 2, "day": legacy_day, "count": count, "month": legacy_day[:7],
                             "reserved_milliusd": max(5000, count * 10), "pricing_checked": PRICING_CHECKED.isoformat()}
                base_fields = {"schema_version", "day", "count", "month", "reserved_milliusd", "pricing_checked"}
                nearby_fields = {"nearby_reserved_milliusd", "nearby_day_count"}
                if (not isinstance(usage, dict) or set(usage) not in (base_fields, base_fields | nearby_fields)
                        or type(usage["schema_version"]) is not int or usage["schema_version"] != 2
                        or type(usage["count"]) is not int or usage["count"] < 0
                        or type(usage["reserved_milliusd"]) is not int or usage["reserved_milliusd"] < usage["count"] * 5
                        or usage["reserved_milliusd"] % 5
                        or not isinstance(usage["day"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", usage["day"])
                        or datetime.fromisoformat(usage["day"]).date() > today or usage["month"] != usage["day"][:7]
                        or usage["pricing_checked"] != PRICING_CHECKED.isoformat()):
                    raise ValueError("Invalid usage counter")
                # Earlier schema2 tracked only operating/public-watch requests.
                # Its validated nominal total is preserved; nearby starts at0.
                if not nearby_fields.intersection(usage):
                    usage.update(nearby_reserved_milliusd=0, nearby_day_count=0)
                if (type(usage["nearby_day_count"]) is not int or not 0 <= usage["nearby_day_count"] <= usage["count"]
                        or type(usage["nearby_reserved_milliusd"]) is not int
                        or usage["nearby_reserved_milliusd"] % 5
                        or not usage["nearby_day_count"] * 5 <= usage["nearby_reserved_milliusd"] <= usage["reserved_milliusd"]):
                    raise ValueError("Invalid nearby usage counter")
                if usage["month"] != month:
                    usage.update(month=month, reserved_milliusd=0, nearby_reserved_milliusd=0)
                if usage["day"] != day:
                    usage.update(day=day, count=0, nearby_day_count=0)
                # Persist migration/rollover even if a limit then blocks this
                # request. A lower configured cap cannot erase reservations.
                if usage["count"] >= daily:
                    private_json(self._budget_path, usage)
                    raise PaidLimitError("daily_limit")
                if usage["reserved_milliusd"] + fee > limit_milliusd:
                    private_json(self._budget_path, usage)
                    raise PaidLimitError("monthly_limit")
                if purpose == "nearby" and (
                        usage["nearby_reserved_milliusd"] + fee > nearby_cap
                        or usage["nearby_day_count"] >= min(20, daily // 5)
                        or usage["reserved_milliusd"] + fee > limit_milliusd * 4 // 5):
                    private_json(self._budget_path, usage)
                    raise PaidLimitError("nearby_limit")
                usage["count"] += 1
                usage["reserved_milliusd"] += fee
                if purpose == "nearby":
                    usage["nearby_day_count"] += 1
                    usage["nearby_reserved_milliusd"] += fee
                private_json(self._budget_path, usage)
        except PaidLimitError:
            raise
        except (OSError, ValueError, TypeError, KeyError, PersonalError, InvalidOperation):
            raise PaidLimitError("usage_unknown") from None

    def _flightaware(self, path, config, *, purpose="operating"):
        path, _ = _paid_path(path, allow_registration=purpose == "nearby")
        key_path = self.data_dir / "flightaware.key"
        try:
            with key_path.open(encoding="utf-8") as stream:
                raw_key = stream.read(4097)
            api_key = raw_key.strip()
        except OSError:
            raise ValueError("FlightAware is enabled; add its API key using store-secret") from None
        if not api_key or len(raw_key) > 4096:
            raise ValueError("FlightAware key is empty")
        self._allow_paid_request(config, path, purpose=purpose)
        return request_json("https://aeroapi.flightaware.com/aeroapi" + path, {"x-apikey": api_key})

    def _set_operating_status(self, config, flight, result):
        self.operating_status = result
        self._operating_completed = (flight_identity(flight, config), result)

    def operating_status_for_config(self, config, flight, now=None):
        """No network; ignore results for another calendar leg/configuration."""
        now = now or utcnow()
        base = {"source": FLIGHT_SOURCE}
        if config.get("demo"):
            return {**base, "status": "demo"}
        if not config.get("flightaware_enabled"):
            return {**base, "status": "off"}
        if not tracking_available(self.data_dir, config):
            return {**base, "status": "unconfigured"}
        if not flight or flight.get("deadhead"):
            return {**base, "status": "idle"}
        if not within_tracking_window(flight, now):
            return {**base, "status": "outside_window"}
        identity, result = self._operating_completed
        if identity != flight_identity(flight, config):
            return {**base, "status": "pending"}
        return fresh_status(result, now)

    def nearby_for_config(self, config, now=None):
        """Publish only a completed observation for the currently selected pin."""
        if not isinstance(config, dict) or config.get("demo") or not config.get("nearby_enabled"):
            return []
        lat, lon = config.get("home_latitude"), config.get("home_longitude")
        if not coordinates(lat, lon):
            return []
        try:
            radius_miles, _ = _nearby_radius(config)
        except ValueError:
            return []
        # One tuple assignment pairs rows with their completed query. An admin
        # may save another pin while the backend's previous HTTP call is pending.
        query, rows = self._nearby_completed
        if query != (lat, lon, radius_miles):
            return []
        now = now or utcnow()
        result = copy.deepcopy([row for row in rows if _nearby_fresh(row, now)])
        if config.get("nearby_enrichment_enabled") is True and tracking_available(self.data_dir, config):
            for row in result:
                entry = self._nearby_route_cache.get(self._nearby_route_identity(row), {})
                candidate = dict(row, route=entry.get("route"))
                route = fresh_nearby_route(candidate, now)
                if route:
                    row["route"] = route
        return result

    def nearby_outer_for_config(self, config, now=None):
        """Fresh surrounding context is never evidence of an empty/stale ring."""
        now = now or utcnow()
        if not isinstance(config, dict) or self.nearby_status_for_config(config, now) != "ready":
            return []
        radius, _ = _nearby_radius(config)
        query = (config.get("home_latitude"), config.get("home_longitude"), radius)
        inner_query, inner_rows = self._nearby_completed
        outer_query, rows = self._nearby_outer_completed
        # A nonempty accepted snapshot whose positions later expire must not
        # turn into an allegedly observed empty sky while the card is paused.
        if inner_query != query or inner_rows or outer_query != query:
            return []
        result = copy.deepcopy([row for row in rows if _nearby_fresh(row, now)
                                and radius < row["distance_miles"] <= NEARBY_OUTER_RADIUS_MILES + 1e-7])
        if config.get("nearby_enrichment_enabled") is True and tracking_available(self.data_dir, config):
            for row in result:
                entry = self._nearby_route_cache.get(self._nearby_route_identity(row), {})
                route = fresh_nearby_route(dict(row, route=entry.get("route")), now)
                if route:
                    row["route"] = route
        return result[:NEARBY_OUTER_MAX_ROWS]

    def nearby_status_for_config(self, config, now):
        if config.get("demo") or not config.get("nearby_enabled"):
            return "off"
        lat, lon = config.get("home_latitude"), config.get("home_longitude")
        try:
            radius, _ = _nearby_radius(config)
        except ValueError:
            return "unavailable"
        query, checked, accepted = self._nearby_receipt
        if not coordinates(lat, lon) or query != (lat, lon, radius) or checked is None:
            return "unavailable"
        inner_query, inner_rows = self._nearby_completed
        if accepted and inner_query == query and inner_rows and not any(_nearby_fresh(row, now) for row in inner_rows):
            # Expired known positions are not a successful empty-sky observation.
            return "stale"
        return ("ready" if accepted and timedelta(0) <= now - checked <= timedelta(seconds=NEARBY_MAX_AGE_SECONDS)
                else "stale")

    def _read_nearby_usage(self, now):
        """Private request timestamps only; no home pin or aircraft history."""
        try:
            with self._nearby_usage_path.open(encoding="utf-8") as stream:
                raw = stream.read(32769)
            usage = json.loads(raw) if len(raw) <= 32768 else None
        except FileNotFoundError:
            return {"schema_version": 1, "requested_at": [], "retry_until": None}
        if (not isinstance(usage, dict) or set(usage) != {"schema_version", "requested_at", "retry_until"}
                or type(usage["schema_version"]) is not int or usage["schema_version"] != 1
                or not isinstance(usage["requested_at"], list) or len(usage["requested_at"]) > 480):
            raise ValueError("Nearby request allowance is unavailable")
        stamps = [parse_time(value) for value in usage["requested_at"]]
        if any(stamp > now for stamp in stamps) or stamps != sorted(set(stamps)):
            raise ValueError("Nearby request allowance is unavailable")
        retry = usage["retry_until"]
        if retry is not None and parse_time(retry) > now + timedelta(days=7):
            raise ValueError("Nearby retry clock is invalid")
        usage["requested_at"] = [stamp.isoformat() for stamp in stamps if now - stamp < timedelta(days=1)]
        return usage

    def _reserve_nearby_request(self, now):
        """A restart/config change cannot reset cadence or the daily allowance."""
        with self._nearby_usage_storage._locked():
            usage = self._read_nearby_usage(now)
            waits = [0]
            if usage["retry_until"] is not None:
                waits.append((parse_time(usage["retry_until"]) - now).total_seconds())
            stamps = usage["requested_at"]
            if stamps:
                waits.append(NEARBY_POLL_SECONDS - (now - parse_time(stamps[-1])).total_seconds())
            if len(stamps) >= 480:
                waits.append((parse_time(stamps[0]) + timedelta(days=1) - now).total_seconds())
            wait = max(waits)
            if wait > 0:
                return wait
            usage["requested_at"].append(now.isoformat())
            usage["retry_until"] = None
            # Reserve before the request, including errors/timeouts, just as
            # the provider does. Fail closed if its private journal is corrupt.
            private_json(self._nearby_usage_path, usage)
            return 0

    def _save_nearby_retry(self, now, seconds):
        try:
            with self._nearby_usage_storage._locked():
                usage = self._read_nearby_usage(now)
                until = now + timedelta(seconds=seconds)
                if usage["retry_until"] is not None:
                    until = max(until, parse_time(usage["retry_until"]))
                usage["retry_until"] = until.isoformat()
                private_json(self._nearby_usage_path, usage)
        except (OSError, ValueError, TypeError, PersonalError):
            pass  # Keep the in-memory cooldown; never overwrite invalid usage.

    @staticmethod
    def _nearby_route_identity(row):
        return row.get("hex"), nearby_lookup_identity(row), row.get("registration") or None

    def _poll_nearby_routes(self, config, now):
        """Last priority: one closest-aircraft metadata lookup per five minutes."""
        if config.get("nearby_enrichment_enabled") is not True or not tracking_available(self.data_dir, config):
            return
        tick = time.monotonic()
        # Bound in-memory cache only; no aircraft/history/provider JSON files.
        self._nearby_route_cache = {key: value for key, value in self._nearby_route_cache.items()
                                   if 0 <= tick - value["checked_at"] <= 1800}
        if tick - self._last_nearby_route < 300:
            return
        closest = sorted((row for row in self.nearby if _nearby_fresh(row, now)),
                         key=lambda row: (row["distance_miles"], row["hex"]))[:3]
        for row in closest:
            lookup = nearby_lookup_identity(row)
            if not lookup:
                continue
            ident_type, ident = lookup
            identity = self._nearby_route_identity(row)
            entry = self._nearby_route_cache.get(identity)
            if entry and tick - entry["checked_at"] < (600 if entry.get("route") else 1800):
                continue
            self._last_nearby_route = tick
            entry = {"checked_at": tick, "route": None}
            self._nearby_route_cache[identity] = entry
            if len(self._nearby_route_cache) > 128:
                oldest = min(self._nearby_route_cache, key=lambda key: self._nearby_route_cache[key]["checked_at"])
                self._nearby_route_cache.pop(oldest)
            try:
                params = urllib.parse.urlencode({"start": (now - timedelta(hours=24)).isoformat(),
                                                 "end": now.isoformat(), "ident_type": ident_type, "max_pages": 1})
                response = self._flightaware("/flights/" + ident + "?" + params, config, purpose="nearby")
                entry["route"] = match_nearby_route(response, row, utcnow())
            except (ValueError, OSError, urllib.error.URLError, KeyError, TypeError, AttributeError, OverflowError):
                pass  # Unknown route metadata must not disturb the owner's feed.
            break

    def poll(self, config, selected, *, on_duty=False, watches=None, personal_flights=None):
        """Called by one backend thread; failures never erase the calendar."""
        self.error = None
        now = utcnow()
        try:
            self._poll_live(config, selected, now)
        except (ValueError, OSError, urllib.error.URLError, KeyError, TypeError, AttributeError, OverflowError) as error:
            self.error = "Live flight lookup unavailable; showing the calendar schedule. Check your airline code, API key, account and request limit."
            previous = fresh_status(self.operating_status, now)
            reason = error.reason if isinstance(error, PaidLimitError) else "provider_unavailable"
            result = (previous if previous.get("status") == "unconfigured" and previous.get("reason") == "airline_required" else
                      {**previous, "status": "stale", "reason": reason} if previous.get("fetched_at") else
                      {"status": "unavailable", "source": FLIGHT_SOURCE, "reason": reason})
            if reason == "pricing_expired":
                result["message"] = "FlightAware pricing needs review before paid requests can resume."
            self._set_operating_status(config, selected, result)
        if self.live:
            try:
                if not 0 <= (now - parse_time(self.live["observed_at"])).total_seconds() <= 300:
                    self.live = None
            except (ValueError, KeyError):
                self.live = None
        # The operating flight always gets the first use of the existing shared
        # paid-request budget. Public requests never replace its live position.
        self.personal_tracking.poll(config, personal_flights or [], now, time.monotonic())
        self._poll_watches(config, watches or [], now)
        if not self._nearby_lock.acquire(blocking=False):
            return
        try:
            self._poll_nearby(config, now, on_duty=on_duty)
        finally:
            self._nearby_lock.release()

    def _poll_nearby(self, config, now, *, on_duty=False):
        if config["demo"] or not config["nearby_enabled"]:
            self.nearby = []
            self._nearby_completed = (None, [])
            self._nearby_outer_completed = (None, [])
            self._nearby_receipt = (None, None, False)
            self._last_nearby = -math.inf
            return
        lat, lon = config["home_latitude"], config["home_longitude"]
        if not coordinates(lat, lon):
            self.nearby = []
            self._nearby_completed = (None, [])
            self._nearby_outer_completed = (None, [])
            return
        try:
            radius_miles, request_radius_nm = _nearby_radius(config)
        except ValueError:
            self.nearby = []
            self._nearby_completed = (None, [])
            self._nearby_outer_completed = (None, [])
            self.error = "Nearby traffic radius is invalid."
            return
        query = (lat, lon, radius_miles)
        if query != self._nearby_query:
            self.nearby = []
            self._nearby_completed = (None, [])
            self._nearby_outer_completed = (None, [])
            self._nearby_query = query
            self._last_nearby = -math.inf
        self.nearby = [row for row in self.nearby if _nearby_fresh(row, now)]
        tick = time.monotonic()
        if tick - self._last_nearby < NEARBY_POLL_SECONDS or tick < self._nearby_retry_at:
            if not on_duty:
                self._poll_nearby_routes(config, now)
            return
        try:
            wait = self._reserve_nearby_request(now)
            if wait > 0:
                self._nearby_retry_at = tick + wait
                return
            self._last_nearby = tick
            # The community ADS-B feed is optional and has no availability guarantee.
            # One circle supplies both areas without an extra metered request.
            request_radius_nm = max(request_radius_nm, math.ceil(NEARBY_OUTER_RADIUS_MILES *
                                                                METRES_PER_MILE / METRES_PER_NAUTICAL_MILE))
            data = request_json(f"https://api.adsb.lol/v2/point/{lat}/{lon}/{request_radius_nm}")
            checked = utcnow()
            snapshot_time = _nearby_snapshot_time(data, checked)
            rows = _nearby_aircraft(data, lat, lon, max(radius_miles, NEARBY_OUTER_RADIUS_MILES), checked)
            self.nearby = [row for row in rows if row["distance_miles"] <= radius_miles + 1e-7]
            outer = [row for row in rows if radius_miles < row["distance_miles"] <= NEARBY_OUTER_RADIUS_MILES + 1e-7]
            self._nearby_completed = (query, copy.deepcopy(self.nearby))
            self._nearby_outer_completed = (query, copy.deepcopy(outer[:NEARBY_OUTER_MAX_ROWS]))
            self._nearby_receipt = (query, snapshot_time, True)
            self._nearby_failures = 0
            self._nearby_retry_at = -math.inf
        except (ValueError, OSError, urllib.error.URLError, TypeError, AttributeError, PersonalError) as error:
            self.nearby = []
            self._nearby_completed = (None, [])
            self._nearby_outer_completed = (None, [])
            previous_query, checked, _ = self._nearby_receipt
            self._nearby_receipt = (query, checked if previous_query == query else None, False)
            self._nearby_failures = min(self._nearby_failures + 1, 5)
            backoff = min(NEARBY_BACKOFF_MAX_SECONDS, NEARBY_POLL_SECONDS * 2 ** (self._nearby_failures - 1))
            if isinstance(error, urllib.error.HTTPError) and 400 <= error.code < 500 and error.code not in {408, 429}:
                backoff = NEARBY_BACKOFF_MAX_SECONDS
            backoff = max(backoff, _nearby_retry_seconds(error, now))
            self._nearby_retry_at = time.monotonic() + backoff
            self._save_nearby_retry(utcnow(), backoff)
            self.error = "Nearby traffic feed unavailable."
        if not on_duty:
            self._poll_nearby_routes(config, utcnow())

    @staticmethod
    def _watch_flight(data, watch):
        if not isinstance(data, dict) or not isinstance(data.get("flights"), list) or len(data["flights"]) > 1000:
            raise ValueError("Invalid public flight result")
        links = data.get("links") or {}
        if not isinstance(links, dict):
            raise ValueError("Invalid pagination")
        # Never buy a second page or silently choose a leg from a partial set.
        if links.get("next") or type(data.get("num_pages", 1)) is not int or data.get("num_pages", 1) != 1:
            return "ambiguous", None
        lower, upper = day_bounds(watch)
        matches = []
        for item in data["flights"]:
            if not isinstance(item, dict):
                continue
            if watch["ident"] not in {item.get("ident"), item.get("ident_iata"), item.get("ident_icao")}:
                continue
            try:
                departure = parse_time(item.get("scheduled_out") or item.get("scheduled_off"))
            except ValueError:
                continue
            if lower <= departure < upper:
                matches.append(item)
        if len(matches) != 1:
            return ("ambiguous" if matches else "not_found"), None
        item = matches[0]
        # FlightAware's cancelled flag also covers other tracking termination.
        # Do not describe it as a confirmed airline cancellation.
        if item.get("blocked") or item.get("cancelled"):
            return "unavailable", None
        identifier = item.get("fa_flight_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", identifier):
            raise ValueError("Missing provider flight identity")
        start = parse_time(item.get("scheduled_out") or item.get("scheduled_off"))
        end = parse_time(item.get("scheduled_in") or item.get("scheduled_on"))
        if not 0 < (end - start).total_seconds() <= 48 * 3600:
            raise ValueError("Invalid public flight interval")
        origin, destination = item.get("origin") or {}, item.get("destination") or {}
        if not isinstance(origin, dict) or not isinstance(destination, dict):
            raise ValueError("Invalid airports")
        origin, destination = origin.get("code_iata"), destination.get("code_iata")
        if not all(isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code) for code in (origin, destination)):
            raise ValueError("Missing airport codes")
        # Project only the fields needed by the TV; discard all other response
        # metadata. These results stay in memory, separate from calendar caches.
        return "scheduled", {"id": identifier, "number": watch["ident"], "origin": origin,
                             "destination": destination, "start": start.isoformat(), "end": end.isoformat(),
                             "deadhead": False, "source": "FlightAware"}

    @staticmethod
    def _watch_position(data, flight, now):
        if not isinstance(data, dict) or not isinstance(data.get("last_position"), dict):
            return None
        position = data["last_position"]
        observed = parse_time(position.get("timestamp"))
        lat, lon = position.get("latitude"), position.get("longitude")
        if not coordinates(lat, lon) or not 0 <= (now - observed).total_seconds() <= 300:
            return None
        # If supplied, the response's association must agree with the queried ID.
        if data.get("fa_flight_id", flight["id"]) != flight["id"]:
            return None
        altitude, speed, heading = position.get("altitude"), position.get("groundspeed"), position.get("heading")
        return {"latitude": lat, "longitude": lon, "observed_at": observed.isoformat(), "flight_id": flight["id"],
                "altitude": altitude * 100 if _is_number(altitude) and -20 <= altitude <= 1000 else None,
                "altitude_units": "feet", "groundspeed": speed if _is_number(speed) and 0 <= speed <= 2000 else None,
                "heading": heading if _is_number(heading) and 0 <= heading <= 360 else None, "source": "FlightAware"}

    def _poll_watches(self, config, watches, now):
        safe = []
        if isinstance(watches, list):
            for watch in watches[:5]:
                try:
                    FlightWatchStore._check_watch(watch)
                    if FlightWatchStore._current(watch, now) and watch["id"] not in {row["id"] for row in safe}:
                        safe.append(watch)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    continue
        wanted = {row["id"] for row in safe}
        self._watch_cache = {key: value for key, value in self._watch_cache.items() if key in wanted}
        if not tracking_available(self.data_dir, config):
            self._watch_cache.clear()
            self.watch_results = {key: {"status": "unconfigured"} for key in wanted}
            return
        tick = time.monotonic()
        for watch in safe:
            identity = watch["ident"], watch["date"], watch["timezone"]
            entry = self._watch_cache.get(watch["id"])
            if entry is None or entry["identity"] != identity:
                entry = self._watch_cache[watch["id"]] = {
                    "identity": identity, "lookup_at": -math.inf, "position_at": -math.inf,
                    "result": {"status": "pending"}}
            result = entry["result"]
            if result.get("live"):
                try:
                    age = (now - parse_time(result["live"]["observed_at"])).total_seconds()
                    if not 0 <= age <= 300:
                        result.pop("live", None)
                        result["status"] = "scheduled"
                except (ValueError, KeyError, TypeError):
                    result.pop("live", None)
                    result["status"] = "scheduled"
        # At most one extra paid request per backend pass, round-robin among
        # at most five watches. Each still shares the normal persisted daily cap.
        ordered = safe[self._watch_cursor % len(safe):] + safe[:self._watch_cursor % len(safe)] if safe else []
        for watch in ordered:
            entry = self._watch_cache[watch["id"]]
            lower, upper = day_bounds(watch)
            # /flights/{ident} only accepts start/end up to two days ahead.
            # Save distant requests, then wait without spending a paid query.
            if upper > now + timedelta(days=2):
                entry["result"] = {"status": "pending"}
                continue
            flight = entry["result"].get("flight")
            lookup_due = tick - entry["lookup_at"] >= (1800 if flight else 300)
            position_due = (flight and parse_time(flight["start"]) - timedelta(hours=1) <= now
                            <= parse_time(flight["end"]) + timedelta(hours=1)
                            and tick - entry["position_at"] >= config["flightaware_poll_seconds"])
            if not lookup_due and not position_due:
                continue
            self._watch_cursor = (safe.index(watch) + 1) % len(safe)
            try:
                if lookup_due:
                    entry["lookup_at"] = tick
                    params = urllib.parse.urlencode({"start": lower.isoformat(), "end": upper.isoformat(),
                                                     "ident_type": "designator", "max_pages": 1})
                    response = self._flightaware("/flights/" + urllib.parse.quote(watch["ident"], safe="") + "?" + params, config)
                    status, resolved = self._watch_flight(response, watch)
                    old = entry["result"]
                    entry["result"] = {"status": status}
                    if resolved:
                        entry["result"]["flight"] = resolved
                        if old.get("flight", {}).get("id") == resolved["id"] and old.get("live"):
                            entry["result"].update(status="tracking", live=old["live"])
                        else:
                            entry["position_at"] = -math.inf
                else:
                    entry["position_at"] = tick
                    response = self._flightaware("/flights/" + urllib.parse.quote(flight["id"], safe="") + "/position", config)
                    live = self._watch_position(response, flight, now)
                    entry["result"] = {"status": "tracking" if live else "scheduled", "flight": flight}
                    if live:
                        entry["result"]["live"] = live
            except (ValueError, OSError, urllib.error.URLError, KeyError, TypeError, AttributeError, OverflowError):
                entry["result"] = {"status": "unavailable", **({"flight": flight} if flight else {})}
            break
        self.watch_results = {watch["id"]: copy.deepcopy(self._watch_cache[watch["id"]]["result"]) for watch in safe}

    def _poll_live(self, config, selected, now):
        if not config["flightaware_enabled"] or not selected or config["demo"] or selected.get("deadhead"):
            self.live = None
            self._selected, self._live_id = None, None
            self._operating_terminal = None
            self._set_operating_status(config, selected, {"status": "demo" if config["demo"] else "off" if not config["flightaware_enabled"] else "idle", "source": FLIGHT_SOURCE})
            return
        identity = flight_identity(selected, config)
        if self._selected != identity:
            self._selected, self._live_id, self._lookup_at = identity, None, -math.inf
            self._last_live = -math.inf
            self.live = None
            self._operating_terminal = None
            self._operating_next = "status"
            self._set_operating_status(config, selected, {"status": "pending", "source": FLIGHT_SOURCE})
        if not tracking_available(self.data_dir, config):
            self.live = None
            self._set_operating_status(config, selected, {"status": "unconfigured", "source": FLIGHT_SOURCE})
            return
        if not within_tracking_window(selected, now):
            self.live = None
            self._set_operating_status(config, selected, {"status": "outside_window", "source": FLIGHT_SOURCE})
            return
        if self._operating_terminal == identity:
            self.live = None
            return
        try:
            number = operating_ident(selected, config.get("airline", ""))
        except ValueError:
            self._set_operating_status(config, selected, {"status": "unconfigured", "source": FLIGHT_SOURCE, "reason": "airline_required"})
            raise
        tick = time.monotonic()
        if tick - self._last_live < config["flightaware_poll_seconds"]:
            return
        if not self._live_id and tick - self._lookup_at < 300:
            return
        self._last_live = tick
        start = status_stamp(selected["start"])
        position_eligible = (self.operating_status.get("phase") != "landed"
                             and (start - timedelta(hours=1) <= now or self.operating_status.get("phase") in {"departed_gate", "airborne"}))
        first = self._live_id is None
        if first or self._operating_next == "status" or not position_eligible:
            self._lookup_at = tick
            params = urllib.parse.urlencode({"start": (start - timedelta(hours=6)).isoformat(),
                                             "end": (start + timedelta(hours=6)).isoformat(),
                                             "ident_type": "designator", "max_pages": 1})
            data = self._flightaware("/flights/" + urllib.parse.quote(number, safe="") + "?" + params, config)
            outcome, matched = match_operating_flight(data, selected, config)
            if matched is None:
                self._live_id, self.live = None, None
                self._set_operating_status(config, selected, {"status": outcome, "source": FLIGHT_SOURCE})
                return
            status = normalize_operating_status(matched, selected, now)
            previous_id = self._live_id
            self._live_id = status["provider_flight_id"]
            if previous_id != self._live_id:
                self.live = None
            self._set_operating_status(config, selected, status)
            if status["phase"] == "landed":
                self.live = None
                self._operating_next = "status"
                return
            if status["phase"] in {"at_gate", "blocked", "tracking_ended"}:
                self._operating_terminal = identity
                self.live = None
                return
            self._operating_next = "position"
            if not first or not position_eligible:
                return
        # The initial association can buy one position immediately. Subsequent
        # passes alternate status/position, one request each poll interval.
        self._operating_next = "status"
        data = self._flightaware("/flights/" + urllib.parse.quote(self._live_id, safe="") + "/position", config)
        live = self._watch_position(data, {"id": self._live_id}, now)
        self.live = ({**live, "flight_id": selected["id"], "provider_flight_id": self._live_id} if live else None)
