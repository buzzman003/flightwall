"""Command-line setup and native Batocera Ports entry point."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import sys
import threading
import tempfile

from . import __version__
from .config import ConfigStore, DEFAULT_DATA_DIR, DEFAULT_INSTALL_DIR, private_json


def parser():
    result = argparse.ArgumentParser(description="Flight Wall: calendar-driven LED flight board for Batocera")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--data-dir", default=os.environ.get("FLIGHTWALL_DATA_DIR", DEFAULT_DATA_DIR))
    result.add_argument("--install-dir", default=os.environ.get("FLIGHTWALL_INSTALL_DIR", DEFAULT_INSTALL_DIR))
    commands = result.add_subparsers(dest="command")
    gui = commands.add_parser("gui", help="Launch the TV display (default)")
    gui.add_argument("--windowed", action="store_true")
    gui.add_argument("--frames", type=int, help=argparse.SUPPRESS)
    commands.add_parser("service", help="Run calendar, settings and updates without opening the display")
    focus = commands.add_parser("focus", help="Bring the existing local Flight Wall display to the front")
    focus.add_argument("--ports", action="store_true", help=argparse.SUPPRESS)
    authorize = commands.add_parser("authorize", help="Authorize your own Google Calendar read-only")
    authorize.add_argument("--credentials", required=True, type=Path)
    authorize.add_argument("--no-browser", action="store_true")
    authorize.add_argument("--callback-port", type=int, default=8766)
    authorize.add_argument("--person", choices=["chris", "lisa"], help="Use this person's separate account; omitted keeps the legacy Chris setup")
    calendars = commands.add_parser("calendars", help="List actual calendars visible to the selected Google account")
    calendars.add_argument("--person", choices=["chris", "lisa"], help="Select a separate household account")
    select = commands.add_parser("select-calendar", help="Assign a flight, MiCrew rotation or family calendar role")
    select.add_argument("--name", help="Calendar name; defaults to FC View for flights or Buzz Flight Brief for secondary")
    select.add_argument("--id", dest="calendar_id")
    select.add_argument("--person", choices=["chris", "lisa"], help="Select the Google account that owns this calendar")
    select.add_argument("--role", choices=["flight", "secondary", "mycrew", "family"], default="flight",
                        help="Chris's flight/rotation calendars or either person's family calendar")
    select.add_argument("--station-local", action="store_true",
                        help="For mycrew only, confirm its exported clocks use each airport's local time")
    config = commands.add_parser("config", help="View or change local configuration")
    config.add_argument("--set", nargs=2, action="append", metavar=("KEY", "VALUE"), default=[])
    commands.add_parser("sync", help="Refresh selected calendars from the current account setup")
    commute = commands.add_parser("commute-plan", help="Save a private VPS/ATL schedule plan against an exact roster report")
    commute.add_argument("--date", required=True, help="Central travel date, YYYY-MM-DD")
    commute.add_argument("--candidates", required=True, type=Path, help="Local JSON list of source-dated scheduled flight candidates")
    schedules = commands.add_parser("commute-schedules", help="Read scheduled VPS/ATL clocks through the existing bounded AeroAPI budget")
    schedules.add_argument("--date", required=True, help="Central travel date, YYYY-MM-DD")
    schedules.add_argument("--idents", required=True, nargs="+", help="At most eight exact DL flight numbers")
    travel = commands.add_parser("travel-import", help="Import privately reviewed personal Delta reservations")
    travel.add_argument("--reservations", required=True, type=Path, help="Local JSON list of reviewed reservation records")
    commands.add_parser("travel-list", help="Read the private personal Delta travel store locally")
    recipe = commands.add_parser("recipe-email", help="Privately configure email for future selected recipes")
    commands.add_parser("recipe-email-status", help="Check recipe email readiness without showing private data")
    recipe.add_argument("--sender", required=True)
    recipe.add_argument("--recipient", required=True)
    secret = commands.add_parser("store-secret", help="Store your own optional API credential privately")
    secret.add_argument("name", choices=["flightaware", "openai"])
    secret.add_argument("--file", type=Path)
    remote = commands.add_parser("remote-info", help="Show connection URL and private token file path")
    remote.add_argument("--show-token", action="store_true", help="Explicitly print the private control token locally")
    setup = commands.add_parser("setup-info", help="Show the setup URL and local owner-token file path")
    setup.add_argument("--show-token", action="store_true", help="Explicitly provision/read and print the local owner token")
    presence = commands.add_parser("presence-pair", help="With that person's approval, rotate their private phone-presence token")
    presence.add_argument("person", choices=["chris", "lisa"])
    presence.add_argument("--show-token", action="store_true", help="Explicitly print the newly generated token locally")
    revoke = commands.add_parser("remote-revoke", help="Revoke remote OAuth connections while the service is stopped")
    revoke.add_argument("--rotate-token", action="store_true")
    alexa = commands.add_parser("alexa-info", help="Show the separate Alexa bridge connection details")
    alexa.add_argument("--show-token", action="store_true", help="Explicitly print the private voice-only token locally")
    personal = commands.add_parser("personal", help="Manage household appointments and dinner cards locally")
    personal_commands = personal.add_subparsers(dest="personal_command", required=True)
    add = personal_commands.add_parser("add", help="Create an appointment or dinner card")
    add.add_argument("--kind", choices=["appointment", "dinner"], required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--date")
    add.add_argument("--time")
    add.add_argument("--duration-minutes", type=int)
    listing = personal_commands.add_parser("list", help="List saved or currently visible cards")
    listing.add_argument("--visible", action="store_true")
    cancel = personal_commands.add_parser("cancel", help="Remove a card from the rotation")
    cancel.add_argument("id")
    update = commands.add_parser("update", help="Download and verify a signed update for next launch")
    update.add_argument("--check", action="store_true", help="Check only; do not download")
    commands.add_parser("activate-pending", help="Internal stable-launcher update helper")
    commands.add_parser("rollback-update", help="Internal stable-launcher recovery helper")
    local_stage = commands.add_parser("stage-local-release", help="Internal explicit installer upgrade helper")
    local_stage.add_argument("--release-version", required=True)
    commands.add_parser("diagnose", help="Report version and setup state without printing credentials")
    preview = commands.add_parser("render-preview", help="Export a clearly labeled demo image, without a screen")
    preview.add_argument("--view", choices=["details", "map", "schedule", "nearby", "dinner", "appointment"], default="map")
    preview.add_argument("--route", choices=["atl-mem", "atl-lax"], default="atl-mem")
    preview.add_argument("--output", required=True, type=Path)
    preview.add_argument("--led", action="store_true")
    preview.add_argument("--scale", default=4, type=int)
    return result


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=True), flush=True)


def _value(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _mirror_legacy_chris(store):
    """A successful old-style reauthorization replaces stale migrated credentials."""
    account = store.data_dir / "accounts" / "chris"
    for name in ("google-client.json", "google-token.json"):
        path = store.data_dir / name
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("The saved Google credentials could not be read")
            account.mkdir(parents=True, exist_ok=True, mode=0o700)
            account.parent.chmod(0o700)
            account.chmod(0o700)
            private_json(account / name, value)


def _scoped_calendars_selected(store):
    config = store.get()
    return bool(config.get("secondary_calendar_id") or config.get("family_calendar_id") or config.get("mycrew_calendar_id") or
                any((store.data_dir / "accounts" / person / "google-token.json").exists()
                    for person in ("chris", "lisa")))


def _select_calendar(store, name=None, calendar_id=None, *, person=None, role="flight", station_local=False):
    from .calendar_client import CalendarClient, CalendarError
    if role not in {"flight", "secondary", "mycrew", "family"}:
        raise CalendarError("Choose calendar role flight, secondary, mycrew or family")
    if role == "mycrew" and (not station_local or not (name or calendar_id)):
        raise CalendarError("Select a verified calendar for MiCrew and confirm its airport-local clocks with --station-local.")
    if station_local and role != "mycrew":
        raise CalendarError("Use --station-local only when selecting the MiCrew role.")
    legacy = person is None and role == "flight"
    manager = None
    if legacy:
        calendars = CalendarClient(store.data_dir, store.get()).list_calendars()
    else:
        from .household_calendar import HouseholdCalendars
        person = person or (store.get().get("family_calendar_profile", "chris") if role == "family" else "chris")
        if role in {"flight", "secondary", "mycrew"} and person != "chris":
            raise CalendarError("Flight calendar roles use Chris's account. Use --role family for Lisa's family calendar.")
        manager = HouseholdCalendars(store.data_dir, store.get)
        calendars = manager.list_calendars(person)
    if name is None:
        name = ("FC View" if role == "flight" else "Buzz Flight Brief" if role == "secondary"
                else store.get().get("family_calendar_name", ""))
    matches = [item for item in calendars if item["id"] == calendar_id] if calendar_id else [item for item in calendars if item["name"].strip().casefold() == name.strip().casefold()]
    if len(matches) != 1:
        raise CalendarError("Calendar selection is not unique or was not found. Run calendars, then select-calendar --id YOUR_CALENDAR_ID.")
    item = matches[0]
    if not legacy:
        if role == "family":
            store.update({"family_calendar_profile": person, "family_calendar_id": item["id"],
                          "family_calendar_name": item["name"], "kids_filter_enabled": True})
        elif role == "secondary":
            store.update({"secondary_calendar_id": item["id"], "secondary_calendar_name": item["name"], "demo": False})
        elif role == "mycrew":
            from .mycrew import STATION_LOCAL
            if item["id"] == store.get().get("family_calendar_id"):
                raise CalendarError("Choose a flight calendar for MiCrew rotation notes, separate from the custody source.")
            if not store.get().get("airline"):
                raise CalendarError("Set your operating airline locally before selecting numeric MiCrew flight notes.")
            store.update({"mycrew_calendar_id": item["id"], "mycrew_clock": STATION_LOCAL})
        else:
            store.update({"calendar_id": item["id"], "calendar_name": item["name"], "timezone": item["timezone"], "demo": False})
        synced = manager.sync()
        result = {"selected": item["name"], "person": person, "role": role,
                  "timezone": item["timezone"], "demo": store.get()["demo"]}
        if role == "family":
            result.update(kids=synced["kids"], error=synced["kids"].get("error"))
        else:
            snapshot = synced["flight_snapshot"]
            result.update(flights=len(snapshot["flights"]), error=snapshot.get("error"))
        return result
    store.update({"calendar_id": item["id"], "calendar_name": item["name"], "timezone": item["timezone"], "demo": False})
    snapshot = CalendarClient(store.data_dir, store.get()).sync()
    return {"selected": item["name"], "timezone": item["timezone"], "demo": False,
            "flights": len(snapshot["flights"]), "error": snapshot.get("error")}


def _write_secret(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            else:
                Path(temporary).chmod(0o600)
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _owner_token(path):
    """Provision only for an explicitly requested local token-display command."""
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
    path.chmod(0o600)
    value = path.read_text(encoding="ascii").strip()
    if len(value) < 32:
        raise ValueError("The local owner token is invalid; repair it before starting setup")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "focus":
            if args.ports:
                from .window_focus import reopen_from_ports
                outcome = reopen_from_ports(args.install_dir, args.data_dir)
                emit({"focused": outcome == "focused", "ports": outcome})
                return {"focused": 0, "released": 10, "unavailable": 1}[outcome]
            from .window_focus import focus_window
            focused = focus_window(args.install_dir, args.data_dir)
            emit({"focused": focused})
            return 0 if focused else 1
        store = ConfigStore(args.data_dir)
        command = args.command or "gui"
        if command == "config":
            if args.set:
                store.update({key: _value(value) for key, value in args.set})
            emit(store.get())
        elif command == "authorize":
            from .calendar_client import CalendarClient, CalendarError
            if args.person:
                from .household_calendar import HouseholdCalendars
                result = HouseholdCalendars(args.data_dir, store.get).authorize(args.person, args.credentials,
                    open_browser=not args.no_browser, callback_port=args.callback_port)
                emit({**result, "next_step": "Choose a calendar with select-calendar --person PERSON --role ROLE --id CALENDAR_ID."})
            else:
                result = CalendarClient(args.data_dir, store.get()).authorize(args.credentials, open_browser=not args.no_browser, callback_port=args.callback_port)
                _mirror_legacy_chris(store)
                emit(result)
                try:
                    emit(_select_calendar(store))
                except CalendarError as exc:
                    print(str(exc), file=sys.stderr)
                    print("Authorization is saved. Choose the calendar using select-calendar.", file=sys.stderr)
        elif command == "calendars":
            if args.person:
                from .household_calendar import HouseholdCalendars
                emit(HouseholdCalendars(args.data_dir, store.get).list_calendars(args.person))
            else:
                from .calendar_client import CalendarClient
                emit(CalendarClient(args.data_dir, store.get()).list_calendars())
        elif command == "select-calendar":
            emit(_select_calendar(store, args.name, args.calendar_id, person=args.person, role=args.role,
                                  station_local=args.station_local))
        elif command == "sync":
            if _scoped_calendars_selected(store):
                from .household_calendar import HouseholdCalendars
                snapshot = HouseholdCalendars(args.data_dir, store.get).sync()["flight_snapshot"]
            else:
                from .calendar_client import CalendarClient
                snapshot = CalendarClient(args.data_dir, store.get()).sync()
            emit({"flights": len(snapshot["flights"]), "last_sync": snapshot["last_sync"], "source": snapshot["source"], "error": snapshot["error"]})
            return 1 if snapshot["error"] else 0
        elif command == "commute-plan":
            from .backend import Runtime
            from .commute import CommuteStore, report_leg
            if args.candidates.is_symlink() or not args.candidates.is_file() or args.candidates.stat().st_size > 32768:
                raise ValueError("Choose a regular local commute-candidates JSON file under 32 KB")
            candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
            snapshot = Runtime(args.data_dir, args.install_dir).snapshot()
            anchor = report_leg(snapshot, args.date, datetime.now(timezone.utc), store.get())
            if anchor is None:
                raise ValueError("The first scheduled ATL leg needs an exact roster report clock")
            result = CommuteStore(args.data_dir).save(candidates, anchor, datetime.now(timezone.utc))
            emit(result)
            return 0 if result["status"] == "ready" else 1
        elif command == "commute-schedules":
            from .commute import lookup_schedules
            from .feeds import FlightFeeds
            emit(lookup_schedules(FlightFeeds(store.data_dir), store.get(), args.date, args.idents, datetime.now(timezone.utc)))
        elif command in {"travel-import", "travel-list"}:
            from .personal_travel import PersonalTravel
            travel = PersonalTravel(store.data_dir, store.get)
            emit(travel.import_file(args.reservations) if command == "travel-import"
                 else {"reservations": travel.list_records()})
        elif command == "recipe-email":
            from .config import private_json
            from .recipe_mail import validate_mail_settings
            if not sys.stdin.isatty():
                raise ValueError("Use a private interactive terminal for the recipe mail password")
            password = getpass.getpass("Gmail app password (hidden): ").replace(" ", "")
            settings = validate_mail_settings({"sender": args.sender, "recipient": args.recipient,
                                               "app_password": password})
            private_json(store.data_dir / "recipe-mail.json", settings)
            emit({"configured": True, "note": "Future generated meal selections queue their recipe by email."})
        elif command == "recipe-email-status":
            from .personal import PersonalStore
            from .recipe_mail import RecipeMail
            def enabled():
                config = store.get()
                return config.get("demo") is False and config.get("personal_enabled") is True
            mail = RecipeMail(store.data_dir, PersonalStore(store.data_dir, store.get)._locked, enabled=enabled)
            emit(mail.status())
        elif command == "store-secret":
            prompt = "OpenAI API key (hidden): " if args.name == "openai" else "FlightAware AeroAPI key (hidden): "
            key = args.file.read_text().strip() if args.file else getpass.getpass(prompt).strip()
            if not key or len(key) > 4096 or any(c.isspace() for c in key):
                raise ValueError("The API key is empty or invalid")
            if args.name == "openai" and not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,250}", key):
                raise ValueError("Enter your own OpenAI API key; it is optional and separate from your ChatGPT subscription")
            path = store.data_dir / (args.name + ".key")
            _write_secret(path, key)
            next_step = ("Optional: enable OpenAI locally in setup. API usage is billed through your own OpenAI API account."
                         if args.name == "openai" else "Set your airline code and flightaware_enabled locally; API billing is through your FlightAware account.")
            emit({"stored": True, "path": str(path), "next_step": next_step})
        elif command == "setup-info":
            config = store.get()
            origin = config["public_url"] or f"http://127.0.0.1:{config['control_port']}"
            path = store.data_dir / "control-token.txt"
            info = {"setup_url": origin + "/setup", "token_file": str(path), "token_present": path.exists(),
                    "public_https_configured": bool(config["public_url"]),
                    "note": "Open this URL while the service/display runs. Never paste the local owner token into chat."}
            if args.show_token:
                info["control_token"] = _owner_token(path)
                info["token_present"] = True
            emit(info)
        elif command == "presence-pair":
            from .presence import PresenceStore
            presence = PresenceStore(store.data_dir, store.get)
            token = presence.rotate(args.person)
            config = store.get()
            info = {"person": args.person, "paired": True, "enabled": config["presence_enabled"],
                    "token_file": str(presence.token_path(args.person)),
                    "endpoint": config["public_url"] + "/api/v1/presence/" + args.person if config["public_url"] else None,
                    "note": "The old token is revoked and presence is Unknown until a fresh phone report. Use --show-token only in your own terminal."}
            if args.show_token:
                info["presence_token"] = token
            emit(info)
        elif command == "remote-info":
            from .remote import RemoteServer
            token_path = store.data_dir / "control-token.txt"
            if not token_path.exists():
                server = RemoteServer(store.data_dir, store.get, {}, port=0)
                server.stop()
            config = store.get()
            origin = config["public_url"] or f"http://127.0.0.1:{config['control_port']}"
            info = {"mcp_url": origin + "/mcp", "public_https_configured": bool(config["public_url"]),
                    "token_file": str(token_path), "oauth": bool(config["public_url"]),
                    "note": "The service and your configured HTTPS tunnel must be running for internet access."}
            if args.show_token:
                info["control_token"] = token_path.read_text().strip()
            emit(info)
        elif command == "remote-revoke":
            from .display import BackendConnection
            try:
                BackendConnection(store.data_dir, store.get()).status()
            except OSError:
                pass
            else:
                raise ValueError("Stop the Flight Wall service and close the display before revoking connections")
            (store.data_dir / "remote-oauth.json").unlink(missing_ok=True)
            if args.rotate_token:
                (store.data_dir / "control-token.txt").unlink(missing_ok=True)
            emit({"revoked": True, "token_rotated_on_next_start": args.rotate_token})
        elif command == "alexa-info":
            from .remote import RemoteServer
            token_path = store.data_dir / "alexa-token.txt"
            if not token_path.exists():
                server = RemoteServer(store.data_dir, store.get, {}, port=0)
                server.stop()
            config = store.get()
            info = {"enabled": config["alexa_enabled"], "personal_display_enabled": config["personal_enabled"],
                    "base_url": config["public_url"], "token_file": str(token_path),
                    "timezone": config["timezone"], "appointment_duration_minutes": config["appointment_duration_minutes"],
                    "dinner_start": config["dinner_start"], "dinner_end": config["dinner_end"],
                    "note": "Configure your own Alexa skill and HTTPS bridge using docs/ALEXA-SETUP.txt."}
            if args.show_token:
                info["alexa_token"] = token_path.read_text().strip()
            emit(info)
        elif command == "personal":
            from .personal import PersonalStore
            personal = PersonalStore(store.data_dir, store.get)
            if args.personal_command == "add":
                payload = {"kind": args.kind, "title": args.title, "source": "manual"}
                for name in ("date", "time", "duration_minutes"):
                    value = getattr(args, name)
                    if value is not None:
                        payload[name] = value
                emit(personal.create(payload))
            elif args.personal_command == "list":
                emit(personal.visible() if args.visible else personal.list_items())
            else:
                emit(personal.cancel(args.id))
        elif command == "update":
            from .updater import UpdateManager
            updater = UpdateManager(args.install_dir, args.data_dir, store.get())
            status = updater.check() if args.check else updater.stage()
            emit(status)
            return 1 if status.get("status") == "error" else 0
        elif command in {"activate-pending", "rollback-update"}:
            from .updater import activate_pending, rollback
            status = (activate_pending if command == "activate-pending" else rollback)(args.install_dir, args.data_dir)
            emit(status)
            return 1 if status.get("status") == "error" else 0
        elif command == "stage-local-release":
            from .updater import stage_local_release
            status = stage_local_release(args.install_dir, args.data_dir, args.release_version)
            emit(status)
            return 1 if status.get("status") == "error" else 0
        elif command == "diagnose":
            from .display import BackendConnection
            from .household_calendar import HouseholdCalendars
            config = store.get()
            service = False
            try:
                BackendConnection(store.data_dir, config).status()
                service = True
            except OSError:
                pass
            profiles = HouseholdCalendars(store.data_dir, store.get).status().get("connected_profiles", {})
            profile_status = {person: {key: bool(profiles.get(person, {}).get(key, False))
                                      for key in ("credentials_present", "verified", "connected")}
                              for person in ("chris", "lisa")}
            google_files = [(store.data_dir / "google-token.json"),
                            *(store.data_dir / "accounts" / person / "google-token.json" for person in ("chris", "lisa"))]
            emit({"version": __version__, "machine": platform.machine(), "python": platform.python_version(),
                  "libc": platform.libc_ver(), "frozen": bool(getattr(sys, "frozen", False)),
                  "data_dir": str(store.data_dir), "install_dir": str(args.install_dir),
                  "service_reachable": service, "demo": config["demo"], "calendar_name": config["calendar_name"],
                  "google_authorized": any(path.is_file() for path in google_files),
                  "google_profiles": profile_status,
                  "optional_live_key_present": (store.data_dir / "flightaware.key").exists(),
                  "remote_https_configured": bool(config["public_url"]), "signed_updates_enabled": config["updates_enabled"],
                  "alexa_enabled": config["alexa_enabled"], "personal_display_enabled": config["personal_enabled"],
                  "display": os.environ.get("DISPLAY", ""), "wayland_display": os.environ.get("WAYLAND_DISPLAY", "")})
        elif command == "render-preview":
            from .render import demo_snapshot, render_frame
            from .display import led_image
            snapshot = demo_snapshot(datetime.now(timezone.utc))
            selected = next((f for f in snapshot["flights"] if f["destination"] == ("LAX" if args.route == "atl-lax" else "MEM")), snapshot["flights"][0])
            state = {**snapshot, "selected_flight": selected, "view": args.view}
            if args.view in {"dinner", "appointment"}:
                from datetime import timedelta
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(store.get()["timezone"])
                start = datetime(2026, 11, 3, 14, 30, tzinfo=tz) if args.view == "dinner" else datetime(2026, 11, 3, 11, 25, tzinfo=tz)
                end = start.replace(hour=19, minute=0) if args.view == "dinner" else start + timedelta(hours=1)
                item = {"id": "demo-personal", "kind": args.view, "title": "Teriyaki chicken" if args.view == "dinner" else "Emerson / doctor appointment",
                        "start": start.isoformat(), "end": end.isoformat(), "timezone": str(tz), "source": "Alexa",
                        "display_start": start.replace(hour=0, minute=0).isoformat() if args.view == "appointment" else start.isoformat(), "display_end": end.isoformat()}
                shown_at = start.replace(hour=16, minute=0) if args.view == "dinner" else start.replace(hour=9, minute=0)
                state.update(view="personal", now=shown_at.isoformat(), personal_items=[item], selected_personal_item=item)
            frame = render_frame(state, store.get())
            if args.led:
                frame = led_image(frame, store.get(), args.scale)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            frame.save(args.output)
            emit({"output": str(args.output), "width": frame.width, "height": frame.height, "demo": True})
        elif command in {"gui", "service"}:
            from .backend import Runtime
            from .display import BackendConnection, run_gui
            from .updater import mark_healthy
            runtime = None
            connection = BackendConnection(args.data_dir, store.get())
            if command == "service":
                runtime = Runtime(args.data_dir, args.install_dir).start()
                stopped = threading.Event()
                for signum in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(signum, lambda *_: stopped.set())
                try:
                    stopped.wait()
                finally:
                    runtime.stop()
            else:
                try:
                    existing = connection.status()
                    if existing.get("version") != __version__:
                        raise ValueError("The background service is running a different release. Close the display, stop the Flight Wall service, and launch from Ports again.")
                except OSError:
                    runtime = Runtime(args.data_dir, args.install_dir).start()
                windowed, max_frames = getattr(args, "windowed", False), getattr(args, "frames", None)
                foreground_stopped = threading.Event()

                def first_frame():
                    mark_healthy(args.install_dir, args.data_dir, version=__version__)
                    if not windowed and max_frames is None:
                        from .window_focus import guard_foreground
                        threading.Thread(target=guard_foreground, name="flightwall-foreground", daemon=True,
                                         args=(args.install_dir, args.data_dir, foreground_stopped)).start()

                try:
                    result = run_gui(connection, windowed=windowed, max_frames=max_frames, on_healthy=first_frame)
                    emit(result)
                finally:
                    foreground_stopped.set()
                    if runtime:
                        runtime.stop()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from .calendar_client import CalendarError
        if isinstance(exc, (ValueError, CalendarError)):
            print(f"Flight Wall: {exc}", file=sys.stderr)
        else:
            print(f"Flight Wall could not complete this command ({type(exc).__name__}). Run diagnose and check the local launcher log.", file=sys.stderr)
        if os.environ.get("FLIGHTWALL_DEBUG") == "1":
            import traceback
            traceback.print_exc()
        return 1
