"""Validated configuration. Credentials live in separate private files."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .card_preferences import CARD_DEFAULTS

DEFAULT_DATA_DIR = "/userdata/system/configs/flightwall"
DEFAULT_INSTALL_DIR = "/userdata/roms/ports/.data/flightwall"
DEFAULTS = {
    **CARD_DEFAULTS,
    "calendar_name": "FC View", "calendar_id": "", "airline": "", "demo": True,
    "timezone": "UTC", "rotation_seconds": 15, "logical_width": 512, "logical_height": 256,
    "brightness": 0.7, "glow": 0.0, "dot_gap": 0.22, "led_effect": True,
    "show_deadheads": True, "takeover_hours": 18,
    "sleep_start": "23:00", "sleep_end": "06:00", "sleep_enabled": False,
    "nearby_enabled": False, "home_latitude": None, "home_longitude": None,
    "nearby_radius_nm": 50, "nearby_radius_miles": None, "nearby_detailed_map": True, "flightaware_enabled": False,
    "weather_enabled": False, "weather_latitude": None, "weather_longitude": None,
    "trip_weather_enabled": False, "national_radar_enabled": False, "airport_alerts_enabled": False,
    "national_radar_scope": "national", "national_radar_flight_only": True,
    "nearby_enrichment_enabled": False, "nearby_enrichment_monthly_limit_usd": 1.0,
    "flightaware_poll_seconds": 120, "flightaware_daily_limit": 240,
    "flightaware_monthly_limit_usd": 5,
    "updates_enabled": False, "update_manifest_url": "", "update_public_key": "",
    "update_interval_minutes": 15, "public_url": "", "control_port": 8765,
    "personal_enabled": True, "appointment_duration_minutes": 60,
    "dinner_start": "14:30", "dinner_end": "19:00", "alexa_enabled": False,
    "dinner_options_enabled": False, "dinner_options_monthly_limit_usd": 0,
    "dinner_options_preferences": "", "dinner_options_servings": 8, "dinner_options_max_minutes": 60,
    "secondary_calendar_id": "", "secondary_calendar_name": "Buzz Flight Brief",
    "mycrew_calendar_id": "", "mycrew_clock": "",
    "family_calendar_profile": "chris", "family_calendar_id": "", "family_calendar_name": "",
    "kids_filter_enabled": False, "kids_stale_minutes": 30,
    "presence_enabled": False, "presence_ttl_minutes": 90, "home_radius_m": 200,
    "chris_phone_type": "unset", "lisa_phone_type": "unset",
    "flight_focus_seconds": 60, "flight_window_before_minutes": 60, "flight_window_after_minutes": 60,
    "flight_focus_only": False,
    "lisa_private_enabled": False, "guest_mode": False,
    "messages_enabled": False, "chris_gmail_enabled": False, "lisa_gmail_enabled": False,
    "chris_lawyer_senders": "[]", "chris_lawyer_domains": "[]",
    "lisa_mail_senders": "[]", "lisa_mail_domains": "[]", "message_preview_chars": 180,
    "chris_notifications_enabled": False, "lisa_notifications_enabled": False,
    "chris_notification_apps": "[]", "lisa_notification_apps": "[]",
    "inspiration_enabled": False, "inspiration_topics": "", "inspiration_sources": "[]",
    "health_enabled": False, "health_query": "", "health_max_age_hours": 168,
    "openai_enabled": False, "openai_model": "gpt-5-mini", "openai_daily_limit": 5,
    "brief_focus_enabled": False, "card_seconds": "{}", "commute_schedule_idents": "[]",
    "hotel_details_enabled": False,
    "brief_leave_times_enabled": False, "brief_leave_policy": "conflict",
    "display_scale_mode": "fit", "timers_enabled": True,
    "custody_outlook_enabled": True, "custody_outlook_days": 10, "flight_preview_days": 5,
    "planned_trip_days": "[]",
    "local_visits_enabled": True, "shopping_ticker_enabled": True,
    "shopping_ticker_visible": True, "shopping_enabled": False,
    "shopping_lisa_opt_in": False, "shopping_list_source": "flightwall",
    "shopping_stores": "[]",
}
REMOTE_SETTINGS = frozenset({
    "card_seconds",
    *CARD_DEFAULTS, "national_radar_scope", "national_radar_flight_only",
    "airline", "timezone", "rotation_seconds", "brightness", "glow", "dot_gap",
    "led_effect", "show_deadheads", "takeover_hours", "sleep_start", "sleep_end",
    "sleep_enabled", "nearby_enabled", "home_latitude", "home_longitude", "nearby_radius_nm", "nearby_radius_miles",
    "weather_enabled", "nearby_detailed_map",
    "personal_enabled", "appointment_duration_minutes", "dinner_start", "dinner_end",
    "guest_mode", "flight_focus_seconds", "flight_window_before_minutes", "flight_window_after_minutes",
    "flight_focus_only",
    "display_scale_mode", "timers_enabled", "custody_outlook_enabled", "custody_outlook_days", "flight_preview_days",
    "local_visits_enabled", "shopping_ticker_enabled", "shopping_ticker_visible",
})
PUBLIC_FIELDS = (REMOTE_SETTINGS - {"home_latitude", "home_longitude"}) | {
    "demo", "calendar_name", "flightaware_enabled", "updates_enabled", "presence_enabled",
    "kids_filter_enabled", "lisa_private_enabled", "health_enabled", "inspiration_enabled",
    "messages_enabled", "presence_ttl_minutes", "kids_stale_minutes", "health_max_age_hours", "hotel_details_enabled",
    "brief_leave_times_enabled",
    "trip_weather_enabled", "national_radar_enabled", "airport_alerts_enabled", "nearby_enrichment_enabled",
    "dinner_options_enabled",
}
JSON_LIST_FIELDS = frozenset({"chris_lawyer_senders", "chris_lawyer_domains", "lisa_mail_senders", "lisa_mail_domains",
                              "chris_notification_apps", "lisa_notification_apps", "inspiration_sources"})


def private_json(path: Path, value) -> None:
    """Replace a JSON file without leaving a partial file on power loss."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate(key, value):
    if key not in DEFAULTS:
        raise ValueError(f"Unknown setting: {key}")
    original = DEFAULTS[key]
    if key == "commute_schedule_idents":
        from .commute import schedule_idents
        return json.dumps(schedule_idents(value), separators=(",", ":"))
    if key == "planned_trip_days":
        from .mycrew import clean_trip_days
        spans = value
        if isinstance(value, str):
            if len(value) > 8192:
                raise ValueError("Planned trips must be a small JSON list of date spans")
            try:
                spans = json.loads(value)
            except (ValueError, RecursionError):
                raise ValueError("Planned trips must be a JSON list of date spans") from None
        if not isinstance(spans, list) or len(spans) > 32:
            raise ValueError("Choose at most 32 planned trip date spans")
        cleaned = clean_trip_days(spans)
        if len(cleaned) != len(spans):
            raise ValueError("Each planned trip needs unique start_date and exclusive end_date within 31 days")
        return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))
    if key == "card_seconds":
        from .card_preferences import TIMING_LABELS
        values = value
        if isinstance(value, str):
            if len(value) > 2000:
                raise ValueError("Card seconds must be a small JSON object")
            try:
                values = json.loads(value)
            except (ValueError, RecursionError):
                raise ValueError("Card seconds must be a JSON object") from None
        if (not isinstance(values, dict) or set(values) - TIMING_LABELS.keys()
                or any(type(seconds) is not int or not 3 <= seconds <= 300 for seconds in values.values())):
            raise ValueError("Choose a known card and an integer duration from 3 to 300 seconds")
        return json.dumps(values, sort_keys=True, separators=(",", ":"))
    if key == "brief_leave_policy":
        if not isinstance(value, str) or value not in {"conflict", "report_safe", "recommended"}:
            raise ValueError("Choose conflict, report_safe or recommended for Brief leave times")
        return value
    if key == "national_radar_scope":
        if not isinstance(value, str) or value not in {"national", "southeast", "local", "hyperlocal"}:
            raise ValueError("Choose national, southeast, local or hyperlocal radar")
        return value
    if key == "mycrew_clock":
        if not isinstance(value, str) or value not in {"", "station-local"}:
            raise ValueError("MiCrew clocks require explicit station-local confirmation")
        return value
    if isinstance(original, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
    elif key in {"home_latitude", "home_longitude", "weather_latitude", "weather_longitude"}:
        if value is None:
            return value
        limit = 90 if key.endswith("latitude") else 180
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not -limit <= value <= limit:
            raise ValueError(f"{key} must be between {-limit} and {limit}, or null")
    elif key == "nearby_radius_miles":
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0.1 <= value <= 250:
            raise ValueError("nearby_radius_miles must be between 0.1 and 250, or null")
    elif isinstance(original, (int, float)):
        limits = {
            "rotation_seconds": (3, 300), "logical_width": (512, 512), "logical_height": (256, 256),
            "brightness": (0.05, 1), "glow": (0, 1), "dot_gap": (0, 0.65),
            "takeover_hours": (1, 96), "nearby_radius_nm": (1, 250),
            "flightaware_poll_seconds": (60, 3600), "flightaware_daily_limit": (1, 2000),
            "flightaware_monthly_limit_usd": (0, 5),
            "nearby_enrichment_monthly_limit_usd": (0, 1),
            "update_interval_minutes": (5, 1440), "control_port": (1024, 65535),
            "appointment_duration_minutes": (1, 1440),
            "kids_stale_minutes": (5, 1440), "presence_ttl_minutes": (5, 1440), "home_radius_m": (30, 5000),
            "flight_focus_seconds": (15, 300), "flight_window_before_minutes": (0, 240),
            "flight_window_after_minutes": (0, 240), "message_preview_chars": (40, 300),
            "health_max_age_hours": (1, 720), "openai_daily_limit": (1, 100),
            "dinner_options_monthly_limit_usd": (0, 3), "dinner_options_servings": (1, 20),
            "dinner_options_max_minutes": (45, 60),
            "custody_outlook_days": (10, 10), "flight_preview_days": (1, 14),
        }
        low, high = limits[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        if isinstance(original, int) and not isinstance(value, int):
            raise ValueError(f"{key} must be a whole number")
    else:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise ValueError(f"Invalid text for {key}")
        if key == "dinner_options_preferences" and len(value) > 1200:
            raise ValueError("Dinner preferences must contain at most 1200 characters")
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Use a timezone such as America/New_York or UTC") from None
        if key == "family_calendar_profile" and value not in {"chris", "lisa"}:
            raise ValueError("The family calendar profile must be chris or lisa")
        if key == "display_scale_mode" and value not in {"fit", "integer"}:
            raise ValueError("Choose display scale fit or integer")
        if key == "shopping_list_source" and value != "flightwall":
            raise ValueError("Only the Flight Wall shopping list is supported")
        if key == "shopping_stores":
            from .shopping import validate_stores
            validate_stores(value)
        if key in {"chris_phone_type", "lisa_phone_type"} and value not in {"iphone", "android", "unset"}:
            raise ValueError("Choose phone type iphone, android or unset")
        if key in JSON_LIST_FIELDS:
            try:
                items = json.loads(value)
            except ValueError:
                raise ValueError(f"{key} must be a JSON list of strings") from None
            if not isinstance(items, list) or len(items) > 50 or any(not isinstance(item, str) or not item.strip() or len(item) > 512 or any(ord(c) < 32 for c in item) for item in items):
                raise ValueError(f"{key} must contain at most 50 nonempty strings")
            for item in items:
                if key.endswith("_domains") and (not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", item) or "." not in item):
                    raise ValueError("Enter email domains such as example.com")
                if key.endswith("_senders") and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", item):
                    raise ValueError("Enter complete sender email addresses")
                if key == "inspiration_sources":
                    url = urlparse(item)
                    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
                        raise ValueError("Inspiration feeds must use HTTPS URLs without credentials")
            value = json.dumps(list(dict.fromkeys(item.strip() for item in items)), ensure_ascii=True)
        if key == "openai_model" and not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", value):
            raise ValueError("Use an OpenAI model ID such as gpt-5-mini")
        if key in {"sleep_start", "sleep_end", "dinner_start", "dinner_end"}:
            try:
                if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
                    raise ValueError()
                hour, minute = map(int, value.split(":"))
                if len(value) != 5 or not 0 <= hour < 24 or not 0 <= minute < 60:
                    raise ValueError()
            except ValueError:
                raise ValueError(f"{key} must be HH:MM in 24-hour time") from None
        if key == "public_url" and value:
            url = urlparse(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
                raise ValueError("public_url must be an HTTPS origin, such as https://host.example.ts.net")
            value = value.rstrip("/")
        if key == "update_manifest_url" and value:
            url = urlparse(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("The update manifest URL must use HTTPS")
        if key == "airline":
            value = value.strip().upper()
            if value and (not value.isalnum() or not 2 <= len(value) <= 3):
                raise ValueError("airline must be a 2- or 3-character airline code")
    return value


class ConfigStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        self.path = self.data_dir / "config.json"
        self.lock = threading.RLock()
        self._config = dict(DEFAULTS)
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                if not isinstance(saved, dict):
                    raise ValueError("configuration must be an object")
                for key, value in saved.items():
                    if key in DEFAULTS:
                        self._config[key] = validate(key, value)
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError(f"Cannot load {self.path}: {exc}") from None
        else:
            private_json(self.path, self._config)
        self._mtime = self.path.stat().st_mtime_ns

    def get(self):
        with self.lock:
            stamp = self.path.stat().st_mtime_ns
            if stamp != self._mtime:
                saved = json.loads(self.path.read_text())
                candidate = dict(DEFAULTS)
                for key, value in saved.items():
                    if key in DEFAULTS:
                        candidate[key] = validate(key, value)
                self._config, self._mtime = candidate, stamp
            return dict(self._config)

    def public(self):
        return {key: value for key, value in self.get().items() if key in PUBLIC_FIELDS}

    def update(self, settings, *, remote=False):
        if not isinstance(settings, dict) or not settings:
            raise ValueError("Provide one or more settings")
        if remote and any(key not in REMOTE_SETTINGS for key in settings):
            raise ValueError("This setting requires the local CLI")
        checked = {key: validate(key, value) for key, value in settings.items()}
        with self.lock:
            candidate = self.get() | checked
            if candidate["dinner_start"] >= candidate["dinner_end"]:
                raise ValueError("Dinner's end time must be after its start time on the same day")
            if candidate["updates_enabled"] and not (candidate["update_manifest_url"] and candidate["update_public_key"]):
                raise ValueError("Set an update manifest URL and trusted public key before enabling updates")
            if candidate["nearby_enabled"] and (candidate["home_latitude"] is None or candidate["home_longitude"] is None):
                raise ValueError("Set your home latitude and longitude before enabling nearby traffic")
            private_json(self.path, candidate)
            self._config = candidate
            self._mtime = self.path.stat().st_mtime_ns
        return self.public() if remote else self.get()
