"""Separate household Google accounts and a minimal Mom/Dad custody cache."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .calendar_client import CalendarClient, CalendarError, parse_events, _event_time
from .brief import attach_brief_leaves, clean_brief_leaves, parse_brief_leave_notes
from .config import private_json
from .mycrew import SOURCE as MYCREW_SOURCE, STATION_LOCAL, airport_zone, clean_trip_days, parse_mycrew_events, parse_mycrew_trip_days
from .outlook import calendar_fresh, custody_outlook

try:
    import fcntl
except ImportError:
    fcntl = None

UTC = timezone.utc
PROFILES = ("chris", "lisa")
_ACTIVITY_ALIASES = {
    "Jiu-jitsu": ("jiu jitsu", "ju jitsu", "jiujitsu", "jujitsu", "bjj", "jiu jitsu class"),
    "Gymnastics": ("gymnastics",),
    "Dance": ("dance", "dance class", "dance classes", "ballet", "tap dance"),
    "Soccer": ("soccer", "soccer game", "soccer games", "soccer match", "soccer practice"),
    "Viola": ("viola", "viola lesson", "viola lessons", "viola class", "viola classes"),
    "Violin": ("violin", "violin lesson", "violin lessons", "violin class", "violin classes"),
}


def _agenda_words(value):
    text = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(char for char in text if not unicodedata.combining(char))))


def _agenda_label(value, limit):
    if not isinstance(value, str):
        return None
    label = " ".join(value.split())
    if 1 <= len(label) <= limit and label[0].isalpha() and all(
            char.isalpha() or char in " '-\u2019" for char in label):
        return label
    return None


def _agenda_preferences(path):
    try:
        with path.open("rb") as handle:
            raw = handle.read(8193)
        value = json.loads(raw) if len(raw) <= 8192 else {}
    except (OSError, ValueError, TypeError):
        value = {}
    if not isinstance(value, dict) or not isinstance(value.get("children"), list):
        return {}, {}, {}
    children = {}
    for item in value["children"][:12]:
        name = _agenda_label(item, 24)
        if name:
            children.setdefault(_agenda_words(name), name)
    names = dict(children)
    child_aliases = value.get("child_aliases")
    if isinstance(child_aliases, dict):
        for item, target in list(child_aliases.items())[:24]:
            alias, name = _agenda_label(item, 24), _agenda_label(target, 24)
            if alias and name and (name := names.get(_agenda_words(name))):
                children.setdefault(_agenda_words(alias), name)
    activities = dict(_ACTIVITY_ALIASES)
    custom = value.get("activities")
    if isinstance(custom, dict):
        for item, synonyms in list(custom.items())[:12]:
            label = _agenda_label(item, 24)
            if label and isinstance(synonyms, list):
                aliases = {_agenda_words(name) for name in (_agenda_label(entry, 32) for entry in synonyms[:12]) if name}
                activities[label] = tuple(sorted(aliases | {_agenda_words(label)} | set(activities.get(label, ()))))
    defaults = {}
    activity_defaults = value.get("activity_defaults")
    if isinstance(activity_defaults, dict):
        for label, target in list(activity_defaults.items())[:12]:
            name = _agenda_label(target, 24)
            if label in activities and name and (name := names.get(_agenda_words(name))):
                defaults[label] = name
    precedence = value.get("activity_precedence")
    if isinstance(precedence, list):
        precedence = list(dict.fromkeys(label for label in precedence[:12]
                                        if isinstance(label, str) and label in activities))
    else:
        precedence = []
    ranks = {label: index for index, label in enumerate(precedence)}
    overrides = {}
    same_day = value.get("same_day_activity_overrides")
    if isinstance(same_day, dict):
        for label, preferred in list(same_day.items())[:12]:
            if (label in defaults and label in ranks and isinstance(preferred, str)
                    and preferred in ranks and ranks[preferred] < ranks[label]):
                overrides[label] = preferred
    return children, activities, {"activity_defaults": defaults, "activity_precedence": precedence,
                                  "same_day_activity_overrides": overrides}


def _now():
    return datetime.now(UTC)


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Missing timezone")
    return result.astimezone(UTC)


def _midnight(value, zone_name):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        day, zone = date.fromisoformat(value), ZoneInfo(zone_name)
        naive = datetime.combine(day, time())
        candidates = set()
        for fold in (0, 1):
            candidate = naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == naive:
                candidates.add(candidate)
        return next(iter(candidates)) if len(candidates) == 1 else None
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
        return None


def _event_versions(events):
    unique, anonymous = {}, []
    def updated(item):
        try:
            return _stamp(item.get("updated"))
        except (ValueError, TypeError, OverflowError):
            return datetime.min.replace(tzinfo=UTC)
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id:
            previous = unique.get(event_id)
            if previous is None or updated(event) > updated(previous) or (updated(event) == updated(previous) and event.get("status") == "cancelled"):
                unique[event_id] = event
        else:
            anonymous.append(event)
    return [*unique.values(), *anonymous]


def parse_kids_events(events, default_timezone="UTC"):
    """Retain normalized exact Mom/Dad/parent's-house aliases only."""
    intervals = {}
    for event in _event_versions(events):
        title = event.get("summary")
        normalized = title.strip().casefold().replace("\u2019", "'") if isinstance(title, str) else ""
        parent = {"mom": "Mom", "mom's house": "Mom", "dad": "Dad", "dad's house": "Dad"}.get(normalized)
        if parent is None or event.get("status") == "cancelled":
            continue
        start_data, end_data = event.get("start"), event.get("end")
        if not isinstance(start_data, dict) or not isinstance(end_data, dict):
            continue
        if "date" in start_data and "date" in end_data and "dateTime" not in start_data and "dateTime" not in end_data:
            start = _midnight(start_data["date"], start_data.get("timeZone") or default_timezone)
            end = _midnight(end_data["date"], end_data.get("timeZone") or start_data.get("timeZone") or default_timezone)
        elif "dateTime" in start_data and "dateTime" in end_data and "date" not in start_data and "date" not in end_data:
            start_value, end_value = dict(start_data), dict(end_data)
            if start_data.get("timeZone"):
                end_value.setdefault("timeZone", start_data["timeZone"])
            start, end = _event_time(start_value), _event_time(end_value)
        else:
            continue
        if start is None or end is None or end.astimezone(UTC) <= start.astimezone(UTC):
            continue
        interval = {"start": _iso(start), "end": _iso(end), "parent": parent,
                    "state": "home" if parent == "Mom" else "away"}
        intervals[(interval["start"], interval["end"], parent)] = interval
    return sorted(intervals.values(), key=lambda item: (_stamp(item["start"]), item["parent"], _stamp(item["end"])))


def parse_kids_activities(events, children, activities, now, display_timezone="UTC", *,
                          event_timezone=None, activity_defaults=None, activity_precedence=(),
                          same_day_activity_overrides=None):
    """Project today's approved activity labels, optional child names and times."""
    if not children:
        return []
    zone = ZoneInfo(display_timezone)
    day = now.astimezone(zone).date()
    day_start = datetime.combine(day, time(), zone).astimezone(UTC)
    day_end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
    entries = set()
    explicitly_named = set()
    for event in _event_versions(events):
        summary = event.get("summary")
        if event.get("status") == "cancelled" or not isinstance(summary, str) or len(summary) > 256:
            continue
        normalized = _agenda_words(summary)
        words = f" {normalized} "
        first_word, last_word = normalized.partition(" ")[0], normalized.rpartition(" ")[2]
        matches = {name for word, name in children.items() if len(word) > 1 and f" {word} " in words}
        if not matches:
            matches = {children[word] for word in (first_word, last_word) if len(word) == 1 and word in children}
        # Without a named child, trust only an exact activity title from the
        # selected family calendar. A longer title may describe an adult event.
        labels = [label for label, aliases in activities.items()
                  if any(f" {alias} " in words for alias in aliases) and (matches or normalized in aliases)]
        if len(labels) != 1 or any(word in words for word in (" cancelled ", " canceled ")):
            continue
        start_data, end_data = event.get("start"), event.get("end")
        if not isinstance(start_data, dict) or not isinstance(end_data, dict):
            continue
        all_day = "date" in start_data and "date" in end_data
        if all_day and "dateTime" not in start_data and "dateTime" not in end_data:
            # Date-only events keep the calendar's civil dates on the wall.
            start = _midnight(start_data["date"], display_timezone)
            end = _midnight(end_data["date"], display_timezone)
        elif "date" not in start_data and "date" not in end_data:
            start_zone = start_data.get("timeZone") or event_timezone or display_timezone
            start = _event_time({**start_data, "timeZone": start_zone})
            end = _event_time({**end_data, "timeZone": end_data.get("timeZone") or start_zone})
        else:
            continue
        if start is None or end is None:
            continue
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if not (day_start < end and start < day_end and start < end):
            continue
        if not all_day and end - start > timedelta(days=1):
            continue
        label = labels[0]
        names = matches or {(activity_defaults or {}).get(label, "")}
        projected = {(start, end, name, label, all_day) for name in names}
        entries.update(projected)
        if matches:
            explicitly_named.update(projected)
    if same_day_activity_overrides:
        # The selected date already limits entries to today's local day.
        # A defaulted row may be skipped, but an explicitly named row stays.
        scheduled = {(name, label) for _, _, name, label, _ in explicitly_named}
        entries = {entry for entry in entries if entry in explicitly_named
                   or (entry[2], same_day_activity_overrides.get(entry[3])) not in scheduled}
    if activity_precedence:
        entries = _preferred_activity_entries(entries, activity_precedence)
    return [{"title": f"{name} {label}" if name else label, "start": _iso(start), "end": _iso(end),
             **({"all_day": True} if all_day else {})}
            for start, end, name, label, all_day in sorted(entries)[:48]]


def _preferred_activity_entries(entries, precedence):
    """Resolve only configured timed overlaps for a single named child."""
    kept = set(entries)
    for index, label in enumerate(precedence):
        for entry in entries:
            start, end, name, kind, all_day = entry
            if kind != label or not name or all_day:
                continue
            if any(other_name == name and other_kind in precedence[:index] and not other_all_day
                   and other_start < end and start < other_end
                   for other_start, other_end, other_name, other_kind, other_all_day in kept):
                kept.discard(entry)
    return kept


def kids_snapshot(intervals, last_sync, now=None, stale_after=1800, source=""):
    now = now or _now()
    result = {"state": "unknown", "parent": None, "until": None, "last_sync": last_sync,
              "stale": True, "error": None, "source": source}
    try:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Invalid current time")
        age = (now.astimezone(UTC) - _stamp(last_sync)).total_seconds()
        if age > stale_after or age < -300:
            result["error"] = "The kids schedule is out of date; current custody is unknown."
            return result
    except (ValueError, TypeError, OverflowError):
        result["last_sync"] = None
        result["error"] = "The kids schedule has not synchronized successfully."
        return result
    result["stale"] = False
    active = []
    for interval in intervals:
        try:
            if interval.get("parent") not in ("Mom", "Dad"):
                continue
            if _stamp(interval["start"]) <= now.astimezone(UTC) < _stamp(interval["end"]):
                active.append(interval)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    parents = {item["parent"] for item in active}
    if len(parents) > 1:
        result["error"] = "Mom and Dad events overlap; current custody is unknown."
    elif not parents:
        result["error"] = "No Mom or Dad event covers the current time."
    else:
        parent = next(iter(parents))
        result.update(parent=parent, state="home" if parent == "Mom" else "away",
                      until=_iso(min(_stamp(item["end"]) for item in active)))
    return result


def _empty_flights(config):
    return {"flights": [], "last_sync": None, "error": None, "source": str(config.get("calendar_name") or "FC View"), "demo": False}


def parse_labelled_layovers(events, source_label="Flight calendar"):
    """Opt-in hotel names only from explicit, timed Hotel/Layover events.

    Report duty intervals never become invented layover times. Contact values,
    crew fields, addresses in other lines, and raw descriptions are discarded.
    """
    result, seen = [], set()
    source_label = "".join(char for char in str(source_label) if ord(char) >= 32)[:120]
    for event in _event_versions(events):
        if not isinstance(event, dict) or event.get("status") == "cancelled":
            continue
        title = event.get("summary", "")
        if not isinstance(title, str):
            continue
        title = title.strip().upper()
        match = re.fullmatch(r"(?:HOTEL|LAYOVER)(?:\s*:\s*|\s+)([A-Z]{3})", title)
        if match is None and title not in {"HOTEL", "LAYOVER"}:
            continue
        start, end = _event_time(event.get("start")), _event_time(event.get("end"))
        if start is None or end is None or end.astimezone(UTC) <= start.astimezone(UTC):
            continue
        description = event.get("description", "")
        if not isinstance(description, str) or len(description) > 32768:
            continue
        airport, hotel, city = match[1] if match else None, None, None
        for line in description.splitlines():
            label = re.fullmatch(r"\s*(Hotel|Layover Hotel|City|Airport|Station)\s*:\s*(.*?)\s*", line, re.IGNORECASE)
            if label is None:
                continue
            key, value = label[1].casefold(), label[2]
            if key in {"airport", "station"}:
                if airport is None and re.fullmatch(r"[A-Z]{3}", value.upper()):
                    airport = value.upper()
                continue
            if not value or len(value) > (120 if key in {"hotel", "layover hotel"} else 80):
                continue
            if any(ord(char) < 32 for char in value) or "@" in value or "<" in value or ">" in value or "://" in value:
                continue
            if re.search(r"\d[\d\s().+-]{5,}\d", value) or re.search(r"\b(?:crew|captain|first officer|phone|telephone|contact)\b", value, re.IGNORECASE):
                continue
            if key in {"hotel", "layover hotel"} and hotel is None:
                hotel = value
            elif key == "city" and city is None:
                city = value
        if not airport or not hotel:
            continue
        key = (airport, _iso(start), _iso(end), hotel)
        if key in seen:
            continue
        seen.add(key)
        result.append({"airport": airport, "start": _iso(start), "end": _iso(end), "hotel": hotel,
                       "city": city or "", "source_label": source_label})
    return sorted(result, key=lambda item: _stamp(item["start"]))


def _binding(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _merge_flights(snapshots):
    flights = []
    for snapshot in snapshots:
        for flight in snapshot.get("flights", []):
            number = str(flight.get("number", "")).upper().replace(" ", "")
            match = re.fullmatch(r"(?:(?P<prefix>[A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z]))?(?P<number>\d{1,5}[A-Z]?)", number)
            duplicate = None
            conflict = False
            for previous in flights:
                previous_number = str(previous.get("number", "")).upper().replace(" ", "")
                prior = re.fullmatch(r"(?:(?P<prefix>[A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z]))?(?P<number>\d{1,5}[A-Z]?)", previous_number)
                same_number = number == previous_number
                if match and prior:
                    same_number = match["number"] == prior["number"] and (not match["prefix"] or not prior["prefix"] or match["prefix"] == prior["prefix"])
                if same_number and flight.get("origin") == previous.get("origin") and flight.get("destination") == previous.get("destination"):
                    try:
                        start, prior_start = _stamp(flight["start"]), _stamp(previous["start"])
                        if start == prior_start:
                            duplicate = previous
                            break
                        if snapshot.get("source") == MYCREW_SOURCE:
                            zone = airport_zone(flight["origin"])
                            if zone and start.astimezone(zone).date() == prior_start.astimezone(zone).date():
                                conflict = True  # Preserve the existing selected leg when clocks disagree.
                    except (ValueError, KeyError):
                        pass
            if duplicate is None and not conflict:
                flights.append(copy.deepcopy(flight))
            elif duplicate is not None:
                if "report" not in duplicate and flight.get("report"):
                    duplicate["report"] = flight["report"]
                if snapshot.get("source") == MYCREW_SOURCE and flight["deadhead"]:
                    duplicate["deadhead"] = True
    return sorted(flights, key=lambda item: _stamp(item["start"]))


class HouseholdCalendars:
    """Two account stores, selected calendar roles, and read-only sync."""

    def __init__(self, data_dir, config_getter):
        self.data_dir = Path(data_dir)
        self.config_getter = config_getter
        self.path = self.data_dir / "household-calendars.json"
        self.agenda_path = self.data_dir / "family-agenda.json"
        self.lock = threading.RLock()
        self._migrate_chris()

    def _migrate_chris(self):
        account = self.data_dir / "accounts" / "chris"
        for name in ("google-client.json", "google-token.json"):
            destination, legacy = account / name, self.data_dir / name
            if destination.exists() or not legacy.exists():
                continue
            try:
                value = json.loads(legacy.read_text())
                if isinstance(value, dict):
                    account.mkdir(parents=True, exist_ok=True, mode=0o700)
                    account.parent.chmod(0o700)
                    account.chmod(0o700)
                    private_json(destination, value)
            except (OSError, ValueError, TypeError):
                pass  # A damaged legacy credential is not proof of a connection.

    def client_for(self, profile):
        if profile not in PROFILES:
            raise CalendarError("Choose household profile chris or lisa.")
        if profile == "chris":
            self._migrate_chris()
        account = self.data_dir / "accounts" / profile
        account.mkdir(parents=True, exist_ok=True, mode=0o700)
        account.parent.chmod(0o700)
        account.chmod(0o700)
        return CalendarClient(account, dict(self.config_getter()))

    def _read(self):
        try:
            value = json.loads(self.path.read_text())
            if isinstance(value, dict) and value.get("schema_version") == 1 and isinstance(value.get("profiles"), dict) and isinstance(value.get("flight_roles"), dict) and isinstance(value.get("family"), dict):
                value["profiles"] = {profile: record for profile, record in value["profiles"].items()
                                     if profile in PROFILES and isinstance(record, dict)
                                     and isinstance(record.get("verified_at"), (str, type(None)))
                                     and isinstance(record.get("error"), (str, type(None)))}
                allowed_flight = {"id", "number", "origin", "destination", "start", "end", "deadhead", "source", "report"}
                required_flight = allowed_flight - {"report"}
                roles = {}
                for role, cached in value["flight_roles"].items():
                    if role not in {"primary", "secondary", "mycrew"} or not isinstance(cached, dict) or not isinstance(cached.get("binding"), str):
                        continue
                    snapshot = cached.get("snapshot")
                    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("flights"), list) or not isinstance(snapshot.get("source"), str):
                        continue
                    if not isinstance(snapshot.get("error"), (str, type(None))):
                        continue
                    if snapshot.get("last_sync") is not None:
                        _stamp(snapshot["last_sync"])
                    valid = True
                    for flight in snapshot["flights"]:
                        if not isinstance(flight, dict) or set(flight) - allowed_flight or not required_flight.issubset(flight):
                            valid = False
                            break
                        _stamp(flight["start"])
                        _stamp(flight["end"])
                    layovers = snapshot.get("layovers", [])
                    if not isinstance(layovers, list):
                        layovers = []
                    clean_layovers = []
                    for item in layovers:
                        if not isinstance(item, dict) or set(item) != {"airport", "start", "end", "hotel", "city", "source_label"}:
                            continue
                        if not all(isinstance(item[key], str) for key in item):
                            continue
                        _stamp(item["start"])
                        _stamp(item["end"])
                        clean_layovers.append(item)
                    snapshot["layovers"] = clean_layovers
                    snapshot["brief_leaves"] = clean_brief_leaves(snapshot.get("brief_leaves")) if role == "secondary" else []
                    snapshot["trip_days"] = clean_trip_days(snapshot.get("trip_days")) if role == "mycrew" else []
                    if valid:
                        roles[role] = cached
                value["flight_roles"] = roles
                family = value["family"]
                if family:
                    valid = isinstance(family.get("binding"), str) and isinstance(family.get("intervals"), list)
                    valid = valid and isinstance(family.get("error"), (str, type(None)))
                    for interval in family.get("intervals", []) if valid else []:
                        if not isinstance(interval, dict) or set(interval) != {"start", "end", "parent", "state"} or interval["parent"] not in ("Mom", "Dad"):
                            valid = False
                            break
                        _stamp(interval["start"])
                        _stamp(interval["end"])
                    if not valid:
                        value["family"] = {}
                return value
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            pass
        return {"schema_version": 1, "profiles": {}, "flight_roles": {}, "family": {}}

    @contextmanager
    def _storage_lock(self):
        with self.lock:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.data_dir.chmod(0o700)
            descriptor = os.open(self.data_dir / "household-calendars.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _update(self, section, value):
        with self._storage_lock():
            state = self._read()
            state[section] = value
            private_json(self.path, state)

    def _profile_result(self, profile, error=None):
        with self._storage_lock():
            state = self._read()
            record = state["profiles"].get(profile, {})
            if error is None:
                record = {"verified_at": _iso(_now()), "error": None}
            else:
                record = {"verified_at": record.get("verified_at"), "error": str(error)}
            state["profiles"][profile] = record
            private_json(self.path, state)

    def list_calendars(self, profile):
        try:
            calendars = self.client_for(profile).list_calendars()
        except CalendarError as error:
            if profile in PROFILES:
                self._profile_result(profile, error)
            raise
        self._profile_result(profile)
        return calendars

    def authorize(self, profile, credentials_path, open_browser=True, **kwargs):
        result = self.client_for(profile).authorize(credentials_path, open_browser=open_browser, **kwargs)
        calendars = self.list_calendars(profile)
        return {**result, "profile": profile, "verified": True, "calendars": calendars}

    def preview_kids(self, profile, calendar_id):
        result = self.client_for(profile).get_events(calendar_id)
        intervals = parse_kids_events(result["items"], result["timezone"])
        now = _now()
        return {"matching_events": len(intervals), "timezone": result["timezone"], "intervals": intervals,
                "kids": kids_snapshot(intervals, _iso(now), now=now)}

    @staticmethod
    def _roles(config):
        primary = str(config.get("calendar_id") or "")
        secondary = str(config.get("secondary_calendar_id") or "")
        roles = [("primary", primary, str(config.get("calendar_name") or "FC View")),
                 ("secondary", secondary, str(config.get("secondary_calendar_name") or "Buzz Flight Brief"))]
        selected = config.get("mycrew_calendar_id")
        if (not config.get("demo") and config.get("mycrew_clock") == STATION_LOCAL and selected and config.get("airline")
                and selected != config.get("family_calendar_id")):
            roles.append(("mycrew", selected, MYCREW_SOURCE))
        return roles

    @staticmethod
    def _flight_binding(role, calendar_id, name, config):
        if role == "mycrew":
            return _binding("chris", calendar_id, name, config.get("airline"))
        return _binding("chris", calendar_id, name)

    @staticmethod
    def _family_binding(config):
        return _binding(config.get("family_calendar_profile", "chris"), config.get("family_calendar_id", ""))

    def status(self, now=None):
        config, now = dict(self.config_getter()), now or _now()
        with self.lock:
            state = self._read()
        profiles = {}
        stale_after = config.get("kids_stale_minutes", 30) * 60
        for profile in PROFILES:
            client = self.client_for(profile)
            authorized = client.client_path.exists() and client.token_path.exists()
            record = state["profiles"].get(profile, {})
            try:
                age = (now - _stamp(record.get("verified_at"))).total_seconds()
                verified = authorized and not record.get("error") and -300 <= age <= stale_after
            except (ValueError, TypeError, OverflowError):
                verified = False
            profiles[profile] = {"credentials_present": authorized, "authorized": bool(verified), "connected": bool(verified), "verified": bool(verified),
                                 "verified_at": record.get("verified_at"), "error": record.get("error")}
        matching, brief_notes = [], []
        for role, calendar_id, name in self._roles(config):
            cached = state["flight_roles"].get(role, {})
            if cached.get("binding") == self._flight_binding(role, calendar_id, name, config):
                matching.append(cached["snapshot"])
                if role == "secondary":
                    brief_notes.extend(cached["snapshot"].get("brief_leaves", []))
        if matching:
            dates = [item.get("last_sync") for item in matching if item.get("last_sync")]
            errors = list(dict.fromkeys(item["error"] for item in matching if item.get("error")))
            flights = {"flights": _merge_flights(matching), "last_sync": min(dates, key=_stamp) if dates else None,
                       "error": " ".join(errors) or None, "source": " + ".join(item["source"] for item in matching), "demo": False}
            flights["trip_days"] = clean_trip_days([span for item in matching for span in item.get("trip_days", [])])
        else:
            flights = CalendarClient(self.data_dir, config).snapshot()
            if config.get("secondary_calendar_id"):
                flights = _empty_flights(config)
        attach_brief_leaves(flights.get("flights", []),
                            brief_notes if config.get("brief_leave_times_enabled", False) else [],
                            config.get("brief_leave_policy", "conflict"))
        flights["layovers"] = []
        if config.get("hotel_details_enabled", False):
            seen_layovers = set()
            for snapshot in matching:
                for layover in snapshot.get("layovers", []):
                    key = (layover["airport"], layover["start"], layover["end"], layover["hotel"])
                    if key not in seen_layovers:
                        flights["layovers"].append(copy.deepcopy(layover))
                        seen_layovers.add(key)
        family = state["family"]
        if family.get("binding") != self._family_binding(config):
            family = {}
        kids = kids_snapshot(family.get("intervals", []), family.get("last_sync"), now,
                             stale_after=stale_after, source=str(config.get("family_calendar_name") or "Family calendar"))
        kids["enabled"] = bool(config.get("kids_filter_enabled", False) and config.get("family_calendar_id"))
        if not kids["enabled"] or family.get("error"):
            kids.update(state="unknown", parent=None, until=None,
                        error=family.get("error") or "Select a family calendar and enable the kids schedule.")
        outlook = custody_outlook(family.get("intervals", []), family.get("last_sync"), config, now,
                                  failed=bool(family.get("error") or not config.get("family_calendar_id")))
        if outlook.get("stale") is False and config.get("kids_filter_enabled") is True and not config.get("guest_mode"):
            children, activities, agenda_policy = _agenda_preferences(self.agenda_path)
            activity_sync = family.get("activity_last_sync")
            display_zone = config.get("timezone", "UTC")
            if (children and family.get("activity_scope") == _binding(children, activities, display_zone, agenda_policy)
                    and calendar_fresh(activity_sync, now, config.get("kids_stale_minutes", 30))):
                zone = ZoneInfo(display_zone)
                day = now.astimezone(zone).date()
                allowed = set(activities) | {name + " " + label for name in children.values() for label in activities}
                agenda = []
                cached = family.get("activities")
                for item in cached[:48] if isinstance(cached, list) else []:
                    try:
                        if (not isinstance(item, dict) or set(item) not in ({"title", "start", "end"}, {"title", "start", "end", "all_day"})
                                or ("all_day" in item and item["all_day"] is not True) or item["title"] not in allowed):
                            continue
                        start, end = _stamp(item["start"]), _stamp(item["end"])
                        if start.astimezone(zone).date() <= day <= (end - timedelta(microseconds=1)).astimezone(zone).date() and start < end:
                            agenda.append(item)
                    except (TypeError, ValueError, OverflowError):
                        continue
                outlook["activities"] = agenda
                outlook["activity_last_sync"] = activity_sync
        return {"flight_snapshot": flights, "kids": kids, "connected_profiles": profiles,
                "custody_outlook": outlook}

    def sync(self):
        config, now = dict(self.config_getter()), _now()
        errors = {}
        for profile in PROFILES:
            client = self.client_for(profile)
            if client.client_path.exists() and client.token_path.exists():
                try:
                    self.list_calendars(profile)
                except CalendarError as error:
                    errors[profile] = str(error)
        with self.lock:
            role_cache = self._read()["flight_roles"]
        roles = self._roles(config)
        mycrew_id = next((calendar_id for role, calendar_id, _ in roles if role == "mycrew"), None)
        if mycrew_id is None:
            role_cache.pop("mycrew", None)
        mycrew_result = None
        for role, calendar_id, source in roles:
            if not calendar_id:
                if role != "primary":
                    role_cache.pop(role, None)
                    continue
                try:
                    matches = [item for item in self.list_calendars("chris") if item["name"].strip().casefold() == source.strip().casefold()]
                    if len(matches) != 1:
                        raise CalendarError("Choose one Chris flight calendar by its ID during setup.")
                    calendar_id = matches[0]["id"]
                except CalendarError as error:
                    errors["chris"] = str(error)
            signature = self._flight_binding(role, str(config.get("calendar_id") or "") if role == "primary" else calendar_id,
                                             source, config)
            previous = role_cache.get(role, {})
            snapshot = previous.get("snapshot") if previous.get("binding") == signature else None
            snapshot = copy.deepcopy(snapshot) if snapshot else _empty_flights({"calendar_name": source})
            try:
                if "chris" in errors:
                    raise CalendarError(errors["chris"])
                include_hotels = bool(config.get("hotel_details_enabled", False) and role != "mycrew")
                include_leaves = bool(config.get("brief_leave_times_enabled", False) and role == "secondary")
                include_mycrew = calendar_id == mycrew_id
                if include_mycrew and mycrew_result is not None:
                    result = mycrew_result
                else:
                    result = self.client_for("chris").get_events(
                        calendar_id, include_descriptions=include_hotels or include_leaves or include_mycrew)
                    if include_mycrew:
                        mycrew_result = result
                flights = (parse_mycrew_events(result["items"], config["airline"]) if role == "mycrew"
                           else parse_events(result["items"], source))
                snapshot = {"flights": flights, "last_sync": _iso(now),
                            "error": None, "source": source, "demo": False,
                            "layovers": parse_labelled_layovers(result["items"], source) if include_hotels else [],
                            "trip_days": parse_mycrew_trip_days(result["items"]) if role == "mycrew" else [],
                            "brief_leaves": parse_brief_leave_notes(result["items"], config.get("timezone")) if include_leaves else []}
            except CalendarError as error:
                snapshot["error"] = str(error)
            role_cache[role] = {"binding": signature, "snapshot": snapshot}
        self._update("flight_roles", role_cache)
        if config.get("family_calendar_id") and config.get("kids_filter_enabled", False):
            signature = self._family_binding(config)
            with self.lock:
                old_family = self._read()["family"]
            family = old_family if old_family.get("binding") == signature else {"binding": signature, "intervals": [], "last_sync": None}
            family = copy.deepcopy(family)
            profile = config.get("family_calendar_profile", "chris")
            try:
                if profile in errors:
                    raise CalendarError(errors[profile])
                result = self.client_for(profile).get_events(config["family_calendar_id"], now - timedelta(days=2), now + timedelta(days=45))
                children, activities, agenda_policy = _agenda_preferences(self.agenda_path)
                display_zone = config.get("timezone", "UTC")
                family.update(intervals=parse_kids_events(result["items"], result["timezone"]),
                              activities=parse_kids_activities(result["items"], children, activities, now, display_zone,
                                                               event_timezone=result["timezone"], **agenda_policy),
                              activity_scope=_binding(children, activities, display_zone, agenda_policy),
                              activity_last_sync=_iso(now),
                              last_sync=_iso(now), error=None)
            except CalendarError as error:
                family["error"] = str(error)
            self._update("family", family)
        return self.status()
