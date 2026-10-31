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
            return
        if time.monotonic() - self._last_nearby < 60:
            return
        self._last_nearby = time.monotonic()
        lat, lon = config["home_latitude"], config["home_longitude"]
        if lat is None or lon is None:
            return
        try:
            # The community ADS-B feed is optional and has no availability guarantee.
            data = request_json(f"https://api.adsb.lol/v2/point/{lat}/{lon}/{config['nearby_radius_nm']}")
            safe = []
            for item in data.get("ac", [])[:500]:
                if not isinstance(item, dict):
                    continue
                la, lo = item.get("lat"), item.get("lon")
                if not coordinates(la, lo):
                    continue
                age = item.get("seen_pos", item.get("seen", 9999))
                if not _is_number(age) or not 0 <= age <= 120:
                    continue
                altitude = item.get("alt_baro")
                altitude = altitude if _is_number(altitude) else None
                safe.append({"latitude": la, "longitude": lo, "lat": la, "lon": lo,
                             "flight": str(item.get("flight", item.get("hex", "?"))).strip()[:12],
                             "altitude": altitude, "groundspeed": item.get("gs"),
                             "track": item.get("track"), "source": "ADSB.lol",
                             "observed_at": (now - timedelta(seconds=age)).isoformat()})
            self.nearby = safe
        except (ValueError, OSError, urllib.error.URLError, TypeError, AttributeError):
            self.nearby = []
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
