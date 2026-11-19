"""Select only validated private visits and keep reminders inside their local day."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

from flightwall.calendar_client import CalendarClient, CalendarError
from flightwall.chris_agenda import ChrisAgenda, calendar_enabled
from flightwall.config import DEFAULTS
from flightwall.visit_activity import PROFILE_FILE, load_profile, matching_dates, permitted, visible_activity


ZONE = ZoneInfo("America/Chicago")
SETTINGS = {"title": "Visit activity", "exact_titles": ["Visit With Community Center"],
            "timed_prefix": "Visit arrive", "timed_suffix": "Community Center"}
CFG = DEFAULTS | {"demo": False, "presence_enabled": True, "chris_agenda_enabled": False,
                  "visit_activity_enabled": True, "timezone": ZONE.key}
NOW = datetime(2026, 10, 31, 23, 53, tzinfo=ZONE)


def appointment(summary="Visit With Community Center", **fields):
    return {"id": "fake-event", "summary": summary, "status": "confirmed", "eventType": "default",
            "start": {"dateTime": "2026-11-01T17:00:00-06:00"},
            "end": {"dateTime": "2026-11-01T18:00:00-06:00"}, **fields}


def configured_path(tmp_path):
    profile = tmp_path / PROFILE_FILE
    profile.write_text(json.dumps(SETTINGS), encoding="utf-8")
    return profile


@pytest.mark.parametrize("fields", [
    {"status": "cancelled"}, {"status": "tentative"}, {"status": None}, {"id": ""},
    {"eventType": "fromGmail"}, {"recurringEventId": "series"}, {"originalStartTime": {"date": "2026-11-01"}},
    {"recurrence": ["RRULE:FREQ=WEEKLY"]}, {"summary": "Visit With Community Center - Cancelled"},
    {"summary": "Canceled visit arrive 945 to 1050m Community Center"}, {"summary": "Automatic visitation"},
    {"summary": "🚫 INACTIVE — Visit With Community Center"}, {"status": "inactive"},
    {"summary": "Another child Visit With Community Center"},
    {"summary": "Visit arrive 915 to 2445m Community Center"},
    {"summary": "Visit arrive 915 to 1045m Community Center - placeholder"},
    {"start": {"dateTime": "2026-11-01T19:00:00-06:00"}},
])
def test_ignore_unconfirmed_automatic_cancelled_or_loosely_matching_visits(tmp_path, fields):
    profile = load_profile(configured_path(tmp_path))
    assert not matching_dates([appointment(**fields)], ZONE.key, ZONE, profile)


def test_exact_timed_alias_and_date_use_local_civil_time(tmp_path):
    profile = load_profile(configured_path(tmp_path))
    event_pair = [appointment(" VISIT with  community center "), appointment(
        "Visit arrive 915 to 0945m Community Center", id="another",
        start={"dateTime": "2026-11-02T00:00:00Z"}, end={"dateTime": "2026-11-02T01:00:00Z"})]
    days = matching_dates(event_pair, ZONE.key, ZONE, profile)
    assert {day.isoformat() for day in days} == {"2026-11-01"}
    cancelled = {"id": "fake-event", "status": "cancelled"}
    assert not matching_dates([cancelled, event_pair[0]], ZONE.key, ZONE, profile)
    inactive = dict(event_pair[0], summary="🚫 INACTIVE — Visit With Community Center")
    assert not matching_dates([event_pair[0], inactive], ZONE.key, ZONE, profile)
    all_day = appointment(start={"date": "2026-11-01"}, end={"date": "2026-11-02"})
    assert matching_dates([all_day], "Asia/Tokyo", ZONE, profile) == days


def test_connects_only_to_primary_calendar_with_minimum_fields_and_never_stores_a_visit_title(tmp_path):
    client = CalendarClient(tmp_path, CFG)
    client._token = Mock(return_value={"refresh_token": "fixture-only"})
    agenda = ChrisAgenda(tmp_path, lambda: CFG, lambda: client)
    assert calendar_enabled(CFG)
    assert not calendar_enabled(CFG | {"demo": True})
    agenda.google_tasks = Mock()
    configured_path(tmp_path)
    with patch.object(client, "_api", return_value={
        "items": [appointment(description="DO NOT RETAIN"), appointment("Other private appointment", id="other")],
        "timeZone": ZONE.key,
    }) as api:
        agenda.refresh(NOW)
    (route, fields), _ = api.call_args
    assert route == "/calendars/primary/events"
    assert "recurringEventId" in fields["fields"]
    assert all(field not in fields["fields"] for field in ("description", "attendees", "organizer", "location"))
    agenda.google_tasks.refresh.assert_not_called()
    card = agenda.visit_activity(NOW)
    assert card.keys() == {"title", "visit_date", "idea", "bring", "synced"}
    assert card["visit_date"] == "2026-11-01" and card["title"] == "Visit activity"
    assert "Community Center" not in json.dumps(card) and "DO NOT RETAIN" not in json.dumps(card)
    assert agenda.snapshot(NOW) == {}
    assert "Other private appointment" not in repr(agenda._calendar)
    assert sorted(file.name for file in tmp_path.iterdir()) == [PROFILE_FILE]
    first_midnight = datetime(2026, 11, 1, 0, 0, tzinfo=ZONE)
    assert agenda.visit_activity(first_midnight) is None
    assert visible_activity(card, first_midnight, ZONE.key) is None
    stale = datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
    assert visible_activity(card | {"synced": (stale - timedelta(minutes=30)).isoformat()}, stale, ZONE.key) is None
    assert visible_activity(card | {"synced": (stale + timedelta(seconds=1)).isoformat()}, stale, ZONE.key) is None


def test_visits_clear_for_cancelled_refresh_removed_profile_unknown_calendar_or_consent(tmp_path):
    settings = dict(CFG)
    client = Mock()
    client._token.return_value = {"refresh_token": "fixture-only"}
    profile = configured_path(tmp_path)
    client.get_events.return_value = {"items": [appointment()], "timezone": ZONE.key}
    agenda = ChrisAgenda(tmp_path, lambda: settings, lambda: client)
    agenda.refresh(NOW)
    assert agenda.visit_activity(NOW)
    settings["visit_activity_enabled"] = False
    assert not agenda.visit_activity(NOW)
    settings["visit_activity_enabled"] = True
    profile.unlink()
    assert not agenda.visit_activity(NOW)
    configured_path(tmp_path)
    assert agenda.visit_activity(NOW)
    client._token.return_value = {"refresh_token": "new-identity"}
    assert not agenda.visit_activity(NOW)
    agenda.refresh(NOW)
    assert agenda.visit_activity(NOW)
    client.get_events.return_value = {"items": [appointment(status="cancelled")], "timezone": ZONE.key}
    agenda.refresh(NOW)
    assert not agenda.visit_activity(NOW)
    client.get_events.return_value = {"items": [appointment()], "timezone": ZONE.key}
    agenda.refresh(NOW)
    client.get_events.side_effect = CalendarError("offline")
    agenda.refresh(NOW)
    assert not agenda.visit_activity(NOW)


def test_visibility_requires_home_and_never_guest_mode():
    assert permitted(CFG, {"chris_home": True})
    assert not permitted(CFG, {"chris_home": None})
    assert not permitted(CFG | {"guest_mode": True}, {"chris_home": True})
    assert not permitted(CFG | {"presence_enabled": False}, {"chris_home": True})
