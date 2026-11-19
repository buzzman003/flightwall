"""Personal flight maps retain source identity and paused-frame privacy gates."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from flightwall import render
from flightwall.card_preferences import card_enabled, duration_override
from flightwall.config import DEFAULTS, validate
from flightwall.display import entry_render_state
from flightwall.flight_alerts import SOURCE, normalize_operating_status
from flightwall.household_policy import build_rotation
from flightwall.personal_tracking import personal_tracking_targets


NOW = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
CONFIG = DEFAULTS | {"demo": False, "flightaware_enabled": True,
                     "lisa_flight_tracking_enabled": True, "timezone": "America/Chicago"}


def tracked_row():
    start, end = NOW - timedelta(minutes=30), NOW + timedelta(minutes=90)
    record = {"number": "DL1234", "origin": "ATL", "destination": "MEM",
              "start": start.isoformat(), "end": end.isoformat(), "traveler": "lisa",
              "status": "listed", "source": "delta_email", "updated_at": (NOW - timedelta(days=1)).isoformat()}
    flight, = personal_tracking_targets([record])
    provider = normalize_operating_status({"fa_flight_id": "fixture-123", "scheduled_out": start.isoformat(),
        "scheduled_in": end.isoformat(), "actual_out": (start + timedelta(minutes=3)).isoformat(),
        "actual_off": (start + timedelta(minutes=10)).isoformat()}, flight, NOW)
    return {"id": flight["id"], "traveler": "lisa", "reservation_status": "listed", "flight": flight,
            "status": "tracking", "provider": provider, "live": {
                "flight_id": flight["id"], "provider_flight_id": "fixture-123", "source": SOURCE,
                "latitude": 34.2, "longitude": -87.2, "observed_at": NOW.isoformat(),
                "altitude": 31000, "altitude_units": "feet", "groundspeed": 430, "heading": 278}}


def state_for(row):
    return {"now": NOW.isoformat(), "view": "personal_tracking", "demo": False,
            "personal_tracking": [row], "selected_card": row}


def labels(state, config=CONFIG):
    with patch("flightwall.render._text", wraps=render._text) as text:
        frame = render.render_frame(state, config)
    return [call.args[2] for call in text.call_args_list], frame


def test_exact_lisa_flight_is_a_separate_card_not_duty_or_observed_phone_presence():
    row = tracked_row()
    status = {"snapshot": {"demo": False, "flights": []}, "personal_tracking": [row],
              "personal_travel": [], "on_duty": False}
    original = deepcopy(status)
    slots = build_rotation(status, CONFIG, NOW)
    slot, = [entry for entry in slots if entry["view"] == "personal_tracking"]
    assert not {"details", "map", "operating_map", "flight_status"} & {entry["view"] for entry in slots}
    assert slot["key"] == "personal_tracking:" + row["id"]
    state = entry_render_state(status, CONFIG, slot, NOW)
    values, frame = labels(state)
    assert "LISA / DL1234 / LISTED" in values
    assert "ATL > MEM" in values
    assert "PLANNED ROUTE / SCHED 11:30 AM - 1:30 PM CST" in values
    assert "OBS 12:00 PM / 31000 FT / GS 430 KT / HDG 278" in values
    assert "FLIGHTAWARE / AIRBORNE / BOARDING UNKNOWN" in values
    assert frame.size == (512, 256)
    assert status == original


@pytest.mark.parametrize("changes", [{"guest_mode": True}, {"demo": True},
    {"lisa_flight_tracking_enabled": False}, {"flightaware_enabled": False},
    {"personal_travel_cards_visible": False}])
def test_settings_revoke_a_retained_paused_card_on_every_frame(changes):
    state = state_for(tracked_row()) | {"rotation_paused": True}
    values, _ = labels(state, CONFIG | changes)
    assert not any("DL1234" in value or "LISA /" in value or "31000" in value for value in values)


def test_backend_loss_removed_record_or_wrong_selected_identity_hides_private_slot():
    base = state_for(tracked_row())
    for changes in ({"backend_available": False}, {"personal_tracking": []},
                    {"selected_card": {"id": "b" * 24}}, {"demo": True},
                    {"now": (NOW + timedelta(hours=6)).isoformat()}):
        values, _ = labels(base | changes)
        assert not any("DL1234" in value or "LISA /" in value for value in values)


def test_stale_or_mismatched_position_cannot_become_a_current_map_marker():
    row = tracked_row()
    for changes in ({"observed_at": (NOW - timedelta(seconds=301)).isoformat()},
                    {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
                    {"provider_flight_id": "other-flight"}, {"flight_id": "b" * 24},
                    {"source": "unverified"}):
        modified = deepcopy(row)
        modified["live"].update(changes)
        values, _ = labels(state_for(modified))
        assert "POSITION NOT AVAILABLE" in values
        assert not any(value.startswith("OBS ") or value.startswith("LIVE POSITION") for value in values)
    later = state_for(row) | {"now": (NOW + timedelta(seconds=301)).isoformat(), "rotation_paused": True}
    assert "POSITION NOT AVAILABLE" in labels(later)[0]


def test_received_point_outside_the_planned_view_is_not_claimed_as_a_visible_marker():
    row = tracked_row()
    row["live"].update(latitude=10, longitude=-120)
    values, _ = labels(state_for(row))
    assert "OBSERVED POSITION OUTSIDE PLANNED MAP" in values
    assert not any(value.startswith("OBS ") for value in values)


def test_unsupported_units_and_landed_phase_cannot_show_misleading_telemetry():
    row = tracked_row()
    row["live"]["altitude_units"] = "metres"
    values, _ = labels(state_for(row))
    assert any(value.startswith("OBS ") for value in values)
    assert not any("31000 FT" in value for value in values)
    row["provider"]["phase"] = "landed"
    values, _ = labels(state_for(row))
    assert "POSITION NOT AVAILABLE" in values
    assert not any(value.startswith("OBS ") for value in values)


def test_personal_tracking_shares_visibility_and_duration_with_personal_travel():
    assert not card_enabled("personal_tracking", CONFIG | {"personal_travel_cards_visible": False})
    config = CONFIG | {"card_seconds": validate("card_seconds", '{"personal_travel":24}')}
    assert duration_override("personal_tracking", config) == duration_override("personal_travel", config) == 24


def test_budget_stop_is_labeled_without_synthesizing_a_position_or_flight_delay():
    row = tracked_row()
    row.update(status="unavailable", reason="monthly_limit", provider={})
    row.pop("live")
    values, _ = labels(state_for(row))
    assert "FLIGHTAWARE / APP BUDGET REACHED / BOARDING UNKNOWN" in values
    assert "POSITION NOT AVAILABLE" in values
    assert not any("DELAYED" in value or value.startswith("OBS ") for value in values)


@pytest.mark.parametrize(("phase", "field", "label"), [("landed", "actual_on", "LANDED"),
                                                      ("at_gate", "actual_in", "AT GATE")])
def test_past_verified_arrival_clock_survives_position_expiry(phase, field, label):
    row = tracked_row()
    row["provider"]["phase"] = phase
    row["provider"]["times"][field] = NOW.isoformat()
    state = state_for(row) | {"now": (NOW + timedelta(minutes=15)).isoformat(), "rotation_paused": True}
    values, _ = labels(state)
    assert label + " 12:00 PM CST" in values
    assert "FLIGHTAWARE / ARRIVAL OBSERVED / BOARDING UNKNOWN" in values
    assert not any("LIVE POSITION" in text or "31000 FT" in text for text in values)
    assert not any("ARRIVAL OBSERVED" in text for text in labels(state, CONFIG | {"guest_mode": True})[0])
    assert not any("ARRIVAL OBSERVED" in text for text in labels(dict(state, now=(NOW + timedelta(hours=4)).isoformat()))[0])

    for changes in ({"diverted": True}, {"cancelled": True}, {"flight_id": "other"},
                    {"times": {"estimated_in": NOW.isoformat()}},
                    {"times": {field: (NOW + timedelta(minutes=1)).isoformat()}}):
        modified = deepcopy(row)
        modified["provider"].update(changes)
        values, _ = labels(dict(state, personal_tracking=[modified]))
        assert not any("ARRIVAL OBSERVED" in text or text.startswith(label + " ") for text in values)

