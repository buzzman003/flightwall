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
from pathlib import Path

from .config import private_json
from .flight_watches import FlightWatchStore, day_bounds, tracking_available


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


def _nearby_aircraft(data, latitude, longitude, radius_miles, now):
    """Normalize one bounded v2 snapshot, without guessing flight-route metadata."""
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


class FlightFeeds:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.live = None
        self.nearby = []
        self.error = None
        self.watch_results = {}
        self._watch_cache = {}
        self._watch_cursor = 0
        self._last_nearby = -math.inf
        self._nearby_query = None
        self._nearby_completed = (None, [])
        self._last_live = -math.inf
        self._live_id = None
        self._selected = None
        self._lookup_at = -math.inf
        self._budget_path = self.data_dir / "flightaware-usage.json"
        self._lock = threading.Lock()

    def _allow_paid_request(self, config):
        day = utcnow().date().isoformat()
        try:
            usage = json.loads(self._budget_path.read_text())
        except FileNotFoundError:
            usage = {}
        except (OSError, ValueError):
            raise ValueError("Cannot verify the local FlightAware usage limit") from None
        if not isinstance(usage, dict):
            raise ValueError("Cannot verify the local FlightAware usage limit")
        count = usage.get("count", 0) if usage.get("day") == day else 0
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Cannot verify the local FlightAware usage limit")
        if count >= config["flightaware_daily_limit"]:
            raise ValueError("FlightAware daily request limit reached")
        private_json(self._budget_path, {"day": day, "count": count + 1})

    def _flightaware(self, path, config):
        key_path = self.data_dir / "flightaware.key"
        try:
            api_key = key_path.read_text().strip()
        except OSError:
            raise ValueError("FlightAware is enabled; add its API key using store-secret") from None
        if not api_key:
            raise ValueError("FlightAware key is empty")
        self._allow_paid_request(config)
        return request_json("https://aeroapi.flightaware.com/aeroapi" + path, {"x-apikey": api_key})

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
        return copy.deepcopy([row for row in rows if _nearby_fresh(row, now)])

    def poll(self, config, selected, *, on_duty=False, watches=None):
        """Called by one backend thread; failures never erase the calendar."""
        self.error = None
        now = utcnow()
        try:
            self._poll_live(config, selected, now)
        except (ValueError, OSError, urllib.error.URLError, KeyError, TypeError, AttributeError):
            self.error = "Live flight lookup unavailable; showing the calendar schedule. Check your airline code, API key, account and request limit."
        if self.live:
            try:
                if abs((now - parse_time(self.live["observed_at"])).total_seconds()) > 300:
                    self.live = None
            except (ValueError, KeyError):
                self.live = None
        # The operating flight always gets the first use of the existing shared
        # paid-request budget. Public requests never replace its live position.
        self._poll_watches(config, watches or [], now)
        if config["demo"] or not config["nearby_enabled"] or on_duty:
            self.nearby = []
            self._nearby_completed = (None, [])
            return
        lat, lon = config["home_latitude"], config["home_longitude"]
        if not coordinates(lat, lon):
            self.nearby = []
            self._nearby_completed = (None, [])
            return
        try:
            radius_miles, request_radius_nm = _nearby_radius(config)
        except ValueError:
            self.nearby = []
            self._nearby_completed = (None, [])
            self.error = "Nearby traffic radius is invalid."
            return
        query = (lat, lon, radius_miles)
        if query != self._nearby_query:
            self.nearby = []
            self._nearby_completed = (None, [])
            self._nearby_query = query
            self._last_nearby = -math.inf
        self.nearby = [row for row in self.nearby if _nearby_fresh(row, now)]
        if time.monotonic() - self._last_nearby < 60:
            return
        self._last_nearby = time.monotonic()
        try:
            # The community ADS-B feed is optional and has no availability guarantee.
            data = request_json(f"https://api.adsb.lol/v2/point/{lat}/{lon}/{request_radius_nm}")
            self.nearby = _nearby_aircraft(data, lat, lon, radius_miles, utcnow())
            self._nearby_completed = (query, copy.deepcopy(self.nearby))
        except (ValueError, OSError, urllib.error.URLError, TypeError, AttributeError):
            self.nearby = []
            self._nearby_completed = (None, [])
            self.error = "Nearby traffic feed unavailable."

    @staticmethod
    def _watch_flight(data, watch):
        if not isinstance(data, dict) or not isinstance(data.get("flights"), list) or len(data["flights"]) > 1000:
            raise ValueError("Invalid public flight result")
        links = data.get("links") or {}
        if not isinstance(links, dict):
            raise ValueError("Invalid pagination")
        # Never buy a second page or silently choose a leg from a partial set.
        if links.get("next"):
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
        if not config["flightaware_enabled"] or not selected or config["demo"]:
            self.live = None
            return
        if self._selected != selected["id"]:
            self._selected, self._live_id, self._lookup_at = selected["id"], None, -math.inf
            self._last_live = -math.inf
            self.live = None
        start, end = parse_time(selected["start"]), parse_time(selected["end"])
        if not -3600 <= (now - start).total_seconds() or (now - end).total_seconds() > 3600:
            self.live = None
            return
        if time.monotonic() - self._last_live < config["flightaware_poll_seconds"]:
            return
        self._last_live = time.monotonic()
        if self._live_id is None and time.monotonic() - self._lookup_at > 300:
            self._lookup_at = time.monotonic()
            number = re.sub(r"[^A-Z0-9]", "", str(selected["number"]).upper())
            if number.isdigit():
                if not config["airline"]:
                    raise ValueError("Set your operating airline code for FlightAware lookups")
                number = config["airline"] + number
            params = urllib.parse.urlencode({"start": (start - timedelta(hours=6)).astimezone(timezone.utc).isoformat(),
                                             "end": (start + timedelta(hours=6)).astimezone(timezone.utc).isoformat(),
                                             "max_pages": 1})
            data = self._flightaware("/flights/" + urllib.parse.quote(number, safe="") + "?" + params, config)
            matches = []
            for flight in data.get("flights", []):
                if not isinstance(flight, dict):
                    continue
                origin, destination = flight.get("origin") or {}, flight.get("destination") or {}
                if origin.get("code_iata") != selected["origin"] or destination.get("code_iata") != selected["destination"]:
                    continue
                departure = flight.get("scheduled_out") or flight.get("scheduled_off")
                if departure and flight.get("fa_flight_id"):
                    try:
                        difference = abs((parse_time(departure) - start).total_seconds())
                    except ValueError:
                        continue
                    if difference < 6 * 3600:
                        matches.append((difference, str(flight["fa_flight_id"])))
            matches.sort()
            # An ambiguous exact tie has no sufficiently reliable position association.
            if matches and (len(matches) == 1 or matches[0][0] < matches[1][0]):
                self._live_id = matches[0][1]
        if not self._live_id:
            return
        data = self._flightaware("/flights/" + urllib.parse.quote(self._live_id, safe="") + "/position", config)
        position = data.get("last_position") or {}
        observed = position.get("timestamp")
        lat, lon = position.get("latitude"), position.get("longitude")
        if not observed or not coordinates(lat, lon):
            return
        if abs((now - parse_time(observed)).total_seconds()) > 300:
            self.live = None
            return
        altitude = position.get("altitude")
        altitude_feet = altitude * 100 if _is_number(altitude) else None
        self.live = {"latitude": lat, "longitude": lon, "observed_at": observed,
                     "flight_id": selected["id"],
                     "altitude": altitude_feet, "altitude_units": "feet", "groundspeed": position.get("groundspeed"),
                     "heading": position.get("heading"), "source": "FlightAware"}
