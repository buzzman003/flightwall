"""Synthetic owner planning never replaces a source report or Buzz note."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from flightwall import render
from flightwall.flight_brief_view import brief_rows
from flightwall.flight_context import build_flight_context
from flightwall.vps_leave_plan import format_owner_plan, owner_leave_plan


UTC = timezone.utc
ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2027, 1, 13, 9, tzinfo=UTC)
CONFIG = {"demo": False, "timezone": "America/Chicago", "vps_leave_plan_enabled": True}
FLIGHT = {"id": "fictional-outbound", "number": "DL123", "origin": "VPS", "destination": "ATL",
          "start": "2027-01-13T12:00:00Z", "end": "2027-01-13T13:10:00Z", "report": "2027-01-13T11:00:00Z"}


def test_owner_formula_is_exact_elapsed_time_and_keeps_report_separate_without_mutation():
    before = deepcopy(FLIGHT)
    plan = owner_leave_plan(FLIGHT, CONFIG, NOW)
    assert plan == {"source": "OWNER PLAN", "departure_at": "2027-01-13T12:00:00+00:00",
                    "window_start_at": "2027-01-13T10:22:00+00:00", "window_end_at": "2027-01-13T10:32:00+00:00",
                    "report_at": "2027-01-13T11:00:00+00:00"}
    assert format_owner_plan(plan, ZONE, short=True) == "4:22-4:32 AM CST"
    assert FLIGHT == before
    drift = owner_leave_plan(FLIGHT | {"report": "2027-01-13T10:50:00Z", "leave_at": "2027-01-13T10:10:00Z"}, CONFIG, NOW)
    assert drift["window_start_at"] == plan["window_start_at"]
    assert drift["window_end_at"] == plan["window_end_at"]
    assert drift["report_at"] == "2027-01-13T10:50:00+00:00"
    assert "report_at" not in owner_leave_plan(FLIGHT | {"report": None}, CONFIG, NOW)


@pytest.mark.parametrize("flight,config,now", [
    (FLIGHT, CONFIG | {"vps_leave_plan_enabled": False}, NOW),
    (FLIGHT, CONFIG | {"demo": True}, NOW),
    (FLIGHT | {"origin": "PNS"}, CONFIG, NOW),
    (FLIGHT | {"destination": "VPS"}, CONFIG, NOW),
    (FLIGHT | {"start": "2027-01-13T06:00:00"}, CONFIG, NOW),
    (FLIGHT | {"end": "2027-01-13T11:00:00Z"}, CONFIG, NOW),
    (FLIGHT, CONFIG, datetime(2027, 1, 13, 12, tzinfo=UTC)),
    (FLIGHT, CONFIG, datetime(2027, 1, 13, 9)),
    (FLIGHT, CONFIG | {"vps_leave_drive_minutes": True}, NOW),
    (FLIGHT, CONFIG | {"vps_leave_security_minutes": -1}, NOW),
    (FLIGHT, CONFIG | {"vps_leave_target_early_minutes": 40}, NOW),
])
def test_disabled_unrelated_past_or_invalid_plan_is_not_projected(flight, config, now):
    assert owner_leave_plan(flight, config, now) is None


def test_dst_and_previous_local_date_are_not_flattened_to_naive_clocks():
    summer = FLIGHT | {"start": "2027-07-13T11:00:00Z", "end": "2027-07-13T12:00:00Z"}
    assert format_owner_plan(owner_leave_plan(summer, CONFIG, NOW), ZONE, short=True) == "4:22-4:32 AM CDT"
    crossing = FLIGHT | {"start": "2027-11-07T08:35:00Z", "end": "2027-11-07T09:35:00Z"}
    plan = owner_leave_plan(crossing, CONFIG, NOW)
    assert plan["window_start_at"] == "2027-11-07T06:57:00+00:00"
    assert plan["window_end_at"] == "2027-11-07T07:07:00+00:00"
    label = format_owner_plan(plan, ZONE)
    assert "1:57 AM CDT" in label and "1:07 AM CST" in label
    midnight = FLIGHT | {"start": "2027-01-13T07:00:00Z", "end": "2027-01-13T08:00:00Z"}
    plan = owner_leave_plan(midnight, CONFIG, datetime(2027, 1, 12, tzinfo=UTC))
    assert "01/12" in format_owner_plan(plan, ZONE, short=True)


def test_owner_plan_needs_no_inbound_or_home_observation_and_brief_labels_both_sources():
    context = build_flight_context({"snapshot": {"demo": False, "flights": [FLIGHT]}, "operating_flight": FLIGHT}, CONFIG, NOW)
    assert context["vps_visit"] is None
    assert context["owner_leave_plan"]["window_start_at"] == "2027-01-13T10:22:00+00:00"
    rows = brief_rows(context, CONFIG)
    assert any(row[1] == "VPS / LEAVE HOME" and row[2].startswith("OWNER PLAN / ") for row in rows)
    assert any(row[1] == "VPS / ROSTER REPORT" and "5:00 AM CST" in row[2] for row in rows)


def test_upcoming_schedule_displays_owner_plan_and_independent_unmatched_note_warning():
    state = {"demo": False, "view": "trip_summary", "now": NOW.isoformat(), "flights": [FLIGHT],
             "brief_leave_warnings": [{"flight_id": FLIGHT["id"], "reason": "visit_not_matched"}],
             "trip_preview": {"flights": [FLIGHT], "start_date": "2027-01-13", "end_date": "2027-01-13"}}
    labels = []
    original = render._text
    def capture(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=capture):
        image = render.render_frame(state, CONFIG | {"brief_leave_times_enabled": True})
    assert image.size == (512, 256)
    assert "OWNER PLAN" in labels and "LEAVE HOME" in labels and "4:22-4:32 AM CST" in labels
    assert "VPS LEAVE NOTE / VISIT NOT MATCHED / CHECK BUZZ BRIEF" in labels
    assert "DL123" in labels


def test_local_visit_keeps_buzz_clock_and_conflict_separate_from_owner_plan():
    card = {"id": "fictional-visit", "airport": "VPS", "arrival": "2027-01-12T22:00:00Z",
            "departure": FLIGHT["start"], "display_start": "2027-01-12T06:00:00Z",
            "display_end": FLIGHT["start"], "duration_minutes": 840,
            "leave_at": "2027-01-13T10:10:00Z", "leave_conflict": True}
    state = {"demo": False, "view": "local_visit", "now": NOW.isoformat(),
             "flights": [FLIGHT], "selected_card": card}
    labels = []
    original = render._text
    def capture(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=capture):
        render.render_frame(state, CONFIG | {"brief_leave_times_enabled": True})
    assert any("OWNER PLAN" in label and "4:22-4:32 AM CST" in label for label in labels)
    assert any(label.startswith("BUZZ BRIEF / ") and "4:10 AM CST" in label for label in labels)
    assert "VERIFY" in labels
