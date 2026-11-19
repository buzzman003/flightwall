"""Chris's primary calendar in memory and his private, local unfinished tasks."""
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID
from zoneinfo import ZoneInfo

from .agenda_google_tasks import AgendaGoogleTasks
from .calendar_client import CalendarError
from .config import private_json
from .household_calendar import _event_versions, _iso, _stamp, event_bounds
from .personal import PersonalStore

MAX_TASKS = 40
MAX_EVENTS = 30
PER_PAGE = 4
RENTAL_STAY_TITLE = re.compile(r"^stay at (?:homeaway|airbnb|vrbo)\b", re.IGNORECASE)


def midnight_after_three_days(day, zone):
    return datetime.combine(day + timedelta(days=3), time.min, zone)


def enabled(config):
    return config.get("demo") is False and config.get("chris_agenda_enabled") is True


def permitted(config, context):
    return (enabled(config) and config.get("guest_mode") is False and config.get("presence_enabled") is True
            and config.get("kids_filter_enabled") is True and isinstance(context, dict)
            and all(context.get(k) is True for k in ("chris_home", "lisa_away", "kids_away")))


def text_label(value):
    return " ".join(value.split()) if isinstance(value, str) and all(
        ch.isprintable() or ch.isspace() for ch in value) else ""


def calendar_rows(events, calendar_zone, now, end_exclusive):
    result = []
    for event in _event_versions(events):
        title = text_label(event.get("summary"))
        if not title or event.get("status") == "cancelled":
            continue
        # Gmail can put a guest's stay on a host's calendar as a self-organized
        # event. That does not establish who is travelling.
        if event.get("eventType") == "fromGmail" and RENTAL_STAY_TITLE.match(title):
            continue
        bounds = event_bounds(event, calendar_zone)
        if bounds is None:
            continue
        start, end, all_day = bounds
        if end > now and start < end_exclusive:
            result.append({"title": title[:100], "start": _iso(start), "end": _iso(end),
                           "date": event["start"]["date"] if all_day else ""})
    return sorted(result, key=lambda row: (row["start"], row["title"]))[:MAX_EVENTS]


def visible_rows(agenda, now, zone_name="UTC"):
    """Expire appointments on the paused board independently of the next sync."""
    if not isinstance(agenda, dict):
        return [], []
    events, tasks = [], []
    def fresh(key):
        try:
            return timedelta(0) <= now - _stamp(agenda.get(key)) < timedelta(minutes=30)
        except (TypeError, ValueError, OverflowError):
            return False

    rows = agenda.get("events")
    zone = ZoneInfo(zone_name)
    end = midnight_after_three_days(now.astimezone(zone).date(), zone)
    for row in rows[:MAX_EVENTS] if fresh("synced") and isinstance(rows, list) else ():
        try:
            if (_stamp(row["end"]) > now and _stamp(row["start"]) < min(end, _stamp(row["end"]))
                    and isinstance(row["date"], str) and isinstance(row["title"], str) and row["title"]):
                events.append(row)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    for field in ("tasks", "google_tasks"):
        rows = agenda.get(field)
        if not isinstance(rows, list) or field == "google_tasks" and not fresh("tasks_synced"):
            continue
        for row in rows[:MAX_TASKS]:
            try:
                if isinstance(row["title"], str) and 0 < len(row["title"]) <= 100 and now < _stamp(row["display_until"]):
                    tasks.append(row)
            except (TypeError, KeyError, ValueError, OverflowError):
                continue
    return events, tasks


def draw_agenda(draw, state, config, now):
    from .display import fresh_household_status
    from .render import CYAN, LINE, MUTED, WHITE, _header, _text
    from .time_format import format_time
    if (state.get("backend_available") is not True or state.get("demo") is not False
            or not permitted(config, fresh_household_status(state, config, now)["household_context"])):
        return None
    events, tasks = visible_rows(state.get("chris_agenda"), now, config.get("timezone", "UTC"))
    page = state.get("agenda_page", 0)
    if type(page) is not int or not 0 <= page * PER_PAGE < max(len(events), len(tasks)):
        return None
    _header(draw, state, now, "CHRIS / MY AGENDA")
    draw.line((258, 54, 258, 220), fill=LINE)
    _text(draw, (15, 53), "UPCOMING", CYAN)
    _text(draw, (272, 53), "UNFINISHED TASKS", CYAN)
    start = page * PER_PAGE
    zone = ZoneInfo(config.get("timezone", "UTC"))
    for index, event in enumerate(events[start:start + PER_PAGE]):
        y = 74 + 38 * index
        clock = (event["date"][5:] + " / ALL DAY" if event["date"] else
                 format_time(_stamp(event["start"]).astimezone(zone), date_format="%a %m/%d"))
        _text(draw, (15, y), clock, MUTED, max_width=233)
        _text(draw, (15, y + 14), event["title"], WHITE, max_width=233)
    shown = tasks[start:start + PER_PAGE]
    for index, row in enumerate(shown):
        y = 79 + 38 * index
        draw.rectangle((272, y + 2, 278, y + 8), outline=CYAN)
        _text(draw, (286, y), row["title"], WHITE, max_width=211)
    return [row["display_id"] for row in shown if "display_id" in row]


class ChrisAgenda:
    def __init__(self, data_dir, config_getter, client_getter):
        self.config = config_getter
        self.client = client_getter
        self.path = Path(data_dir) / "chris-tasks.json"
        self.display_dates_path = Path(data_dir) / "chris-task-display-days.json"
        self.storage = PersonalStore(data_dir, config_getter)
        self.google_tasks = AgendaGoogleTasks(data_dir, config_getter)
        self._tasks_synced = None
        self._calendar = None

    @staticmethod
    def _binding(client):
        # Access tokens rotate normally. Bind to the durable grant in memory
        # so disconnecting or switching accounts invalidates the old titles.
        token = client._token()
        grant = token.get("refresh_token")
        if not isinstance(grant, str) or not grant:
            raise CalendarError("Connect Chris’s primary calendar.")
        return hashlib.sha256(grant.encode()).digest()

    def refresh(self, now):
        if not enabled(self.config()):
            self._calendar = None
            self._tasks_synced = None
            return
        try:
            client = self.client()
            binding = self._binding(client)
            zone = ZoneInfo(self.config()["timezone"])
            end = midnight_after_three_days(now.astimezone(zone).date(), zone)
            result = client.get_events("primary", now, end, include_event_type=True)
            rows = calendar_rows(result["items"], result.get("timezone"), now, end)
            self._calendar = ((client, binding, _iso(now), rows)
                              if enabled(self.config()) and self._binding(client) == binding else None)
        except (OSError, CalendarError, TypeError, ValueError, KeyError, OverflowError):
            self._calendar = None
        self.google_tasks.refresh(now)
        self._tasks_synced = _iso(now)

    def snapshot(self, now):
        if not enabled(self.config()):
            return {}
        calendar = self._calendar
        events, synced = [], None
        if calendar:
            try:
                client, binding, updated, rows = calendar
                if self._binding(client) == binding:
                    synced, events = updated, rows
            except (OSError, CalendarError):
                pass
        zone = ZoneInfo(self.config()["timezone"])
        today = now.astimezone(zone).date()
        tasks = []
        with self.storage._locked():
            first_days = self._display_dates()
            for task in self._read_tasks()["open"]:
                first = first_days.get(task["id"])
                start = date.fromisoformat(first) if first else today
                until = midnight_after_three_days(start, zone)
                if start <= today and now < until:
                    row = {"title": task["title"], "display_until": _iso(until)}
                    if first is None:
                        row["display_id"] = task["id"]
                    tasks.append(row)
        # Copy the last completed refresh timestamp first. A concurrent new
        # batch can expire early, but an old batch cannot gain another lease.
        tasks_synced = self._tasks_synced
        agenda = {"synced": synced, "events": events, "tasks": tasks, "tasks_synced": tasks_synced,
                  "google_tasks": self.google_tasks.visible_tasks(now)}
        agenda["events"], _ = visible_rows(agenda, now, zone.key)
        return agenda

    def _display_dates(self):
        try:
            with self.display_dates_path.open("rb") as stream:
                data = stream.read(8193)
            state = json.loads(data) if len(data) <= 8192 else None
            if not isinstance(state, dict) or len(state) > MAX_TASKS:
                raise ValueError
            for key, day in state.items():
                if not re.fullmatch("[0-9a-f]{32}", key) or date.fromisoformat(day).isoformat() != day:
                    raise ValueError
            return state
        except FileNotFoundError:
            return {}
        except (ValueError, TypeError, OSError):
            raise ValueError("Chris’s task display dates could not be read.") from None

    def mark_displayed(self, ids, now):
        """Record only a first showing, never an inferred completion or TV history."""
        local = {key for key in ids if len(key) == 32}
        if local:
            today = now.astimezone(ZoneInfo(self.config()["timezone"])).date().isoformat()
            with self.storage._locked():
                current_ids = {task["id"] for task in self._read_tasks()["open"]}
                previous = self._display_dates()
                days = {key: day for key, day in previous.items() if key in current_ids}
                for key in local & current_ids:
                    days.setdefault(key, today)
                if days != previous:
                    private_json(self.display_dates_path, days)
        google = [key for key in ids if len(key) == 64]
        if google:
            self.google_tasks.mark_displayed(google, now)

    def _read_tasks(self):
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(80001)
            if len(raw) > 80000:
                raise ValueError
            state = json.loads(raw)
            if (not isinstance(state, dict) or state.keys() != {"open", "completed"}
                    or not isinstance(state["open"], list) or len(state["open"]) > MAX_TASKS
                    or not isinstance(state["completed"], list) or len(state["completed"]) > 256):
                raise ValueError
            ids = list(state["completed"])
            for task in state["open"]:
                if (not isinstance(task, dict) or task.keys() != {"id", "title"}
                        or not isinstance(task["title"], str) or not 1 <= len(task["title"]) <= 100):
                    raise ValueError
                ids.append(task["id"])
            if not all(isinstance(i, str) and re.fullmatch("[0-9a-f]{32}", i) for i in ids) or len(set(ids)) != len(ids):
                raise ValueError
            return state
        except FileNotFoundError:
            return {"open": [], "completed": []}
        except (ValueError, TypeError, OSError):
            raise ValueError("Chris’s task list could not be read.") from None

    def tasks(self):
        with self.storage._locked():
            return self._read_tasks()["open"]

    def edit(self, action, payload):
        try:
            task_id = UUID(payload["id"]).hex
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("Choose a valid task.") from None
        with self.storage._locked():
            state = self._read_tasks()
            found = next((row for row in state["open"] if row["id"] == task_id), None)
            if action == "add":
                title = text_label(payload.get("title"))
                if not 1 <= len(title) <= 100:
                    raise ValueError("Use a task title of 1 to 100 characters.")
                if found and found["title"] != title:
                    raise ValueError("This task was already added with another title.")
                if not found and task_id not in state["completed"]:
                    if len(state["open"]) >= MAX_TASKS:
                        raise ValueError("Complete a task before adding another.")
                    state["open"].append({"id": task_id, "title": title})
            elif action == "complete":
                if found:
                    state["open"].remove(found)
                    state["completed"] = [*state["completed"][-255:], task_id]
                elif task_id not in state["completed"]:
                    raise ValueError("This task is no longer on your list.")
            else:
                raise ValueError("Choose add or complete.")
            private_json(self.path, state)
            return {"tasks": state["open"]}
