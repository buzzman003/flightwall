"""Presence-gated public briefing and private, counts-only guest operations."""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .briefing_public import fetch_events, fetch_headlines
from .feeds import parse_time
from .guest_operations import fetch_operations


SEED_FILE = "guest-ops-snapshot.json"
PUBLIC_SECTIONS = {"headlines": fetch_headlines, "events": fetch_events}
REFRESH = {"headlines": timedelta(minutes=30), "events": timedelta(hours=2),
           "guest_ops": timedelta(minutes=15)}
FRESHNESS = {"headlines": timedelta(hours=3), "events": timedelta(hours=8),
             "guest_ops": timedelta(hours=2)}


def enabled(config):
    return (config.get("demo") is False and config.get("presence_enabled") is True
            and config.get("guest_mode") is False
            and (config.get("home_briefing_enabled") is True or config.get("guest_ops_enabled") is True))


def permitted(config, context):
    return enabled(config) and isinstance(context, dict) and context.get("chris_home") is True


def _day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Use a calendar date")
    return parsed


def _label(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() and bool(value.strip())


def visible_sections(payload, now, timezone_name):
    """Validate a fresh snapshot and project only the presentation fields."""
    if not isinstance(payload, dict):
        return {}
    try:
        today = now.astimezone(ZoneInfo(timezone_name)).date()
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return {}
    result = {}
    for section, max_age in FRESHNESS.items():
        data = payload.get(section)
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list) or len(data["rows"]) > 6:
            continue
        try:
            synced = parse_time(data.get("synced"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not timedelta(0) <= now - synced < max_age:
            continue
        rows = []
        for row in data["rows"]:
            if not isinstance(row, dict):
                continue
            try:
                day = _day(row.get("date"))
            except (TypeError, ValueError):
                continue
            if section == "guest_ops":
                counts = [row.get(key) for key in ("arrivals", "departures")]
                if all(type(n) is int and 0 <= n <= 1000 for n in counts):
                    rows.append({"date": day.isoformat(), "arrivals": counts[0], "departures": counts[1]})
            elif ((today - timedelta(days=3) <= day <= today if section == "headlines"
                   else today <= day <= today + timedelta(days=30))
                  and _label(row.get("title"), 160) and _label(row.get("source"), 48)):
                rows.append({key: row[key] for key in ("title", "source", "date")})
        if section == "guest_ops" and sorted(r["date"] for r in rows) != [
            (today + timedelta(days=offset)).isoformat() for offset in range(4)
        ]:
            continue
        if rows:
            result[section] = {"synced": synced.isoformat(), "rows": rows}
    return result


class HomeBriefing:
    """Refresh on a single worker and expose only validated rows to the TV."""

    def __init__(self, data_dir, config_getter):
        self.data_dir = Path(data_dir)
        self.config = config_getter
        self.sections = {}
        self.attempts = {}

    def _token(self):
        try:
            with (self.data_dir / "hospitable.key").open(encoding="utf-8") as stream:
                token = stream.read(4097).strip()
            if 4 <= len(token) <= 4096 and token.isascii() and token.isprintable() and not any(c.isspace() for c in token):
                return token
        except (OSError, ValueError):
            pass
        return None

    def _seed(self):
        try:
            with (self.data_dir / SEED_FILE).open("rb") as stream:
                raw = stream.read(8193)
            return json.loads(raw) if len(raw) <= 8192 else None
        except (OSError, ValueError, RecursionError):
            return None

    def _due(self, section, now, scope):
        last = self.attempts.get(section)
        if last and last[0] == scope and timedelta(0) <= now - last[1] < REFRESH[section]:
            return False
        self.attempts[section] = scope, now
        return True

    def refresh(self, now):
        config = self.config()
        if not enabled(config):
            return
        zone_name = config["timezone"]
        if config.get("home_briefing_enabled") is True:
            for section, fetch in PUBLIC_SECTIONS.items():
                if self._due(section, now, zone_name):
                    self._refresh_section(section, now, lambda: fetch(now, zone_name))
        if config.get("guest_ops_enabled") is True:
            today = now.astimezone(ZoneInfo(zone_name)).date()
            token = self._token()
            if token:
                binding = hashlib.sha256(token.encode("ascii")).digest()
                if self._due("guest_ops", now, (zone_name, today, binding)):
                    self._refresh_section("guest_ops", now, lambda: fetch_operations(token, today), binding)

    def _refresh_section(self, section, now, fetch, binding=None):
        try:
            rows = fetch()
            if isinstance(rows, list):
                self.sections = self.sections | {section: {"synced": now.isoformat(), "rows": rows, "binding": binding}}
        except (OSError, TypeError, ValueError, OverflowError, RecursionError):
            # Credentials, provider responses and failure details stay out of logs.
            pass

    def snapshot(self, now, config):
        sections = self.sections.copy()
        if config.get("home_briefing_enabled") is not True:
            sections.pop("headlines", None)
            sections.pop("events", None)
        if config.get("guest_ops_enabled") is not True:
            sections.pop("guest_ops", None)
        else:
            token = self._token()
            if not token:
                sections["guest_ops"] = self._seed()
            elif sections.get("guest_ops", {}).get("binding") != hashlib.sha256(token.encode("ascii")).digest():
                sections.pop("guest_ops", None)
        return visible_sections(sections, now, config["timezone"])
