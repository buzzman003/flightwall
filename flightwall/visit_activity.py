"""Local, day-before ideas for explicitly recognized calendar visits."""
from dataclasses import dataclass
from datetime import date, timedelta
import json
import re
from zoneinfo import ZoneInfo

from .calendar_visibility import event_is_inactive
from .household_calendar import _event_versions, _stamp, event_bounds


PROFILE_FILE = "visit-activity.json"
_CLOCK = r"(?:[01]?[0-9]|2[0-3])[0-5][0-9]"
_CANCELLED = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
_IDEAS = (
    ("Build a tower together and let her knock it down. Take turns making a taller one.",
     "Pack a few large blocks or make a tower with paper cups."),
    ("Draw a silly castle together. Take turns adding a room, a pet or a secret door.",
     "Bring blank paper and washable crayons."),
    ("Make a little picture book. Let her decide what happens on each page.",
     "Bring folded paper and washable crayons."),
    ("Make a paper puppet together and take turns giving it a funny voice.",
     "Pack paper lunch bags and washable crayons."),
    ("Start a pretend picnic and let her choose what each pretend guest gets to eat.",
     "Bring paper plates for pretend snacks. Check with staff before bringing food."),
    ("Draw a zoo together. Take turns making animal sounds and guessing the animal.",
     "Bring paper and crayons."),
    ("Stack the cups into a castle. Let her knock it down and rebuild it her way.",
     "Pack a small stack of paper cups."),
    ("Read a picture book together and let her make up a different ending.",
     "Bring a short picture book she can flip through."),
    ("Draw a comic together. Each person adds one picture to continue the story.",
     "Bring folded paper and washable crayons."),
    ("Play a colors scavenger hunt. Take turns picking a color and pointing out a match.",
     "Nothing to pack. Play in the room together."),
    ("Draw a favorite character together. If screens are allowed, share a short clip first.",
     "Bring paper and crayons. Save a favorite short clip if allowed."),
)


@dataclass(frozen=True)
class VisitProfile:
    title: str
    exact_titles: frozenset[str]
    timed_title: re.Pattern | None


def enabled(config):
    return config.get("demo") is False and config.get("visit_activity_enabled") is True


def permitted(config, context):
    return (enabled(config) and config.get("guest_mode") is False and config.get("presence_enabled") is True
            and isinstance(context, dict) and context.get("chris_home") is True)


def _label(value, limit):
    if isinstance(value, str) and 0 < len(value) <= limit and all(c.isprintable() or c.isspace() for c in value):
        return " ".join(value.split())
    return ""


def load_profile(path):
    """Read bounded, local aliases. Missing or invalid setup disables the card."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
        settings = json.loads(raw) if len(raw) <= 4096 else None
        if not isinstance(settings, dict) or set(settings) - {"title", "exact_titles", "timed_prefix", "timed_suffix"}:
            return None
        title = _label(settings.get("title"), 36)
        exact = settings.get("exact_titles")
        if not title or not isinstance(exact, list) or not 1 <= len(exact) <= 16:
            return None
        aliases = frozenset(_label(item, 160).casefold() for item in exact)
        if "" in aliases or any(_CANCELLED.search(item) or event_is_inactive({"summary": item}) for item in aliases):
            return None
        timed_title = None
        if "timed_prefix" in settings or "timed_suffix" in settings:
            prefix = _label(settings.get("timed_prefix"), 64).casefold()
            suffix = _label(settings.get("timed_suffix"), 64).casefold()
            if not prefix or not suffix or any(
                _CANCELLED.search(part) or event_is_inactive({"summary": part}) for part in (prefix, suffix)
            ):
                return None
            timed_title = re.compile(rf"{re.escape(prefix)} {_CLOCK} to {_CLOCK}m? {re.escape(suffix)}")
        return VisitProfile(title, aliases, timed_title)
    except (OSError, ValueError, RecursionError):
        return None


def matching_dates(events, calendar_zone, board_zone, profile):
    """Project confirmed one-off events to civil dates without retaining event data."""
    days = set()
    for event in _event_versions(events):
        if (event.get("status") != "confirmed" or event_is_inactive(event) or event.get("eventType", "default") != "default"
                or not isinstance(event.get("id"), str) or not event["id"]
                or any(field in event for field in ("recurringEventId", "originalStartTime", "recurrence"))):
            continue
        title = _label(event.get("summary"), 160).casefold()
        if not title or _CANCELLED.search(title) or not (
            title in profile.exact_titles or (profile.timed_title and profile.timed_title.fullmatch(title))
        ):
            continue
        bounds = event_bounds(event, calendar_zone)
        if bounds is not None:
            start, _, all_day = bounds
            days.add(date.fromisoformat(event["start"]["date"]) if all_day else start.astimezone(board_zone).date())
    return frozenset(days)


def tomorrow(now, zone):
    return now.astimezone(zone).date() + timedelta(days=1)


def suggestion(profile, visit_date, synced):
    idea, bring = _IDEAS[visit_date.toordinal() % len(_IDEAS)]
    return {"title": profile.title, "idea": idea, "bring": bring,
            "visit_date": visit_date.isoformat(), "synced": synced}


def visible_activity(activity, now, zone_name="UTC"):
    """Expire the reminder at midnight or when its calendar observation ages out."""
    if not isinstance(activity, dict):
        return None
    if not all(_label(activity.get(key), limit) for key, limit in (("title", 36), ("idea", 105), ("bring", 90))):
        return None
    try:
        if (activity.get("visit_date") == tomorrow(now, ZoneInfo(zone_name)).isoformat()
                and timedelta(0) <= now - _stamp(activity.get("synced")) < timedelta(minutes=30)):
            return activity
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return None
