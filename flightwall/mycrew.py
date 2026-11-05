"""Extract schedule-only legs from the verified FC View MiCrew note format.

Each supported duty is a one-day all-day event with a local report clock.
Its leg rows repeat the departure weekday and day of month. Source notes,
rotation titles, aircraft details, hotels, and contacts are never returned.
"""
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .brief import _latest, _note_text
from .calendar_client import _event_time

SOURCE = "MiCrew rotations"
STATION_LOCAL = "station-local"
UTC = timezone.utc
_ZONES = Path(__file__).resolve().parent.parent / "assets" / "maps" / "airport-timezones.json"
_ROTATION = re.compile(r"(?:[A-Z]?\d{3}|\d{4})")
_CLOCK = r"(?:[01]\d|2[0-3])[0-5]\d"
_CARRIER = re.compile(r"(?:[A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z])")
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LEG = re.compile(
    rf"(?P<day>[A-Z]{{2}}\d{{2}})\s+(?P<deadhead>DH\s+)?(?P<number>\d{{1,5}})\s+"
    rf"(?P<origin>[A-Z]{{3}})-(?P<destination>[A-Z]{{3}})\s+"
    rf"(?P<departure>{_CLOCK})-(?P<arrival>{_CLOCK})"
)
_LEG_PREFIX = re.compile(r"[A-Z]{2}\d{2}\s+(?:DH\s+)?\d")
_REPORT = re.compile(rf"REPORT:\s*(?P<clock>{_CLOCK})L")
_MAX_CONNECTION = timedelta(hours=6)
_MAX_LEG = timedelta(hours=20)


@lru_cache(maxsize=1)
def _airport_zones():
    try:
        value = json.loads(_ZONES.read_bytes())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def airport_zone(code):
    try:
        return ZoneInfo(_airport_zones()[code])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None


def _duty_date(event):
    bounds = _rotation_dates(event)
    return bounds[0] if bounds and bounds[1] - bounds[0] == timedelta(days=1) else None


def _rotation_dates(event):
    bounds = []
    for key in ("start", "end"):
        field = event.get(key)
        value = field.get("date") if isinstance(field, dict) and "dateTime" not in field else None
        if not isinstance(value, str) or not _DATE.fullmatch(value):
            return None
        bounds.append(date.fromisoformat(value))
    return bounds if timedelta(0) < bounds[1] - bounds[0] <= timedelta(days=31) else None


def clean_trip_days(value):
    """Retain only bounded, exact date spans from the selected rotation source."""
    if not isinstance(value, list):
        return []
    spans = set()
    for item in value[:256]:
        if not isinstance(item, dict) or set(item) != {"start_date", "end_date"}:
            continue
        try:
            bounds = _rotation_dates({"start": {"date": item["start_date"]}, "end": {"date": item["end_date"]}})
        except (ValueError, OverflowError):
            continue
        if bounds:
            spans.add((bounds[0].isoformat(), bounds[1].isoformat()))
    return [{"start_date": start, "end_date": end} for start, end in sorted(spans)]


def parse_mycrew_trip_days(events):
    """Project source rotation event dates, without joining separate duties."""
    spans = []
    for event in _latest(events):
        title = event.get("summary")
        if (event.get("status") == "cancelled" or not isinstance(title, str)
                or len(title) > 150 or not _ROTATION.fullmatch(" ".join(title.upper().split()))):
            continue
        try:
            bounds = _rotation_dates(event)
        except (ValueError, OverflowError):
            continue
        if bounds:
            spans.append({"start_date": bounds[0].isoformat(), "end_date": bounds[1].isoformat()})
    return clean_trip_days(spans)


def _local_clock(day, clock, zone):
    local = datetime.combine(day, time(int(clock[:2]), int(clock[2:])))
    value = _event_time({"dateTime": local.isoformat(), "timeZone": zone.key})
    return value.astimezone(UTC) if value is not None else None


def _arrival_clock(clock, zone, departure):
    """Choose one local date consistent with the departure and elapsed time."""
    last = departure + _MAX_LEG
    first_day, last_day = departure.astimezone(zone).date(), last.astimezone(zone).date()
    candidates = set()
    for offset in range((last_day - first_day).days + 1):
        value = _local_clock(first_day + timedelta(days=offset), clock, zone)
        if value is not None and departure < value <= last:
            candidates.add(value)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _rotation_legs(event, airline):
    day = _duty_date(event)
    if day is None:
        return []
    note = _note_text(event.get("description"))
    if note is None:
        return []
    day_code = f"{_WEEKDAYS[day.weekday()]}{day.day:02d}"
    flights, last_arrival, last_airport = [], None, None
    report_clock, report = None, None
    for raw in note.splitlines():
        line = " ".join(raw.upper().split())
        if line.startswith("REPORT:"):
            marker = _REPORT.fullmatch(line)
            if marker is None or report_clock is not None or flights:
                return []
            report_clock = marker["clock"]
            continue
        leg = _LEG.fullmatch(line)
        if leg is None:
            if _LEG_PREFIX.match(line):
                return []  # A malformed leg invalidates the sequence after it.
            continue
        origin, destination = leg["origin"], leg["destination"]
        if (report_clock is None or leg["day"] != day_code or origin == destination
                or last_airport not in (None, origin) or len(flights) >= 64):
            return []
        departure_zone, arrival_zone = airport_zone(origin), airport_zone(destination)
        if departure_zone is None or arrival_zone is None:
            return []
        if report is None:
            report = _local_clock(day, report_clock, departure_zone)
            if report is None:
                return []
        earliest = last_arrival or report
        departure = _local_clock(day, leg["departure"], departure_zone)
        if departure is None or not earliest <= departure <= earliest + _MAX_CONNECTION:
            return []
        arrival = _arrival_clock(leg["arrival"], arrival_zone, departure)
        if arrival is None:
            return []
        number = airline + leg["number"]
        key = "|".join((number, origin, destination, departure.isoformat()))
        flight = {"id": hashlib.sha256(key.encode("ascii")).hexdigest()[:24], "number": number,
                  "origin": origin, "destination": destination, "start": departure.isoformat(),
                  "end": arrival.isoformat(), "deadhead": leg["deadhead"] is not None,
                  "source": SOURCE, "report": report.isoformat()}
        flights.append(flight)
        last_arrival, last_airport = arrival, destination
    return flights


def parse_mycrew_events(events, airline):
    """Return deduplicated legs from supported, non-cancelled rotations."""
    if not isinstance(airline, str) or not _CARRIER.fullmatch(airline):
        return []
    flights, conflicts = {}, set()
    for event in _latest(events):
        title = event.get("summary")
        if (event.get("status") == "cancelled" or not isinstance(title, str)
                or len(title) > 150 or not _ROTATION.fullmatch(" ".join(title.upper().split()))):
            continue
        try:
            for flight in _rotation_legs(event, airline):
                key = flight["number"], flight["origin"], flight["destination"], event["start"]["date"]
                previous = flights.get(key)
                if previous is None:
                    flights[key] = flight
                elif any(flight[field] != previous[field] for field in ("start", "end", "report")):
                    conflicts.add(key)
                elif flight["deadhead"]:
                    previous["deadhead"] = True
        except (ValueError, OverflowError):
            continue
    return sorted((flight for key, flight in flights.items() if key not in conflicts),
                  key=lambda row: (row["start"], row["number"]))
