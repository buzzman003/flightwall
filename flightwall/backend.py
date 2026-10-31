"""One background owner for calendar refresh, feeds and remote settings."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

from . import __version__
from .calendar_client import CalendarClient, CalendarError
from .config import ConfigStore
from .feeds import FlightFeeds, parse_time
from .remote import RemoteServer
from .render import demo_snapshot
from .updater import UpdateManager
from .personal import PersonalStore, PersonalError
from .household_calendar import HouseholdCalendars
from .presence import PresenceStore
from .messages import MessageFeeds
from .content import SourceContent
from .household_policy import household_context, filter_personal_items, card_visible
from .timers import TimerStore, TimerError
from .shopping import ShoppingAlerts, ShoppingListStore
from .outlook import local_visits, trip_preview
from .flight_watches import FlightWatchStore, tracking_available


def choose_flight(snapshot, config, now=None):
    now = now or datetime.now(timezone.utc)
    candidates = []
    for flight in snapshot.get("flights", []):
        try:
            if not config.get("show_deadheads", True) and flight.get("deadhead"):
                continue
            start, end = parse_time(flight["start"]), parse_time(flight["end"])
            if end > now and end > start:
                candidates.append((start, flight))
        except (ValueError, KeyError, TypeError):
            continue
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def is_on_duty(flight, config, now=None):
    if not flight:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        takeover = parse_time(flight.get("report") or flight["start"])
        return (takeover - now).total_seconds() <= config.get("takeover_hours", 18) * 3600 and now < parse_time(flight["end"])
    except (ValueError, KeyError, TypeError):
        return False


class Runtime:
    def __init__(self, data_dir, install_dir):
        self.data_dir = Path(data_dir)
        self.install_dir = Path(install_dir)
        self.config = ConfigStore(data_dir)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self._snapshot = None
        self._snapshot_selection = None
        self._snapshot_cache_stamp = None
        self._demo_snapshot = demo_snapshot(datetime.now(timezone.utc))
        self.feeds = FlightFeeds(data_dir)
        self.personal = PersonalStore(data_dir, self.config.get)
        self.timers = TimerStore(data_dir, self.config.get)
        self.flight_watches = FlightWatchStore(data_dir, self.config.get)
        self.shopping_list = ShoppingListStore(data_dir)
        self.shopping_alerts = ShoppingAlerts(data_dir, self.config.get, list_source=self.shopping_list)
        self.household = HouseholdCalendars(data_dir, self.config.get)
        self.presence = PresenceStore(data_dir, self.config.get)
        self.messages = MessageFeeds(data_dir, self.config.get)
        self.content = SourceContent(data_dir, self.config.get)
        self.updater_status = {"status": "disabled"}
        self.last_error = None
        self.remote = None
        self.worker = None
        self._sync_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._mail_lock = threading.Lock()
        self._content_lock = threading.Lock()
        self._mail_worker = None
        self._content_worker = None
        self._mail_refresh = threading.Event()
        self._content_refresh = threading.Event()

    @staticmethod
    def _calendar_selection(config):
        return (bool(config.get("demo")),
                str(config.get("calendar_id") or config.get("calendar_name") or "FC View"))

    def _calendar_cache_stamp(self):
        try:
            metadata = (self.data_dir / "calendar-cache.json").stat()
            return metadata.st_mtime_ns, metadata.st_ino, metadata.st_size
        except OSError:
            return None

    def snapshot(self):
        config = self.config.get()
        with self.lock:
            if config["demo"]:
                return copy.deepcopy(self._demo_snapshot)
            household = self.household.status().get("flight_snapshot", {})
            if self.household.path.exists() and household.get("last_sync") is not None:
                selection = self._calendar_selection(config)
                if selection != self._snapshot_selection:
                    self.refresh_event.set()
                    self._snapshot_selection = selection
                return copy.deepcopy(household)
            selection = self._calendar_selection(config)
            stamp = self._calendar_cache_stamp()
            changed = selection != self._snapshot_selection
            if self._snapshot is None or changed or stamp != self._snapshot_cache_stamp:
                self._snapshot = CalendarClient(self.data_dir, config).snapshot()
                self._snapshot_selection = selection
                self._snapshot_cache_stamp = stamp
                if changed:
                    self.refresh_event.set()
            return copy.deepcopy(self._snapshot)

    def display_status(self, now=None):
        now = now or datetime.now(timezone.utc)
        config = self.config.get()
        snapshot = self.snapshot()
        flight = choose_flight(snapshot, config, now)
        personal_error = None
        try:
            personal_items = self.personal.visible(now) if config["personal_enabled"] else []
        except PersonalError as exc:
            personal_items, personal_error = [], str(exc)
        with self.lock:
            live = self.feeds.live
            if not flight or not live or live.get("flight_id") != flight.get("id"):
                live = None
            status = {"version": __version__, "snapshot": snapshot, "config": self.config.public(),
                    "selected_flight": flight, "on_duty": is_on_duty(flight, config, now),
                    "live": copy.deepcopy(live), "nearby": copy.deepcopy(self.feeds.nearby),
                    "updater": copy.deepcopy(self.updater_status), "last_live_error": self.feeds.error,
                    "last_error": self.last_error,
                    "personal_items": personal_items, "personal_error": personal_error}
        household = self.household.status(now=now)
        status.update(presence=self.presence.snapshot(now), kids=household.get("kids", {}))
        status["custody_outlook"] = household.get("custody_outlook", {}) if config["family_calendar_id"] else {}
        status["local_visits"] = local_visits(snapshot, now) if config["local_visits_enabled"] else []
        status["trip_preview"] = trip_preview(snapshot, config, now)
        try:
            results = copy.deepcopy(self.feeds.watch_results)
            available = tracking_available(self.data_dir, config)
            status["flight_watches"] = [{**watch, **results.get(watch["id"],
                 {"status": "pending" if available else "unconfigured"})}
                 for watch in self.flight_watches.list_watches(now)]
        except (ValueError, OSError):
            status["flight_watches"] = []
        try:
            status["timers"] = self.timers.list_timers(now) if config["timers_enabled"] else []
        except TimerError:
            status["timers"] = []
        try:
            status["shopping_list"] = self.get_shopping_list()
        except (ValueError, OSError):
            status["shopping_list"] = {"items": [], "enabled": False, "visible": False}
        context = household_context(status, config, now)
        status["household_context"] = context
        status["personal_items"] = filter_personal_items(personal_items, status, config, now)
        status["messages"] = {"chris": [], "lisa": []}
        for person in ("chris", "lisa"):
            eligible = context["chris_home"] if person == "chris" else context["private_lisa"]
            if eligible and config["messages_enabled"]:
                candidates = self.messages.status(person).get("items", [])
                status["messages"][person] = [item for item in candidates if card_visible("message", item, context, config, now)]
        source_content = self.content.status()
        status["inspiration"] = [item for item in source_content["inspiration"] if card_visible("inspiration", item, context, config, now)]
        status["health"] = [item for item in source_content["health"] if card_visible("health", item, context, config, now)]
        for item in status["inspiration"]:
            if item.get("image_id"):
                data = self.content.image_data(item)
                if data:
                    item["image_data"] = data
        status["layovers"] = self.content.layovers(snapshot, now) if context["chris_away"] else []
        if context["chris_away"]:
            status["nearby"] = []
        setup_url = (config["public_url"] or f"http://127.0.0.1:{config['control_port']}") + "/setup"
        status["setup_required"] = not (self.data_dir / "setup-complete.json").exists()
        setup_body = ("Open the setup page on your phone or laptop to connect calendars and household cards."
                      if config["public_url"] else "Use the setup guide to open this address through a localhost SSH tunnel.")
        status["setup"] = {"url": setup_url, "title": "Make this wall yours", "body": setup_body}
        return status

    def status(self, now=None):
        """Maintenance/MCP never receives Lisa's private cards or mailbox data."""
        status = self.display_status(now)
        status["messages"]["lisa"] = []
        status["health"] = []
        shopping = status.get("shopping_list", {})
        status["shopping_list"] = {"enabled": shopping.get("enabled", False),
                                   "visible": shopping.get("visible", False),
                                   "item_count": len(shopping.get("items", []))}
        status["inspiration"] = [{k: v for k, v in item.items() if k != "image_data"}
                                 for item in status["inspiration"] if item.get("owner", item.get("profile")) == "public"]
        return status

    def setup_get(self):
        household = self.household.status()
        content = self.content.status()
        return {"version": __version__, "profiles": household["connected_profiles"],
                "kids": household["kids"], "presence": self.presence.snapshot(),
                "messages": {person: {key: value for key, value in self.messages.status(person).items() if key != "items"}
                             for person in ("chris", "lisa")},
                "research": {"last_sync": content.get("research_last_sync"), "error": content.get("research_error")},
                "setup_complete": (self.data_dir / "setup-complete.json").exists(),
                "remote": {"https_configured": bool(self.config.get()["public_url"]), "service_running": self.remote is not None}}

    def setup_save(self, patch):
        result = self.config.update(patch)
        self.refresh_event.set()
        self._mail_refresh.set()
        self._content_refresh.set()
        return {"saved": True}

    def update_presence(self, person, payload):
        result = self.presence.update(person, payload)
        if result.get("updated") and person == "lisa":
            # Only fresh authenticated reports accepted by PresenceStore reach
            # the opt-in alert engine. Coordinates are never written here.
            try:
                self.shopping_alerts.consume_location(person, payload)
            except (ValueError, OSError):
                pass
        return result

    def get_timers(self):
        return {"timers": self.timers.list_timers()}

    def create_flight_watch(self, payload):
        return self.flight_watches.create(payload)

    def start_timer(self, payload):
        if not self.config.get()["timers_enabled"]:
            raise ValueError("TV timers are disabled")
        return self.timers.start(payload)

    def cancel_timer(self, payload):
        return self.timers.cancel(payload.get("id"))

    def get_shopping_list(self):
        config = self.config.get()
        return {**self.shopping_list.snapshot(), "enabled": config["shopping_ticker_enabled"],
                "visible": config["shopping_ticker_visible"]}

    def _shopping_edit(self, operation, payload):
        result = getattr(self.shopping_list, operation)(payload)
        return {"changed": result.get("changed", False), **self.get_shopping_list()}

    def clear_shopping_list(self, payload):
        return self._shopping_edit("clear", {"confirmation": payload.get("confirmed"),
                                              **({"request_id": payload["request_id"]} if "request_id" in payload else {})})

    def show_shopping_list(self, payload):
        visible = payload.get("visible", True)
        if type(visible) is not bool:
            raise ValueError("Choose whether the shopping list is visible")
        self.config.update({"shopping_ticker_visible": visible})
        return {"changed": True, **self.get_shopping_list()}

    def receive_notification(self, person, payload):
        return self.messages.receive_notification(person, payload)

    def update_settings(self, patch):
        result = self.config.update(patch, remote=True)
        return {"updated": True, "config": result}

    def refresh_calendar(self):
        self.refresh_event.set()
        self._mail_refresh.set()
        self._content_refresh.set()
        return {"queued": True, "message": "Calendar refresh queued."}

    def create_personal_item(self, payload):
        result = self.personal.create(payload)
        result["display_enabled"] = self.config.get()["personal_enabled"]
        return result

    def cancel_personal_item(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"id", "request_id"}:
            raise ValueError("Provide the personal card ID to cancel")
        return self.personal.cancel(payload.get("id"))

    def get_voice_context(self):
        return self.personal.voice_context()

    def sync(self):
        if not self._sync_lock.acquire(blocking=False):
            return
        try:
            config = self.config.get()
            profiles_ready = any((self.data_dir / "accounts" / person / "google-token.json").exists() for person in ("chris", "lisa"))
            if profiles_ready or config["family_calendar_id"] or config["secondary_calendar_id"]:
                result = self.household.sync()
                if not config["demo"]:
                    with self.lock:
                        self._snapshot = result["flight_snapshot"]
                        self.last_error = None
                return
            if config["demo"]:
                return
            snapshot = CalendarClient(self.data_dir, config).sync()
            with self.lock:
                if self._calendar_selection(config) != self._calendar_selection(self.config.get()):
                    # A separate authorize/select-calendar command may finish
                    # while the previous calendar's network request is running.
                    self.refresh_event.set()
                    return
                self._snapshot = snapshot
                self._snapshot_selection = self._calendar_selection(config)
                self._snapshot_cache_stamp = self._calendar_cache_stamp()
                self.last_error = None
        except CalendarError as exc:
            with self.lock:
                config = self.config.get()
                cached = CalendarClient(self.data_dir, config).snapshot()
                cached["error"] = str(exc)
                self._snapshot = cached
                self._snapshot_selection = self._calendar_selection(config)
                self._snapshot_cache_stamp = self._calendar_cache_stamp()
        except Exception:
            with self.lock:
                self.last_error = "Calendar refresh failed. Run diagnose locally; cached flights remain available."
        finally:
            self._sync_lock.release()

    def check_updates(self):
        config = self.config.get()
        if not config["updates_enabled"]:
            return {"status": "disabled", "message": "Configure a signed release channel locally first."}
        if not self._update_lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            status = UpdateManager(self.install_dir, self.data_dir, config).stage()
            with self.lock:
                self.updater_status = status
            return status
        except Exception:
            with self.lock:
                self.updater_status = {"status": "error", "message": "Signed update check failed. See local updater status; the current release is unchanged."}
            return self.updater_status
        finally:
            self._update_lock.release()

    def _run(self):
        last_sync, last_update, last_mail, last_content = -1000, time.monotonic(), -1000, -1_000_000
        while not self.stop_event.is_set():
            config = self.config.get()
            tick = time.monotonic()
            if tick - last_sync >= 300 or self.refresh_event.is_set():
                self.refresh_event.clear()
                self.sync()
                last_sync = time.monotonic()
            selected = choose_flight(self.snapshot(), config)
            context = household_context({"presence": self.presence.snapshot(), "snapshot": self.snapshot()}, config)
            try:
                watches = self.flight_watches.list_watches()
            except (ValueError, OSError):
                watches = []
            try:
                self.feeds.poll(config, selected, on_duty=is_on_duty(selected, config) or context["chris_away"], watches=watches)
            except Exception:
                self.feeds.error = "Optional traffic feed is unavailable."
            if config["updates_enabled"] and tick - last_update >= config["update_interval_minutes"] * 60:
                self.check_updates()
                last_update = time.monotonic()
            if tick-last_mail >= 300 or self._mail_refresh.is_set():
                if self._mail_worker is None or not self._mail_worker.is_alive():
                    self._mail_worker = threading.Thread(target=self._sync_messages, name="flightwall-messages", daemon=True)
                    self._mail_worker.start()
                    self._mail_refresh.clear()
                    last_mail = tick
            if tick-last_content >= 21600 or self._content_refresh.is_set():
                if self._content_worker is None or not self._content_worker.is_alive():
                    self._content_worker = threading.Thread(target=self._sync_content, name="flightwall-content", daemon=True)
                    self._content_worker.start()
                    self._content_refresh.clear()
                    last_content = tick
            self.stop_event.wait(2)

    def _sync_messages(self):
        if not self._mail_lock.acquire(blocking=False):
            return
        try:
            config = self.config.get()
            if config["messages_enabled"]:
                for person in ("chris", "lisa"):
                    if not self.stop_event.is_set() and config[f"{person}_gmail_enabled"]:
                        self.messages.sync(person)
        except Exception:
            pass  # The feed keeps its own sanitized cache/error; flights remain available.
        finally:
            self._mail_lock.release()

    def _sync_content(self):
        if not self._content_lock.acquire(blocking=False):
            return
        try:
            self.content.refresh()
            # Design interests are the only input used for optional AI generation.
            # Neither mailbox contents nor health topics are sent to this endpoint.
            config = self.config.get()
            if config["openai_enabled"] and config["inspiration_enabled"] and config["inspiration_topics"]:
                self.content.generate_inspiration()
        except Exception:
            pass
        finally:
            self._content_lock.release()

    def start(self):
        self.remote = RemoteServer(self.data_dir, self.config.get, {
            "get_status": self.status, "update_settings": self.update_settings,
            "refresh_calendar": self.refresh_calendar, "check_updates": self.check_updates,
            "create_personal_item": self.create_personal_item,
            "cancel_personal_item": self.cancel_personal_item,
            "get_voice_context": self.get_voice_context,
            "start_timer": self.start_timer, "cancel_timer": self.cancel_timer, "get_timers": self.get_timers,
            "create_flight_watch": self.create_flight_watch,
            "get_shopping_list": self.get_shopping_list,
            "add_shopping_item": lambda payload: self._shopping_edit("add", payload),
            "remove_shopping_item": lambda payload: self._shopping_edit("remove", payload),
            "clear_shopping_list": self.clear_shopping_list, "show_shopping_list": self.show_shopping_list,
            "get_display_status": self.display_status,
            "update_presence": self.update_presence,
            "receive_notification": self.receive_notification,
            "setup_get": self.setup_get, "setup_save": self.setup_save,
            "setup_calendars": self.household.list_calendars,
            "setup_kids_preview": self.household.preview_kids,
            "setup_refresh": self.refresh_calendar,
            "content_add": self.content.add_card,
            "content_generate": self.content.generate_inspiration,
            "setup_layover": self.content.add_layover,
        })
        self.remote.start()
        self.worker = threading.Thread(target=self._run, name="flightwall-sync", daemon=True)
        self.worker.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.remote:
            self.remote.stop()
        if self.worker:
            self.worker.join(timeout=2)
