"""Pure household display policy. Unknown presence never grants privacy.

Upstream integrations own identity, freshness and card validation. This module
does no I/O and never infers a person's physical presence from a flight roster.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from .outlook import trip_preview


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
    def minutes(key):
        value = config.get(key, 60)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1440 else 60
    before = timedelta(minutes=minutes("flight_window_before_minutes"))
    after = timedelta(minutes=minutes("flight_window_after_minutes"))
    candidates = [entry for entry in _flights(status, config)
                  if entry[1] - before <= now < entry[2] + after]
    if not candidates:
        return None
    def order(entry):
        flight, start, end = entry
        phase = 0 if start <= now < end else 1 if now < start else 2
        return phase, abs((start - now).total_seconds())
    return min(candidates, key=order)[0]


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
    return [dict(item) for item in items or [] if _visible(item, now, require_window=True)
            and item.get("kind") in {"appointment", "dinner"}
            and not (item.get("kind") == "dinner" and config.get("kids_filter_enabled") is True and not context["kids_home"])]


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
        entry = {"key": key or view, "view": view, "duration_seconds": seconds or duration, **extra}
        if item is not None:
            entry["item"] = dict(item)
        entries.append(entry)

    if status.get("setup_required") is True:
        add("setup", item=status.get("setup") if isinstance(status.get("setup"), dict) else {})
    selected = _priority_flight(status, config, now) if context["flight_priority"] else status.get("selected_flight")
    snapshot = status.get("snapshot") or {}
    preview = trip_preview(snapshot, config, now)
    if snapshot.get("demo") or context["flight_priority"]:
        for view in ("details", "map", "schedule"):
            add(view, seconds=focus if context["flight_priority"] and view in {"details", "map"} else duration,
                selected_flight=selected)
    elif not preview:
        add("home")
    if preview:
        for page in range((len(preview["flights"]) + 2) // 3):
            for view in ("trip_summary", "trip_map"):
                add(view, f"{view}:{page}", trip_page=page, trip_preview=preview)
    if config.get("nearby_enabled") is True and not context["trip_away"] and not context["flight_priority"] and not status.get("on_duty"):
        add("nearby")
    if config.get("weather_enabled") is True and snapshot.get("demo") is False:
        add("weather")
    if config.get("custody_outlook_enabled", True) and status.get("custody_outlook", {}).get("days"):
        add("custody_outlook")
    if config.get("local_visits_enabled", True):
        for item in _cards(status.get("local_visits")):
            if card_visible("local_visit", item, context, config, now):
                add("local_visit", "local_visit:" + str(item.get("id")), item)
    for item in _cards(status.get("flight_watches"))[:5]:
        add("flight_watch", "flight_watch:" + str(item.get("id")), item)
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
    if context["flight_priority"]:
        flight_slots = [entry for entry in entries if entry["view"] in {"details", "map"}]
        other_slots = [entry for entry in entries if entry["view"] not in {"details", "map"}]
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
