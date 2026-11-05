"""Synthetic FC View notes exercise the opt-in MiCrew calendar path."""
import copy
from datetime import datetime
import json

import pytest

from flightwall.calendar_client import CalendarClient, CalendarError
from flightwall.config import ConfigStore, PUBLIC_FIELDS, REMOTE_SETTINGS
from flightwall.flight_alerts import choose_tracking_flight
from flightwall.household_calendar import HouseholdCalendars, _merge_flights
from flightwall.main import _select_calendar
from flightwall.mycrew import SOURCE, clean_trip_days, parse_mycrew_events, parse_mycrew_trip_days


def duty():
    return {"summary": "X321", "id": "private-rotation-id", "start": {"date": "2026-02-06"},
            "end": {"date": "2026-02-07"}, "description": (
                "Report: 0800L\nFR06 DH 123 ATL-SJU 0900-1300\nFR06 123 SJU-ATL 1400-1700\n"
                "FR06 456 ATL-VPS 1800-1815\nHotel and crew private details: not for display")}


def test_dated_station_clocks_keep_return_direction_and_deadhead_separate():
    flights = parse_mycrew_events([duty()], "DL")
    assert [(row["number"], row["origin"], row["destination"], row["deadhead"]) for row in flights] == [
        ("DL123", "ATL", "SJU", True), ("DL123", "SJU", "ATL", False), ("DL456", "ATL", "VPS", False)]
    assert [row["start"] for row in flights] == ["2026-02-06T14:00:00+00:00", "2026-02-06T18:00:00+00:00",
                                                "2026-02-06T23:00:00+00:00"]
    assert flights[-1]["end"] == "2026-02-07T00:15:00+00:00"
    assert all(row["report"] == "2026-02-06T13:00:00+00:00" for row in flights)
    assert choose_tracking_flight({"flights": flights[:1]}, {"airline": "DL"},
                                  datetime.fromisoformat(flights[0]["start"])) is None
    assert "private" not in json.dumps(flights).casefold()
    assert "X321" not in json.dumps(flights)


@pytest.mark.parametrize("before,after", [
    ("FR06 456", "SA06 456"), ("FR06 456", "SA07 456"), ("Report: 0800L", "Report: 0800Z"),
    ("Report: 0800L", "Unlabelled 0800"), ("ATL-VPS 1800", "ATL-XXX 1800"),
    ("ATL-VPS 1800", "ATL-VPS 2600"), ("ATL-VPS 1800", "CID-VPS 1800"),
    ("ATL-VPS 1800", "ATL-VPS 0900"),
])
def test_inconsistent_rotation_lines_fail_closed(before, after):
    event = duty()
    event["description"] = event["description"].replace(before, after)
    assert parse_mycrew_events([event], "DL") == []


def test_requires_single_day_rotation_and_selected_carrier_and_respects_cancellation():
    event = duty()
    assert parse_mycrew_events([event], "") == []
    assert parse_mycrew_events([event | {"end": {"date": "2026-02-08"}}], "DL") == []
    assert parse_mycrew_events([event | {"summary": "Private trip"}], "DL") == []
    assert parse_mycrew_events([event, event | {"status": "cancelled"}], "DL") == []


def test_trip_dates_preserve_source_spans_without_joining_separate_duties_or_retaining_notes():
    first = duty()
    later = duty() | {"id": "another-private-id", "start": {"date": "2026-02-10"}, "end": {"date": "2026-02-11"}}
    assert parse_mycrew_trip_days([first, later]) == [
        {"start_date": "2026-02-06", "end_date": "2026-02-07"},
        {"start_date": "2026-02-10", "end_date": "2026-02-11"},
    ]
    full_rotation = first | {"end": {"date": "2026-02-11"}}
    projected = parse_mycrew_trip_days([full_rotation, first | {"status": "cancelled"}, later])
    assert projected == [{"start_date": "2026-02-10", "end_date": "2026-02-11"}]
    projected = parse_mycrew_trip_days([full_rotation])
    assert projected == [{"start_date": "2026-02-06", "end_date": "2026-02-11"}]
    assert not any(value in json.dumps(projected) for value in ("private", "X321", "ATL", "Hotel"))
    for damaged in (
        {"summary": "Private vacation"}, {"end": {"date": "2026-03-15"}},
        {"start": {"date": "2026-02-30"}}, {"end": {"date": "2026-02-06"}},
        {"start": {"dateTime": "2026-02-06T12:00:00Z"}}, {"end": {"date": "20260211"}},
    ):
        assert parse_mycrew_trip_days([full_rotation | damaged]) == []
    assert clean_trip_days([projected[0] | {"private": "Unapproved"}, *projected, *projected]) == projected


def test_refuses_ambiguous_dst_report_and_uses_next_day_arrival_when_unique():
    event = {"summary": "X654", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"},
             "description": "Report: 0115L\nSU01 123 ATL-VPS 0300-0315"}
    assert parse_mycrew_events([event], "DL") == []
    event["description"] = "Report: 2200L\nSU01 123 ATL-VPS 2300-0015"
    flight, = parse_mycrew_events([event], "DL")
    assert flight["start"] == "2026-11-02T04:00:00+00:00"
    assert flight["end"] == "2026-11-02T06:15:00+00:00"


def test_conflicting_supplement_never_adds_duplicate_operating_leg():
    parsed = parse_mycrew_events([duty()], "DL")
    exact = parsed[0] | {"source": "FC View", "deadhead": False}
    alternate = parsed[2] | {"source": "FC View", "start": "2026-02-06T22:50:00+00:00"}
    merged = _merge_flights([{"source": "FC View", "flights": [exact, alternate]},
                             {"source": SOURCE, "flights": parsed}])
    assert len(merged) == 3
    assert merged[0]["source"] == "FC View" and merged[0]["deadhead"] is True
    assert merged[-1]["start"] == alternate["start"]
    assert exact["deadhead"] is False
    duplicate = duty() | {"id": "different-private-event-id"}
    duplicate["description"] = duplicate["description"].replace("1800-1815", "1810-1825")
    assert len(parse_mycrew_events([duty(), duplicate], "DL")) == 2


def test_verified_fc_view_selection_reuses_fetch_and_disabling_hides_its_notes(tmp_path, monkeypatch):
    calls = []
    calendar_id = "private-selected-calendar-id"

    def events(client, selected, *args, include_descriptions=False):
        calls.append((client.data_dir.name, selected, include_descriptions))
        raw = copy.deepcopy(duty())
        if not include_descriptions:
            raw.pop("description")
        return {"items": [raw], "timezone": "America/Chicago"}

    monkeypatch.setattr(CalendarClient, "get_events", events)
    monkeypatch.setattr(HouseholdCalendars, "list_calendars", lambda self, profile: [
        {"name": "FC View", "id": calendar_id, "timezone": "America/Chicago"}])
    store = ConfigStore(tmp_path)
    store.update({"calendar_id": calendar_id, "demo": False, "airline": "DL"})
    manager = HouseholdCalendars(tmp_path, store.get)
    assert manager.sync()["flight_snapshot"]["flights"] == []
    assert calls == [("chris", calendar_id, False)]
    with pytest.raises(CalendarError, match="airport-local"):
        _select_calendar(store, "FC View", person="chris", role="mycrew")
    assert len(calls) == 1
    selected = _select_calendar(store, "FC View", person="chris", role="mycrew", station_local=True)
    assert selected["flights"] == 3
    assert calls == [("chris", calendar_id, False), ("chris", calendar_id, True)]
    assert not {"mycrew_calendar_id", "mycrew_clock"} & (PUBLIC_FIELDS | REMOTE_SETTINGS)
    cached = manager.path.read_text()
    assert calendar_id not in cached and "private-rotation-id" not in cached
    assert "private details" not in cached and "X321" not in cached
    assert manager.status()["flight_snapshot"]["trip_days"] == [
        {"start_date": "2026-02-06", "end_date": "2026-02-07"}]
    assert HouseholdCalendars(tmp_path, store.get).status()["flight_snapshot"]["trip_days"] == [
        {"start_date": "2026-02-06", "end_date": "2026-02-07"}]
    store.update({"airline": "AA"})
    assert manager.status()["flight_snapshot"]["flights"] == []
    store.update({"mycrew_clock": ""})
    assert manager.sync()["flight_snapshot"]["flights"] == []
    assert "mycrew" not in json.loads(manager.path.read_text())["flight_roles"]
    assert calls[-1] == ("chris", calendar_id, False)
