"""One background owner for calendar refresh, feeds and remote settings."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
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
from .personal import PersonalStore, PersonalError, personal_card_visible
from .dinner_options import DinnerOptions
from .brief_focus import BriefFocus
from .flight_brief_view import brief_rows
from .commute import CommuteStore, attach_commute_plan
from .recipe_mail import RecipeMail
from .household_calendar import HouseholdCalendars
from .presence import PresenceStore
from .messages import MessageFeeds
from .content import SourceContent
from .household_policy import household_context, filter_personal_items, card_visible
from .timers import TimerStore, TimerError
from .shopping import ShoppingAlerts, ShoppingListStore
from .outlook import local_visits, planned_buzz_days, trip_preview
from .flight_watches import FlightWatchStore, tracking_available
from .personal_travel import PersonalTravel, visible_travel
from .weather import WeatherFeed
from .trip_weather import TripWeatherFeed, preview_airports, select_airports, trip_weather_eligible
from .flight_environment import FlightEnvironmentFeed, _radar_context as radar_context
from .flight_context import build_flight_context
from .flight_alerts import SOURCE as FLIGHT_SOURCE, choose_tracking_flight, fresh_status, stamp as flight_stamp


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
        self.weather = WeatherFeed(data_dir, self.config.get)
        self.trip_weather = TripWeatherFeed(data_dir, self.config.get)
        self.environment = FlightEnvironmentFeed(data_dir, self.config.get)
        self.personal = PersonalStore(data_dir, self.config.get)
        self.dinner_options = DinnerOptions(data_dir, self.config.get, self.personal)
        self.brief_focus = BriefFocus(self.data_dir, self.config.get, self.personal._locked, self.dinner_options._key)
        self.commute = CommuteStore(self.data_dir)
        self.recipe_mail = RecipeMail(data_dir, self.personal._locked, enabled=self._recipe_mail_enabled)
        self.timers = TimerStore(data_dir, self.config.get)
        self.flight_watches = FlightWatchStore(data_dir, self.config.get)
        self.personal_travel = PersonalTravel(data_dir, self.config.get)
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
        self._weather_worker = None
        self._trip_weather_worker = None
        self._environment_worker = None
        self._dinner_worker = None
        self._recipe_worker = None
        self._brief_worker = None
        # One transient confirmed arrival, never a location/presence history.
        self._recent_vps_arrival = None
        self._mail_refresh = threading.Event()
        self._content_refresh = threading.Event()

    @staticmethod
    def _calendar_selection(config):
        mycrew_id = config.get("mycrew_calendar_id")
        return (bool(config.get("demo")),
                str(config.get("calendar_id") or config.get("calendar_name") or "FC View"),
                mycrew_id, config.get("mycrew_clock"), config.get("airline") if mycrew_id else None)

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
        snapshot = attach_commute_plan(self.snapshot(), self.data_dir, config, now)
        flight = choose_flight(snapshot, config, now)
        operating = choose_tracking_flight(snapshot, config, now, self.feeds.operating_status)
        operating_status = self.feeds.operating_status_for_config(config, operating, now)
        recent_arrival = self._vps_arrival(snapshot, config, operating, operating_status, now)
        if ((operating_status.get("status") == "fresh"
             or config.get("flight_focus_only") is True and operating_status.get("status") == "stale")
                and operating_status.get("phase") in {"departed_gate", "airborne", "landed"}):
            flight = operating
        personal_error = None
        try:
            personal_all = self.personal.list_items(now) if config["personal_enabled"] else []
            personal_items = [item for item in personal_all if personal_card_visible(item, config, now)]
        except PersonalError as exc:
            personal_all, personal_items, personal_error = [], [], str(exc)
        with self.lock:
            live = self.feeds.live
            operating_live = live
            if (not isinstance(operating, dict) or not isinstance(operating_live, dict)
                    or operating_live.get("flight_id") != operating.get("id")
                    or not operating_status.get("provider_flight_id")
                    or operating_live.get("provider_flight_id") != operating_status.get("provider_flight_id")):
                operating_live = None
            if not flight or not live or live.get("flight_id") != flight.get("id"):
                live = None
            status = {"version": __version__, "snapshot": snapshot, "config": self.config.public(),
                    "selected_flight": flight, "on_duty": is_on_duty(flight, config, now),
                    "live": copy.deepcopy(live), "nearby": self.feeds.nearby_for_config(config, now),
                    "nearby_status": self.feeds.nearby_status_for_config(config, now),
                    "updater": copy.deepcopy(self.updater_status), "last_live_error": self.feeds.error,
                    "last_error": self.last_error,
                    "personal_items": personal_items, "personal_error": personal_error}
        status["operating_status"] = operating_status
        status["operating_flight"] = operating
        status["operating_live"] = copy.deepcopy(operating_live)
        status["recent_vps_arrival"] = recent_arrival
        household = self.household.status(now=now)
        status.update(presence=self.presence.snapshot(now), kids=household.get("kids", {}))
        outlook = dict(household.get("custody_outlook", {})) if config["family_calendar_id"] else {}
        if outlook and not config.get("guest_mode"):
            plan = planned_buzz_days(snapshot, config, now)
            if plan:
                outlook.update(buzz_plan=plan, roster_last_sync=snapshot.get("last_sync"))
        status["custody_outlook"] = outlook
        status["local_visits"] = local_visits(snapshot, now) if config["local_visits_enabled"] else []
        status["trip_preview"] = trip_preview(snapshot, config, now)
        try:
            status["weather"] = self.weather.status(now)
        except Exception:
            status["weather"] = {"status": "unavailable", "source": "National Weather Service",
                                 "error": "Weather status is unavailable."}
        try:
            airport_weather = copy.deepcopy(self.trip_weather.status(now))
            if not isinstance(airport_weather, dict) or not isinstance(airport_weather.get("airports"), list):
                raise ValueError("Invalid airport weather status")
            status["trip_weather"] = airport_weather
        except Exception:
            status["trip_weather"] = {"status": "unavailable", "source": "AviationWeather.gov",
                                      "airports": [], "error": "Airport weather status is unavailable."}
        try:
            environment = copy.deepcopy(self.environment.status(now))
            if (not isinstance(environment, dict) or not isinstance(environment.get("radar"), dict)
                    or not isinstance(environment.get("airport_alerts"), dict)
                    or not isinstance(environment["airport_alerts"].get("events"), list)):
                raise ValueError("Invalid flight environment status")
            status["national_radar"] = environment["radar"]
            status["airport_alerts"] = environment["airport_alerts"]
        except Exception:
            status["national_radar"] = {"status": "unavailable", "source": "NOAA",
                                        "image_available": False, "error": "National radar status is unavailable."}
            status["airport_alerts"] = {"status": "unavailable", "source": "FAA",
                                        "events": [], "error": "Airport advisory status is unavailable."}
        # Context is reselected without network I/O. A calendar switch need not
        # wait for the independent workers to discard a previous airport batch.
        try:
            airports = {row["iata"] for row in select_airports(snapshot, now, timezone=config.get("timezone", "UTC"))}
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            airports = set()
        weather_airports = set(airports)
        if trip_weather_eligible(status, config, now):
            weather_airports.update(row["iata"] for row in preview_airports(snapshot, config, now))
        trip_weather = status["trip_weather"]
        trip_weather["airports"] = [row for row in trip_weather.get("airports", [])
                                    if isinstance(row, dict) and row.get("iata") in weather_airports]
        status["airport_alerts"]["events"] = [row for row in status["airport_alerts"].get("events", [])
                                              if isinstance(row, dict) and row.get("airport") in airports]
        if not weather_airports:
            if trip_weather.get("status") not in {"demo", "off"}:
                trip_weather["status"] = "idle"
        if not airports:
            if status["airport_alerts"].get("status") not in {"demo", "off"}:
                status["airport_alerts"]["status"] = "outside_window"
        status["flight_context"] = build_flight_context(status, config, now)
        # Global radar has a broader day/day-before context, including shown
        # deadheads and legs without a supported METAR station. Do not couple
        # it to the narrower operating-airport weather/advisory selection.
        try:
            radar_eligible = radar_context(snapshot, config, now)
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            radar_eligible = False
        if not radar_eligible:
            if status["national_radar"].get("status") not in {"demo", "off"}:
                status["national_radar"]["status"] = "outside_window"
            status["national_radar"].update(image_available=False, image_sha256=None)
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
        status["personal_travel"] = []
        if (snapshot.get("demo") is False and config.get("guest_mode") is False
                and config.get("personal_travel_cards_visible") is True):
            try:
                status["personal_travel"] = visible_travel(self.personal_travel.list_records(), config, now)
            except ValueError:
                pass
        status["personal_items"] = filter_personal_items(personal_items, status, config, now)
        for item in status["personal_items"]:
            if item.get("kind") == "dinner":
                picture = self.dinner_options.picture_for(item, now)
                if picture:
                    item["image_data"] = picture
        try:
            from .personal import dinner_meal_date
            from zoneinfo import ZoneInfo
            today = now.astimezone(ZoneInfo(config["timezone"])).date()
            status["dinner_today_selected"] = any(not item.get("cancelled") and item.get("kind") == "dinner"
                and dinner_meal_date(item, config) == today for item in personal_all)
            if personal_error:
                status["dinner_today_selected"] = True
            status["dinner_options"] = self.dinner_options.status(now)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            status["dinner_today_selected"] = True
            status["dinner_options"] = {"status": "unavailable", "options": []}
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
        setup_url = (config["public_url"] or f"http://127.0.0.1:{config['control_port']}") + "/setup"
        status["setup_required"] = not (self.data_dir / "setup-complete.json").exists()
        setup_body = ("Open the setup page on your phone or laptop to connect calendars and household cards."
                      if config["public_url"] else "Use the setup guide to open this address through a localhost SSH tunnel.")
        status["setup"] = {"url": setup_url, "title": "Make this wall yours", "body": setup_body}
        if self.brief_focus.enabled():
            rows = brief_rows(status.get("flight_context"), config)
            status["brief_focus"] = self.brief_focus.selection((row[0] for row in rows), now.date().isoformat())
        return status

    def _vps_arrival(self, snapshot, config, operating, provider, now):
        """Retain one actual VPS arrival for at most one hour, without presence.

        The calendar identity is internal only. Switching accounts/calendars or
        disabling tracking invalidates this exception immediately. A schedule,
        estimated arrival, diversion or stale first observation cannot create it.
        """
        identity = self._calendar_selection(config), config.get("airline")
        flights = snapshot.get("flights", []) if isinstance(snapshot, dict) else []
        if not isinstance(flights, list):
            flights = []
        enabled = (isinstance(snapshot, dict) and snapshot.get("demo") is False
                   and config.get("flightaware_enabled") is True)
        with self.lock:
            retained = self._recent_vps_arrival
            if (not enabled or not retained or retained[0] != identity
                    or not any(isinstance(row, dict) and row.get("id") == retained[1]["flight_id"]
                               and row.get("destination") == "VPS" and not row.get("deadhead") for row in flights)):
                retained = None
            current = fresh_status(provider, now)
            if (enabled and isinstance(operating, dict) and not operating.get("deadhead")
                    and operating.get("destination") == "VPS" and current.get("status") == "fresh"
                    and current.get("source") == FLIGHT_SOURCE and current.get("phase") in {"landed", "at_gate"}
                    and current.get("flight_id") == operating.get("id")
                    and current.get("provider_flight_id")
                    and current.get("origin") == operating.get("origin") and current.get("destination") == "VPS"
                    and current.get("cancelled") is not True and current.get("diverted") is not True
                    and any(isinstance(row, dict) and row == operating for row in flights)):
                times = current.get("times") if isinstance(current.get("times"), dict) else {}
                basis = "actual_on" if times.get("actual_on") else "actual_in"
                try:
                    arrived = flight_stamp(times.get(basis))
                    if timedelta(0) <= now - arrived < timedelta(hours=1):
                        record = {"flight_id": operating["id"], "destination": "VPS", "arrived_at": arrived.isoformat(),
                                  "basis": basis, "source": FLIGHT_SOURCE}
                        retained = identity, record
                except (TypeError, ValueError, OverflowError):
                    pass
            if retained:
                try:
                    if not timedelta(0) <= now - flight_stamp(retained[1]["arrived_at"]) < timedelta(hours=1):
                        retained = None
                except (TypeError, KeyError, ValueError, OverflowError):
                    retained = None
            self._recent_vps_arrival = retained
            return copy.deepcopy(retained[1]) if retained else None

    def status(self, now=None):
        """Maintenance/MCP never receives Lisa's private cards or mailbox data."""
        status = self.display_status(now)
        status.pop("personal_travel", None)
        status["messages"]["lisa"] = []
        status["health"] = []
        for item in status["personal_items"]:
            item.pop("image_data", None)
        status["custody_outlook"] = {key: value for key, value in status.get("custody_outlook", {}).items()
                                     if key not in {"activities", "activity_last_sync", "buzz_plan", "roster_last_sync"}}
        # Absolute aircraft positions plus home-relative offsets would reveal
        # the private home pin. Maintenance needs only the relative local view;
        # the separate admin TV route retains validated provider coordinates.
        status["nearby"] = [{key: value for key, value in row.items()
                             if key not in {"latitude", "longitude", "lat", "lon"}}
                            if "east_miles" in row or "north_miles" in row else row
                            for row in status["nearby"]]
        shopping = status.get("shopping_list", {})
        status["shopping_list"] = {"enabled": shopping.get("enabled", False),
                                   "visible": shopping.get("visible", False),
                                   "item_count": len(shopping.get("items", []))}
        status["inspiration"] = [{k: v for k, v in item.items() if k != "image_data"}
                                 for item in status["inspiration"] if item.get("owner", item.get("profile")) == "public"]
        ideas = status.get("dinner_options", {})
        status["dinner_options"] = {"status": ideas.get("status"), "date": ideas.get("date"),
                                   "option_count": len(ideas.get("options", []))}
        status.pop("flight_context", None)
        status.pop("brief_focus", None)
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
            raise TimerError("TV timers are disabled")
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
        from .card_preferences import card_enabled
        result = self.personal.create(payload)
        config = self.config.get()
        result["display_enabled"] = (config["personal_enabled"]
                                     and card_enabled("personal", config, result.get("item")))
        return result

    def cancel_personal_item(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"id", "request_id"}:
            raise ValueError("Provide the personal card ID to cancel")
        return self.personal.cancel(payload.get("id"))

    def get_voice_context(self):
        return {**self.personal.voice_context(), **self.dinner_options.voice_context()}

    def select_dinner_option(self, payload):
        from .card_preferences import card_enabled
        def queue(option, day, generation):
            try:
                if self._recipe_mail_enabled():
                    self.recipe_mail.queue(option, day, generation)
            except (OSError, ValueError, TypeError, KeyError, PersonalError):
                pass  # A mail setup or queue error must never undo the saved meal.
        result = self.dinner_options.select(payload, on_selected=queue)
        config = self.config.get()
        result["display_enabled"] = (config["personal_enabled"]
                                     and card_enabled("personal", config, result.get("item")))
        return result

    def sync(self):
        if not self._sync_lock.acquire(blocking=False):
            return
        try:
            config = self.config.get()
            profiles_ready = any((self.data_dir / "accounts" / person / "google-token.json").exists() for person in ("chris", "lisa"))
            if profiles_ready or config["family_calendar_id"] or config["secondary_calendar_id"] or config["mycrew_calendar_id"]:
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
            now = datetime.now(timezone.utc)
            snapshot = self.snapshot()
            roster_selected = choose_flight(snapshot, config, now)
            selected = choose_tracking_flight(snapshot, config, now, self.feeds.operating_status)
            provider = self.feeds.operating_status_for_config(config, selected, now)
            recent = self._vps_arrival(snapshot, config, selected, provider, now)
            context = household_context({"presence": self.presence.snapshot(now), "snapshot": snapshot,
                                         "recent_vps_arrival": recent, "operating_status": provider}, config, now)
            try:
                watches = self.flight_watches.list_watches()
            except (ValueError, OSError):
                watches = []
            try:
                self.feeds.poll(config, selected, on_duty=context["flight_priority"] or
                                (not context["recent_vps_arrival"] and
                                 (is_on_duty(roster_selected, config, now) or context["chris_away"])), watches=watches)
                provider = self.feeds.operating_status_for_config(config, selected, now)
                self._vps_arrival(snapshot, config, selected, provider, now)
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

    def _run_weather(self):
        # Weather has its own bounded network poller and provider cadence.
        # A slow forecast cannot delay calendar refresh, controls, or rendering.
        while not self.stop_event.is_set():
            try:
                self.weather.poll()
            except Exception:
                pass  # Provider/cache errors remain sanitized in weather status.
            self.stop_event.wait(2)

    def _run_trip_weather(self):
        # Airport batches/forecasts cannot block home weather or paid tracking.
        while not self.stop_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                snapshot = self.snapshot()
                config = self.config.get()
                presence = self.presence.snapshot(now)
                self.commute.poll(snapshot, config, self.feeds, now, presence=presence)
                snapshot = attach_commute_plan(snapshot, self.data_dir, config, now)
                context = {"snapshot": snapshot, "presence": presence}
                self.trip_weather.poll(snapshot, now, preview=trip_weather_eligible(context, self.config.get(), now))
            except Exception:
                pass
            self.stop_event.wait(2)

    def _run_environment(self):
        # FAA advisories and national radar have their own bounded provider I/O.
        while not self.stop_event.is_set():
            try:
                self.environment.poll(self.snapshot(), datetime.now(timezone.utc))
            except Exception:
                pass
            self.stop_event.wait(2)

    def _run_dinner_options(self):
        # Slow optional generation cannot block calendars, Alexa or rendering.
        while not self.stop_event.is_set():
            try:
                if self.config.get().get("dinner_options_enabled") is True:
                    kids = self.household.status().get("kids", {})
                    self.dinner_options.poll(kids, datetime.now(timezone.utc))
                    self.dinner_options.poll_picture(kids, datetime.now(timezone.utc))
            except Exception:
                pass  # The generator exposes bounded, nonsecret status only.
            self.stop_event.wait(60)

    def _recipe_mail_enabled(self):
        config = self.config.get()
        return config.get("demo") is False and config.get("personal_enabled") is True

    def _run_brief_focus(self):
        while not self.stop_event.is_set():
            try:
                if self.brief_focus.enabled():
                    now = datetime.now(timezone.utc)
                    status = self.display_status(now)
                    rows = brief_rows(status.get("flight_context"), self.config.get())
                    if not self.stop_event.is_set():
                        self.brief_focus.poll((row[0] for row in rows), now.date().isoformat())
            except Exception:
                pass  # The optional model never blocks the board or logs response content.
            self.stop_event.wait(60)

    def _run_recipe_mail(self):
        while not self.stop_event.is_set():
            try:
                if self._recipe_mail_enabled():
                    self.recipe_mail.poll()
            except Exception:
                pass  # Never log addresses, credentials, or SMTP bodies.
            self.stop_event.wait(5)

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
            "select_dinner_option": self.select_dinner_option,
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
            "get_dinner_options": lambda: self.dinner_options.status(include_recipes=True),
            "focus_display": lambda: self._focus_display(),
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
        self._weather_worker = threading.Thread(target=self._run_weather, name="flightwall-weather", daemon=True)
        self._weather_worker.start()
        self._trip_weather_worker = threading.Thread(target=self._run_trip_weather, name="flightwall-trip-weather", daemon=True)
        self._trip_weather_worker.start()
        self._environment_worker = threading.Thread(target=self._run_environment, name="flightwall-environment", daemon=True)
        self._environment_worker.start()
        self._dinner_worker = threading.Thread(target=self._run_dinner_options, name="flightwall-dinner-options", daemon=True)
        self._dinner_worker.start()
        self._recipe_worker = threading.Thread(target=self._run_recipe_mail, name="flightwall-recipe-mail", daemon=True)
        self._recipe_worker.start()
        self._brief_worker = threading.Thread(target=self._run_brief_focus, name="flightwall-brief-focus", daemon=True)
        self._brief_worker.start()
        return self

    def _focus_display(self):
        from .window_focus import focus_window
        return focus_window(self.install_dir, self.data_dir)

    def stop(self):
        self.stop_event.set()
        if self.remote:
            self.remote.stop()
        if self.worker:
            self.worker.join(timeout=2)
        if self._weather_worker:
            self._weather_worker.join(timeout=2)
        if self._trip_weather_worker:
            self._trip_weather_worker.join(timeout=2)
        if self._environment_worker:
            self._environment_worker.join(timeout=2)
        if self._dinner_worker:
            self._dinner_worker.join(timeout=2)
        if self._recipe_worker:
            self._recipe_worker.join(timeout=2)
        if self._brief_worker:
            self._brief_worker.join(timeout=2)
