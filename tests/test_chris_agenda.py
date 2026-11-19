"""Private household decisions must remain true at the moment a TV frame is drawn."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from flightwall import display
from flightwall.backend import Runtime
from flightwall.calendar_client import CalendarError
from flightwall.chris_agenda import ChrisAgenda, calendar_rows, visible_rows
from flightwall.config import DEFAULTS
from flightwall.household_calendar import kids_snapshot
from flightwall.household_policy import build_rotation
from flightwall.render import render_frame


NOW = datetime(2026, 10, 22, 18, tzinfo=timezone.utc)
CFG = DEFAULTS | {"demo": False, "presence_enabled": True, "kids_filter_enabled": True,
                  "chris_agenda_enabled": True, "timezone": "America/Chicago"}


def fixture():
    people = {who: {"state": state, "last_seen": NOW.isoformat(), "stale": False}
              for who, state in (("chris", "home"), ("lisa", "away"))}
    return {"config": CFG, "backend_available": True, "snapshot": {"demo": False, "flights": []},
            "presence": people, "kids": {"state": "away", "stale": False, "last_sync": NOW.isoformat(),
                                         "until": (NOW + timedelta(hours=1)).isoformat()},
            "chris_agenda": {"synced": NOW.isoformat(), "events": [], "tasks": ["PRIVATE TEST TASK"]}}


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
    rows = calendar_rows(events, "America/Chicago", NOW)
    assert [r["title"] for r in rows] == ["Day", "Ongoing", "Next"]
    assert rows[0]["date"] == "2026-10-22"
    assert all(set(row) == {"title", "start", "end", "date"} for row in rows)


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
    assert agenda.snapshot(NOW)["tasks"] == ["Unfinished task"]
    client.get_events.assert_called_once_with("primary", NOW, NOW + timedelta(days=7))
    client._token.return_value = {"refresh_token": "different-grant"}
    assert agenda.snapshot(NOW)["events"] == []
    client.get_events.side_effect = CalendarError("Unavailable")
    agenda.refresh(NOW)
    assert agenda.snapshot(NOW)["tasks"] == ["Unfinished task"]
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
    agenda.google_tasks.titles.return_value = ["Google unfinished title"]
    agenda.edit("add", {"id": str(uuid4()), "title": "Local unfinished title"})
    agenda.refresh(NOW)
    agenda.google_tasks.refresh.assert_called_once_with(NOW)
    snapshot = agenda.snapshot(NOW + timedelta(minutes=29))
    assert visible_rows(snapshot, NOW + timedelta(minutes=29))[1] == ["Local unfinished title", "Google unfinished title"]
    assert visible_rows(snapshot, NOW + timedelta(minutes=30))[1] == ["Local unfinished title"]


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
