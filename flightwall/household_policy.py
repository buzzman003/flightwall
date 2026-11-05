"""Pure household display policy. Unknown presence never grants privacy.

Upstream integrations own identity, freshness and card validation. This module
does no I/O and never infers a person's physical presence from a flight roster.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re
from urllib.parse import urlsplit
from .outlook import ACTIVITIES_PER_PAGE, today_activities, trip_preview
from .personal import personal_card_visible
from .card_preferences import card_enabled


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return result if result.tzinfo is not None else None


def _now(value):
    return _time(value) or datetime.now(timezone.utc)


def _known(record, state):
    return isinstance(record, dict) and record.get("stale") is False and record.get("state") == state


def _flights(status, config):
    snapshot = status.get("snapshot") or {}
    if not isinstance(snapshot, dict) or snapshot.get("demo"):
        return []
    result = []
    for flight in snapshot.get("flights") or []:
        if not isinstance(flight, dict) or flight.get("deadhead") and not config.get("show_deadheads", True):
            continue
        start, end = _time(flight.get("start")), _time(flight.get("end"))
        if start and end and start < end:
            result.append((flight, start, end))
    return sorted(result, key=lambda entry: entry[1])


def _priority_flight(status, config, now):
    from .flight_alerts import fresh_status, within_tracking_window
    live_status = fresh_status(status.get("operating_status"), now)
    confirmed = live_status.get("status") == "fresh" or (
        config.get("flight_focus_only") is True and live_status.get("status") == "stale")
    arrived_id = (status.get("recent_vps_arrival") or {}).get("flight_id") if recent_vps_arrival(status, now) else None
    if (config.get("flightaware_enabled") is True and confirmed
            and live_status.get("source") == "FlightAware AeroAPI"
            and live_status.get("provider_flight_id")
            and live_status.get("phase") in {"departed_gate", "airborne"}):
        matched = next((flight for flight, _, _ in _flights(status, config)
                        if flight.get("id") != arrived_id and flight.get("id") == live_status.get("flight_id")
                        and (flight.get("origin"), flight.get("destination")) ==
                        (live_status.get("origin"), live_status.get("destination"))
                        and not flight.get("deadhead") and within_tracking_window(flight, now)), None)
        if matched:
            return matched
    def minutes(key):
        value = config.get(key, 60)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1440 else 60
    before = timedelta(minutes=minutes("flight_window_before_minutes"))
    after = timedelta(minutes=minutes("flight_window_after_minutes"))
    candidates = [entry for entry in _flights(status, config)
                  if entry[0].get("id") != arrived_id and entry[1] - before <= now < entry[2] + after]
    if not candidates:
        return None
    def order(entry):
        flight, start, end = entry
        phase = 0 if start <= now < end else 1 if now < start else 2
        return phase, abs((start - now).total_seconds())
    return min(candidates, key=order)[0]


def recent_vps_arrival(status, now=None):
    """A short actual-arrival exception for the local map, never presence."""
    now = _now(now)
    record = status.get("recent_vps_arrival") if isinstance(status, dict) else None
    snapshot = status.get("snapshot") if isinstance(status, dict) else None
    if (not isinstance(record, dict) or not isinstance(snapshot, dict) or snapshot.get("demo") is not False
            or record.get("destination") != "VPS" or record.get("source") != "FlightAware AeroAPI"
            or record.get("basis") not in {"actual_on", "actual_in"} or not record.get("flight_id")):
        return False
    arrived = _time(record.get("arrived_at"))
    if arrived is None or not timedelta(0) <= now - arrived < timedelta(hours=1):
        return False
    return any(isinstance(row, dict) and row.get("id") == record["flight_id"]
               and row.get("destination") == "VPS" and not row.get("deadhead")
               for row in snapshot.get("flights") or [])


def household_context(status, config, now=None):
    """Return explicit booleans; every private-room condition must be known."""
    status, config, now = status or {}, config or {}, _now(now)
    presence = status.get("presence") or {}
    chris = presence.get("chris") if isinstance(presence, dict) else None
    lisa = presence.get("lisa") if isinstance(presence, dict) else None
    kids = status.get("kids")
    result = {"chris_home": _known(chris, "home"), "chris_away": _known(chris, "away"),
              "lisa_home": _known(lisa, "home"), "lisa_away": _known(lisa, "away"),
              "kids_home": _known(kids, "home"), "kids_away": _known(kids, "away")}
    result["private_lisa"] = bool(config.get("lisa_private_enabled") is True
                                  and config.get("guest_mode", False) is False
                                  and result["lisa_home"] and result["chris_away"] and result["kids_away"])
    # A real roster can control display timing while a phone is in airplane
    # mode. This is a display choice, never evidence of physical presence.
    result["flight_priority"] = _priority_flight(status, config, now) is not None
    result["recent_vps_arrival"] = recent_vps_arrival(status, now)
    # Being known away is enough to suppress unrelated local traffic. A roster
    # alone must not become a claim that Chris has physically left home.
    result["trip_away"] = result["chris_away"]
    return result


def _visible(item, now, *, require_window=False):
    if not isinstance(item, dict):
        return False
    start = _time(item.get("display_start") or item.get("start"))
    end = _time(item.get("display_end") or item.get("end") or item.get("expires_at"))
    if require_window and (start is None or end is None):
        return False
    return not (start and now < start or end and now >= end)


def filter_personal_items(items, status, config, now=None):
    """Keep active household cards; a configured kids filter fails closed."""
    now = _now(now)
    if config.get("personal_enabled", True) is not True:
        return []
    context = household_context(status, config, now)
    return [dict(item) for item in items or [] if personal_card_visible(item, config, now)
            and not (item.get("kind") == "dinner" and config.get("kids_filter_enabled") is True and not context["kids_home"])]


def kids_home_current(status, config, now=None):
    """Recheck a dated custody observation, not a cached room boolean."""
    now = _now(now)
    kids = status.get("kids") if isinstance(status, dict) else None
    if not _known(kids, "home"):
        return False
    observed, until = _time(kids.get("last_sync")), _time(kids.get("until"))
    age = config.get("kids_stale_minutes", 30)
    age = age if type(age) in (int, float) and math.isfinite(age) and 1 <= age <= 1440 else 30
    return bool(observed and until and timedelta(minutes=-5) <= now - observed <= timedelta(minutes=age) and now < until)


def dinner_options_visible(status, config, now=None):
    """Daily ideas require dated Kids Home, current generation and no selection.

    This view is never a presence observation, an alert, or a cached private
    boolean. Missing/expired backend data fails closed even during manual pause.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    now = _now(now)
    if (not isinstance(status, dict) or config.get("personal_enabled", True) is not True
            or config.get("dinner_options_enabled") is not True
            or config.get("demo") is True or status.get("demo") is True
            or isinstance(status.get("snapshot"), dict) and status["snapshot"].get("demo") is True
            or config.get("dinner_options_card_visible", True) is not True
            or status.get("backend_available", True) is not True
            or status.get("dinner_today_selected") is not False):
        return False
    kids, options = status.get("kids"), status.get("dinner_options")
    if not kids_home_current(status, config, now) or not isinstance(options, dict) or options.get("status") != "ready":
        return False
    try:
        zone = ZoneInfo(config.get("timezone", "UTC"))
        local = now.astimezone(zone)
        if not (5, 0) <= (local.hour, local.minute) < (19, 0) or options.get("date") != local.date().isoformat():
            return False
        generated = _time(options.get("generated_at"))
        if not generated or generated > now or generated.astimezone(zone).date() != local.date():
            return False
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
        return False
    if (not isinstance(options.get("generation"), str) or not re.fullmatch(r"[a-f0-9]{24}", options["generation"])
            or not isinstance(options.get("options"), list) or len(options["options"]) != 4):
        return False
    numbers = set()
    def plain(value, maximum, *, empty=False):
        return (isinstance(value, str) and (empty or bool(value.strip())) and len(value) <= maximum
                and not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                            or char in "\u2028\u2029" for char in value))
    for row in options["options"]:
        if (not isinstance(row, dict) or set(row) - {"number", "title", "sides", "minutes", "image_data"}
                or type(row.get("number")) is not int or not 1 <= row["number"] <= 4 or row["number"] in numbers
                or not plain(row.get("title"), 80) or not plain(row.get("sides", ""), 120, empty=True)
                or type(row.get("minutes")) is not int or not 1 <= row["minutes"] <= 120):
            return False
        data = row.get("image_data")
        if data is not None and (not isinstance(data, str) or len(data) > 1_100_000 or not data.startswith("data:image/png;base64,")):
            return False
        numbers.add(row["number"])
    return numbers == {1, 2, 3, 4}


def _cards(value):
    if isinstance(value, list):
        return [card for card in value if isinstance(card, dict)]
    if isinstance(value, dict):
        return _cards(value.get("cards", []))
    return []


def _has_source(card):
    source = card.get("source")
    name = source.get("name") if isinstance(source, dict) else source
    url = card.get("source_url") or card.get("url") or (source.get("url") if isinstance(source, dict) else None)
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return False
    return bool(isinstance(name, str) and name.strip() and parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password)


def _fresh_health(card, config, now):
    if card.get("stale") is True or not _has_source(card):
        return False
    published = _time(card.get("published_at") or card.get("source_date"))
    fetched = _time(card.get("fetched_at") or card.get("observed_at") or card.get("updated_at"))
    if not published or not fetched or published > now + timedelta(minutes=5):
        return False
    age = config.get("health_max_age_hours", 168)
    age = age if isinstance(age, (int, float)) and not isinstance(age, bool) and 1 <= age <= 8760 else 168
    return timedelta(minutes=-5) <= now - fetched <= timedelta(hours=age)


def _message_allowed(card, profile=None):
    # Connectors supply metadata only after sender/category filtering. Retain a
    # second explicit category check so arbitrary inbox content cannot slip in.
    category = str(card.get("category") or card.get("source_kind") or "").lower()
    allowed = {"rental", "lawyer"} if profile == "chris" else {"personal"} if profile == "lisa" else set()
    return category in allowed and card.get("filtered", True) is not False


def card_visible(view, card, context, config, now=None):
    """A final renderer gate for a slot retained while presence changes."""
    if not isinstance(card, dict) or not isinstance(context, dict) or not _visible(card, _now(now)):
        return False
    private = (config.get("lisa_private_enabled") is True and config.get("guest_mode", False) is False
               and context.get("private_lisa") is True and context.get("lisa_home") is True
               and context.get("chris_away") is True and context.get("kids_away") is True)
    if view == "message":
        profile = card.get("profile") or card.get("owner") or card.get("recipient")
        return config.get("guest_mode", False) is False and config.get("messages_enabled") is True and _message_allowed(card, profile) and bool(
            context.get("chris_home") is True if profile == "chris" else
            private if profile == "lisa" else False)
    if view == "inspiration":
        profile = card.get("profile") or card.get("owner") or config.get("inspiration_profile") or "lisa"
        return config.get("inspiration_enabled") is True and (profile == "public" or profile == "lisa" and private)
    if view == "health":
        return config.get("health_enabled") is True and private and _fresh_health(card, config, _now(now))
    if view == "layover":
        return context.get("chris_away") is True and _visible(card, _now(now), require_window=True)
    if view == "local_visit":
        return config.get("local_visits_enabled", True) is True and _visible(card, _now(now), require_window=True)
    return False


def _operating_map_available(status, selected, config, now):
    """Only an independently fresh, exactly associated provider position."""
    from .flight_alerts import fresh_status
    current = fresh_status(status.get("operating_status"), now)
    live = status.get("operating_live")
    if (config.get("flightaware_enabled") is not True or config.get("demo") is True
            or (status.get("snapshot") or {}).get("demo") is not False
            or not isinstance(selected, dict) or selected.get("deadhead") or not isinstance(live, dict)
            or current.get("status") != "fresh" or current.get("source") != "FlightAware AeroAPI"
            or current.get("phase") not in {"scheduled", "departed_gate", "airborne"}
            or current.get("cancelled") is True or current.get("diverted") is True
            or current.get("flight_id") != selected.get("id") or live.get("flight_id") != selected.get("id")
            or not current.get("provider_flight_id") or current["provider_flight_id"] != live.get("provider_flight_id")
            or (current.get("origin"), current.get("destination")) != (selected.get("origin"), selected.get("destination"))
            or live.get("source") not in {"FlightAware", "FlightAware AeroAPI"}):
        return False
    observed = _time(live.get("observed_at"))
    latitude, longitude = live.get("latitude"), live.get("longitude")
    return bool(observed and timedelta(0) <= now - observed <= timedelta(seconds=300)
                and all(type(value) in (int, float) and math.isfinite(value) for value in (latitude, longitude))
                and -85 <= latitude <= 85 and -180 <= longitude <= 180)


def build_rotation(status, config, now=None):
    """Build stable, ordered slots for the GUI: key/view/duration_seconds/item.

    Individual item slots make independent card expiry and manual selection
    deterministic. Every caller should rebuild the list as presence changes.
    """
    status, config, now = status or {}, config or {}, _now(now)
    context = household_context(status, config, now)
    duration = config.get("rotation_seconds", 15)
    duration = duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 3 <= duration <= 300 else 15
    focus = config.get("flight_focus_seconds", 60)
    focus = focus if isinstance(focus, (int, float)) and not isinstance(focus, bool) and 3 <= focus <= 600 else 60
    entries = []
    def add(view, key=None, item=None, seconds=None, **extra):
        if not card_enabled(view, config, item):
            return
        entry = {"key": key or view, "view": view, "duration_seconds": seconds or duration, **extra}
        if item is not None:
            entry["item"] = dict(item)
        entries.append(entry)

    if status.get("setup_required") is True:
        add("setup", item=status.get("setup") if isinstance(status.get("setup"), dict) else {})
    selected = _priority_flight(status, config, now) if context["flight_priority"] else status.get("selected_flight")
    snapshot = status.get("snapshot") or {}
    preview = trip_preview(snapshot, config, now)
    # Current-flight-only applies to flight views, not the family's short cards.
    # Outside current flight focus, the owner's normal preview remains useful.
    strict = config.get("flight_focus_only") is True and context["flight_priority"]
    operating_map = _operating_map_available(status, selected, config, now)
    if snapshot.get("demo") or context["flight_priority"]:
        views = ("details", "operating_map" if operating_map else "map") + (() if strict else ("schedule",))
        for view in views:
            add(view, seconds=focus if context["flight_priority"] and view in {"details", "map", "operating_map"} else duration,
                selected_flight=selected)
    elif not preview:
        add("home")
    if preview and not strict:
        for page in range((len(preview["flights"]) + 2) // 3):
            for view in ("trip_summary", "trip_map"):
                add(view, f"{view}:{page}", trip_page=page, trip_preview=preview)
    if (config.get("nearby_enabled") is True and not context["flight_priority"]
            and (context["recent_vps_arrival"] or not context["trip_away"] and not status.get("on_duty"))):
        add("nearby")
    if config.get("weather_enabled") is True and snapshot.get("demo") is False:
        add("weather")
    section = status.get("national_radar")
    if (config.get("national_radar_enabled") is True and snapshot.get("demo") is False
            and isinstance(section, dict) and section.get("status") not in {None, "demo", "off", "idle", "outside_window"}):
        add("national_radar")
    from .flight_alerts import fresh_status, within_tracking_window
    operating = fresh_status(status.get("operating_status"), now)
    tracked = status.get("operating_flight")
    if (config.get("flightaware_enabled") is True and snapshot.get("demo") is False
            and isinstance(tracked, dict) and within_tracking_window(tracked, now)
            and (not strict or isinstance(selected, dict) and tracked.get("id") == selected.get("id"))
            and any(flight.get("id") == tracked.get("id") for flight, _, _ in _flights(status, config))):
        add("flight_status", "flight_status:" + str(tracked.get("id") or "current"))
    section = status.get("trip_weather")
    if config.get("trip_weather_enabled") is True and snapshot.get("demo") is False and isinstance(section, dict):
        seen = set()
        for airport in _cards(section.get("airports"))[:4]:
            code = airport.get("iata")
            if (isinstance(code, str) and len(code) == 3 and code.isascii() and code.isalpha() and code.isupper()
                    and code not in seen):
                add("trip_weather", "trip_weather:" + code, item=airport)
                seen.add(code)
    section = status.get("airport_alerts")
    if (config.get("airport_alerts_enabled") is True and snapshot.get("demo") is False
            and isinstance(section, dict) and section.get("status") == "ready" and _cards(section.get("events"))):
        fetched, issued = _time(section.get("fetched_at")), _time(section.get("issued_at"))
        if (fetched and issued and timedelta(0) <= now - fetched < timedelta(seconds=600)
                and timedelta(0) <= now - issued < timedelta(seconds=900)):
            add("airport_alerts")
    outlook = status.get("custody_outlook") or {}
    if config.get("custody_outlook_enabled", True) and outlook.get("days"):
        add("custody_outlook")
    if snapshot.get("demo") is not True and card_enabled("today_activities", config):
        activities = today_activities(outlook, config, now, backend_available=status.get("backend_available", True))
        if activities is not None:
            pages = max(1, math.ceil(len(activities) / ACTIVITIES_PER_PAGE))
            for page in range(pages):
                add("today_activities", f"today_activities:{page}", agenda_page=page)
    if config.get("local_visits_enabled", True):
        for item in _cards(status.get("local_visits")):
            if card_visible("local_visit", item, context, config, now):
                add("local_visit", "local_visit:" + str(item.get("id")), item)
    for item in _cards(status.get("flight_watches"))[:5]:
        add("flight_watch", "flight_watch:" + str(item.get("id")), item)
    if dinner_options_visible(status, config, now):
        add("dinner_options")
    for index, item in enumerate(filter_personal_items(status.get("personal_items"), status, config, now)):
        add("personal", "personal:" + str(item.get("id") or index), item)
    messages = status.get("messages") or {}
    if isinstance(messages, dict) and config.get("messages_enabled") is True and config.get("guest_mode", False) is False:
        for profile, allowed in (("chris", context["chris_home"]), ("lisa", context["private_lisa"])):
            if not allowed:
                continue
            for index, item in enumerate(_cards(messages.get(profile))[:8]):
                if _visible(item, now) and _message_allowed(item, profile):
                    add("message", f"message:{profile}:" + str(item.get("id") or index), {**item, "profile": profile})
    if config.get("inspiration_enabled") is True:
        inspiration = status.get("inspiration", status.get("inspiration_cards", []))
        for index, item in enumerate(_cards(inspiration)[:8]):
            profile = item.get("profile") or item.get("owner") or config.get("inspiration_profile") or "lisa"
            if _visible(item, now) and (profile == "public" or profile == "lisa" and context["private_lisa"]):
                add("inspiration", "inspiration:" + str(item.get("id") or index), item)
    if config.get("health_enabled") is True and context["private_lisa"]:
        for index, item in enumerate(_cards(status.get("health", status.get("health_cards", [])))[:8]):
            if _visible(item, now) and _fresh_health(item, config, now):
                add("health", "health:" + str(item.get("id") or index), item)
    if context["trip_away"]:
        layovers = status.get("layovers") or ([status["layover"]] if isinstance(status.get("layover"), dict) else [])
        for index, item in enumerate(_cards(layovers)[:4]):
            if _visible(item, now, require_window=True):
                add("layover", "layover:" + str(item.get("id") or index), item)
    if not entries:
        add("home")
    if context["flight_priority"]:
        core = {"details", "map", "operating_map"}
        flight_slots = [entry for entry in entries if entry["view"] in core]
        other_slots = [entry for entry in entries if entry["view"] not in core]
        if not flight_slots:
            return other_slots
        if len(other_slots) % 2:
            repeated = dict(other_slots[0])
            repeated["key"] += ":repeat"
            other_slots.append(repeated)
        interleaved = []
        # A flight slot precedes every short card, irrespective of card count.
        # Keep all eligible cards reachable rather than starving later cards
        # behind a permanent first-six truncation.
        for index, item in enumerate(other_slots):
            flight = dict(flight_slots[index % len(flight_slots)])
            flight["key"] += ":priority:" + str(index)
            interleaved.extend((flight, item))
        return interleaved or flight_slots
    return entries
