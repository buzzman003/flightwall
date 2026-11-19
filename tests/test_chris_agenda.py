"""Private household decisions must remain true at the moment a TV frame is drawn."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from flightwall import display
from flightwall.backend import Runtime
from flightwall.calendar_client import CalendarClient, CalendarError
from flightwall.chris_agenda import ChrisAgenda, calendar_rows, midnight_after_three_days, visible_rows
from flightwall.config import DEFAULTS
from flightwall.household_calendar import kids_snapshot
from flightwall.household_policy import build_rotation
from flightwall.render import render_frame


NOW = datetime(2026, 10, 22, 18, tzinfo=timezone.utc)
CFG = DEFAULTS | {"demo": False, "presence_enabled": True, "kids_filter_enabled": True,
                  "chris_agenda_enabled": True, "timezone": "America/Chicago"}
BOARD_ZONE = ZoneInfo(CFG["timezone"])


def titles(rows):
    return [row["title"] for row in rows]


def fixture():
    people = {who: {"state": state, "last_seen": NOW.isoformat(), "stale": False}
              for who, state in (("chris", "home"), ("lisa", "away"))}
    return {"config": CFG, "backend_available": True, "snapshot": {"demo": False, "flights": []},
            "presence": people, "kids": {"state": "away", "stale": False, "last_sync": NOW.isoformat(),
                                         "until": (NOW + timedelta(hours=1)).isoformat()},
            "chris_agenda": {"synced": NOW.isoformat(), "events": [], "tasks": [
                {"title": "PRIVATE TEST TASK", "display_until": (NOW + timedelta(days=2)).isoformat()}]}}


def test_custody_lease_expires_on_first_conflicting_start_even_with_a_paused_renderer():
    start = NOW - timedelta(hours=1)
    overlap = NOW + timedelta(seconds=1)
    intervals = [{"parent": parent, "start": at.isoformat(), "end": (NOW + timedelta(hours=2)).isoformat()}
                 for parent, at in (("Dad", start), ("Mom", overlap))]
    base = fixture()
    base["presence"]["chris"]["state"], base["presence"]["lisa"]["state"] = "away", "home"
    base["kids"] = kids_snapshot(intervals, NOW.isoformat(), NOW)
    cfg = CFG | {"lisa_private_enabled": True}
    assert display.fresh_household_status(base, cfg, NOW)["household_context"]["private_lisa"]
    assert not display.fresh_household_status(base, cfg, overlap)["household_context"]["private_lisa"]
    assert not display.fresh_household_status(base, cfg, NOW, backend_available=False)["household_context"]["private_lisa"]
    assert kids_snapshot(intervals, NOW.isoformat(), overlap)["state"] == "unknown"


@pytest.mark.parametrize("cause", ["lisa_home", "chris_away", "kids_home", "stale_away", "stale_calendar",
                                   "guest", "offline", "opt_out"])
def test_agenda_is_excised_from_paused_display_and_rotation(cause):
    state = fixture()
    cfg = dict(CFG)
    initial = display.fresh_household_status(state, cfg, NOW)
    entry, = [e for e in build_rotation(initial, cfg, NOW) if e["view"] == "chris_agenda"]
    frame = display.entry_render_state(initial, cfg, entry, NOW)
    with patch("flightwall.render._text") as drawing:
        render_frame(frame, cfg)
        assert "PRIVATE TEST TASK" in str(drawing.call_args_list)
    if cause == "lisa_home":
        state["presence"]["lisa"]["state"] = "home"
    elif cause == "chris_away":
        state["presence"]["chris"]["state"] = "away"
    elif cause == "kids_home":
        state["kids"]["state"] = "home"
    elif cause == "stale_away":
        state["presence"]["lisa"]["last_seen"] = (NOW - timedelta(days=2)).isoformat()
    elif cause == "stale_calendar":
        state["kids"]["until"] = NOW.isoformat()
    elif cause == "guest":
        cfg["guest_mode"] = True
    elif cause == "opt_out":
        cfg["chris_agenda_enabled"] = False
    else:
        state["backend_available"] = False
    safe = display.fresh_household_status(state, cfg, NOW)
    assert "chris_agenda" not in safe
    assert all(e["view"] != "chris_agenda" for e in build_rotation(safe, cfg, NOW))
    with patch("flightwall.render._text") as drawing:
        render_frame(display.entry_render_state(safe, cfg, entry, NOW), cfg)
        assert "PRIVATE TEST TASK" not in str(drawing.call_args_list)


def event(title, start, end):
    return {"id": title, "summary": title, "description": "UNRELATED PRIVATE BODY",
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}


def test_events_keep_in_progress_and_upcoming_including_all_day_without_bodies_or_cancelled():
    hour = timedelta(hours=1)
    upcoming = event("Next", NOW + hour, NOW + 2 * hour)
    events = [event("Past", NOW - hour, NOW), event("Ongoing", NOW - hour, NOW + hour),
              {"id": "day", "summary": "Day", "start": {"date": "2026-10-22"}, "end": {"date": "2026-10-23"}},
              upcoming, event("Cancelled", NOW, NOW + hour), {"id": "Cancelled", "status": "cancelled"}]
    end = midnight_after_three_days(NOW.astimezone(BOARD_ZONE).date(), BOARD_ZONE)
    events.extend([event("Just inside", end - timedelta(seconds=1), end),
                   event("Too far", end, end + hour)])
    rows = calendar_rows(events, "America/Chicago", NOW, end)
    assert titles(rows) == ["Day", "Ongoing", "Next", "Just inside"]
    assert rows[0]["date"] == "2026-10-22"
    assert all(set(row) == {"title", "start", "end", "date"} for row in rows)


def test_private_agenda_keeps_personal_travel_and_excludes_gmail_rental_stays(tmp_path):
    client = CalendarClient(tmp_path, CFG)
    imported = {"eventType": "fromGmail", "organizer": {"self": True}, "attendees": [{"self": True}]}
    events = [{"id": str(i), "summary": title, "start": {"date": "2026-10-22"},
               "end": {"date": "2026-10-24"}, **source}
              for i, (title, source) in enumerate([
                  ("Stay at HomeAway Example", imported), ("STAY AT  Airbnb Example", imported),
                  ("Stay at Vrbo", imported), ("Stay at HomeAway Example", {"eventType": "default"}),
                  ("Flight to Example City", imported), ("Stay at Example Hotel", imported),
                  ("Airbnb host meeting", imported), ("Practice: Home and Away", {}),
                  ("Stay at HomeAwayville", imported)])]
    client._token = Mock(return_value={"refresh_token": "fixture-only"})
    agenda = ChrisAgenda(tmp_path, lambda: CFG, lambda: client)
    with patch.object(client, "_api", return_value={"items": events, "timeZone": "America/Chicago"}) as api:
        agenda.refresh(NOW)
    fields = api.call_args.args[1]["fields"]
    assert "eventType" in fields
    assert all(field not in fields for field in ("description", "attendees", "organizer", "location"))
    assert titles(agenda.snapshot(NOW)["events"]) == [
        "Airbnb host meeting", "Flight to Example City", "Practice: Home and Away",
        "Stay at Example Hotel", "Stay at HomeAway Example", "Stay at HomeAwayville"]


def test_agenda_reauth_and_network_failure_remove_private_events_and_retain_tasks(tmp_path):
    cfg = dict(CFG)
    client = Mock()
    client._token.return_value = {"refresh_token": "offline-test-grant"}
    client.get_events.return_value = {"items": [event("Private appointment", NOW, NOW + timedelta(hours=1))],
                                      "timezone": "UTC"}
    agenda = ChrisAgenda(tmp_path, lambda: cfg, lambda: client)
    task = {"id": str(uuid4()), "title": "Unfinished task"}
    agenda.edit("add", task)
    agenda.edit("add", task)
    agenda.refresh(NOW)
    assert len(agenda.snapshot(NOW)["events"]) == 1
    assert titles(agenda.snapshot(NOW)["tasks"]) == ["Unfinished task"]
    client.get_events.assert_called_once_with(
        "primary", NOW, midnight_after_three_days(NOW.astimezone(BOARD_ZONE).date(), BOARD_ZONE), include_event_type=True)
    client._token.return_value = {"refresh_token": "different-grant"}
    assert agenda.snapshot(NOW)["events"] == []
    client.get_events.side_effect = CalendarError("Unavailable")
    agenda.refresh(NOW)
    assert titles(agenda.snapshot(NOW)["tasks"]) == ["Unfinished task"]
    reopened = ChrisAgenda(tmp_path, lambda: cfg, lambda: client)
    assert len(reopened.tasks()) == 1
    assert reopened.edit("complete", task)["tasks"] == []
    assert reopened.edit("add", task)["tasks"] == []
    assert "Unfinished task" not in reopened.path.read_text()
    assert all(b"Private appointment" not in path.read_bytes() for path in tmp_path.iterdir() if path.is_file())


def test_maintenance_status_never_discloses_agenda(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    state = runtime.display_status(NOW) | {"chris_agenda": {"tasks": ["TEST SECRET"]}}
    with patch.object(runtime, "display_status", return_value=state):
        assert "TEST SECRET" not in json.dumps(runtime.status(NOW))


def test_earlier_calendar_outage_does_not_drop_google_tasks_and_paused_google_tasks_expire(tmp_path):
    client = Mock()
    client._token.side_effect = CalendarError("Calendar unavailable")
    agenda = ChrisAgenda(tmp_path, lambda: CFG, lambda: client)
    agenda.google_tasks = Mock()
    agenda.google_tasks.visible_tasks.return_value = [
        {"title": "Google unfinished title", "display_until": (NOW + timedelta(days=1)).isoformat()}]
    agenda.edit("add", {"id": str(uuid4()), "title": "Local unfinished title"})
    agenda.refresh(NOW)
    agenda.google_tasks.refresh.assert_called_once_with(NOW)
    snapshot = agenda.snapshot(NOW + timedelta(minutes=29))
    assert titles(visible_rows(snapshot, NOW + timedelta(minutes=29))[1]) == ["Local unfinished title", "Google unfinished title"]
    assert titles(visible_rows(snapshot, NOW + timedelta(minutes=30))[1]) == ["Local unfinished title"]


def test_local_checklist_first_render_only_and_three_local_dates_across_dst(tmp_path):
    day1 = datetime(2026, 10, 31, 23, 57, tzinfo=BOARD_ZONE)
    fourth_midnight = datetime(2026, 11, 3, tzinfo=BOARD_ZONE)
    agenda = ChrisAgenda(tmp_path, lambda: CFG, Mock())
    old_id, unseen = uuid4().hex, uuid4().hex
    legacy = json.dumps({"open": [{"id": old_id, "title": "Existing task"}, {"id": unseen, "title": "Later page"}],
                         "completed": []})
    agenda.path.write_text(legacy, encoding="utf-8")
    assert len(agenda.snapshot(day1 - timedelta(weeks=2))["tasks"]) == 2
    assert not agenda.display_dates_path.exists()
    first = agenda.snapshot(day1)["tasks"]
    assert first[0]["display_id"] == old_id
    agenda.mark_displayed([old_id], day1)
    assert json.loads(agenda.display_dates_path.read_text()) == {old_id: "2026-10-31"}
    agenda = ChrisAgenda(tmp_path, lambda: CFG, Mock())
    agenda.mark_displayed([old_id], day1 + timedelta(days=1))
    last = agenda.snapshot(fourth_midnight - timedelta(seconds=1))
    assert last["tasks"][0]["display_until"] == "2026-11-03T06:00:00Z"
    assert titles(visible_rows(last, fourth_midnight)[1]) == ["Later page"]
    assert titles(agenda.snapshot(fourth_midnight)["tasks"]) == ["Later page"]
    assert titles(agenda.tasks()) == ["Existing task", "Later page"]
    assert agenda.path.read_text(encoding="utf-8") == legacy


def test_only_rendered_page_supplies_first_display_ids():
    state = fixture()
    until = (NOW + timedelta(days=2)).isoformat()
    tasks = [{"title": "Task " + str(i), "display_until": until, "display_id": uuid4().hex} for i in range(6)]
    state["chris_agenda"]["tasks"] = tasks
    entry, = [e for e in build_rotation(state, CFG, NOW) if e["key"] == "chris_agenda:1"]
    rendered = display.entry_render_state(state, CFG, entry, NOW) | {"defer_display_overlays": True}
    assert render_frame(rendered, CFG).info["agenda_shown_ids"] == [t["display_id"] for t in tasks[4:]]
    assert "agenda_shown_ids" not in render_frame(rendered, CFG | {"guest_mode": True}).info


@pytest.mark.parametrize("case", ["shown", "background", "sleeping", "guest", "different_card"])
def test_gui_only_acknowledges_tasks_after_actual_foreground_frame(monkeypatch, case):
    import pygame
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    state = fixture()
    task = state["chris_agenda"]["tasks"][0]
    task["display_id"] = uuid4().hex
    state["config"] = dict(CFG, guest_mode=(case == "guest"))
    operations = []

    class Screen:
        gl = None
        def __init__(self, *args):
            pass
        def draw(self, *args):
            operations.append("draw")

    def tick(cursor, entries, *args):
        selected = "home" if case == "different_card" else "chris_agenda"
        return next((entry for entry in entries if entry["view"] == selected), entries[0])

    connection = Mock()
    connection.request.side_effect = lambda name, data: operations.append(name)
    with patch.object(display, "datetime") as clock, patch.object(display, "DisplayScreen", Screen), \
            patch.object(display, "is_sleeping", return_value=(case == "sleeping")), \
            patch.object(display.RotationCursor, "tick", tick), patch.object(pygame.event, "get", return_value=[]), \
            patch.object(pygame.display, "get_active", return_value=True), \
            patch.object(pygame.key, "get_focused", return_value=(case != "background")):
        clock.now.return_value = NOW
        display.run_gui(connection, windowed=True, max_frames=1, initial_status=state)
    assert operations == (["draw", "agenda-displayed"] if case == "shown" else ["draw"])


@pytest.mark.parametrize("state", ["allowed", "guest", "kids_home", "stale", "late"])
def test_display_ack_revalidates_room_and_never_dates_a_task_from_a_status_read(tmp_path, state):
    runtime = Runtime(tmp_path, tmp_path / "install")
    cfg = CFG | {"guest_mode": state == "guest"}
    display_state = fixture()
    kids = display_state["kids"] | ({"state": "home"} if state == "kids_home" else {})
    if state == "stale":
        display_state["presence"]["chris"]["stale"] = True
    shown = NOW - timedelta(minutes=5) if state == "late" else NOW
    with patch.object(runtime, "snapshot", return_value={"demo": False}), patch.object(runtime.config, "get", return_value=cfg), \
            patch.object(runtime.presence, "snapshot", return_value=display_state["presence"]), \
            patch.object(runtime.household, "status", return_value={"kids": kids}), \
            patch("flightwall.backend.datetime") as clock, patch.object(runtime.chris_agenda, "mark_displayed") as mark:
        clock.now.return_value = NOW
        runtime.note_agenda_display(["a" * 32], shown.isoformat())
        assert mark.called is (state == "allowed")


def test_sleep_blanks_final_frame_after_timer_shopping_and_guest_banner(monkeypatch):
    import pygame
    from flightwall.render import demo_snapshot
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    config = DEFAULTS | {"guest_mode": True}
    status = {"config": config, "snapshot": demo_snapshot(NOW), "shopping_list": {"items": [{"name": "Milk"}]},
              "timers": [{"id": "timer", "label": "Tea", "ends_at": (NOW + timedelta(hours=1)).isoformat()}]}
    class Screen:
        gl = None
        def __init__(self, *args):
            pass
        def draw(self, frame, config):
            assert frame.getbbox() is None
            assert not frame.info
    with patch.object(display, "DisplayScreen", Screen), patch.object(display, "is_sleeping", return_value=True), \
            patch.object(pygame.event, "get", return_value=[]):
        assert display.run_gui(Mock(), windowed=True, max_frames=1, initial_status=status)["frames"] == 1
