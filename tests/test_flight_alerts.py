"""Synthetic AeroAPI status; a schedule alone is never a realtime verdict."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from flightwall.flight_alerts import (alert_card, choose_tracking_flight, fresh_status, match_operating_flight,
                                     normalize_operating_status, operating_ident, within_tracking_window)

NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)


def selected(**changes):
    return {"id": "synthetic-own", "number": "DL123", "origin": "ATL", "destination": "MEM",
            "start": "2026-10-31T17:00:00Z", "end": "2026-10-31T19:00:00Z", "deadhead": False, **changes}


def provider(**changes):
    return {"fa_flight_id": "DAL123-synthetic-1", "ident": "DAL123", "ident_iata": "DL123",
            "origin": {"code_iata": "ATL"}, "destination": {"code_iata": "MEM"},
            "scheduled_out": "2026-10-31T17:00:00Z", "scheduled_in": "2026-10-31T19:00:00Z",
            "estimated_out": "2026-10-31T17:10:00Z", "actual_out": "2026-10-31T17:05:00Z",
            "estimated_in": "2026-10-31T18:48:00Z", "cancelled": False, "diverted": False, **changes}


def status(**changes):
    return normalize_operating_status(provider(**changes), selected(), NOW)


def test_match_requires_unique_ident_route_and_departure_date_window_not_closest():
    config = {"airline": "DAL"}
    assert match_operating_flight({"flights": [provider()]}, selected(), config)[0] == "matched"
    for item in (provider(ident="AAL123", ident_iata="AA123"), provider(destination={"code_iata": "LAX"}),
                 provider(scheduled_out="2026-10-30T17:00:00Z"), provider(fa_flight_id="../../private")):
        assert match_operating_flight({"flights": [item]}, selected(), config) == ("not_found", None)
    for data in ({"flights": [provider(), provider(fa_flight_id="second", scheduled_out="2026-10-31T19:00:00Z")]},
                 {"flights": [provider()], "links": {"next": "https://example.invalid/page"}},
                 {"flights": [provider()], "num_pages": 2}):
        assert match_operating_flight(data, selected(), config) == ("ambiguous", None)


def test_provider_clocks_are_independent_of_calendar_and_raw_metadata_discarded():
    item = provider(scheduled_out="2026-10-31T17:02:00Z", crew="PRIVATE-CREW", status="PRIVATE-PAYLOAD", api_key="PRIVATE-KEY")
    original = deepcopy(item)
    result = normalize_operating_status(item, selected(), NOW)
    assert item == original
    assert result["departure"]["delta_minutes"] == 3
    assert result["departure"]["basis"] == "actual"
    assert result["departure"]["comparison"] == "late"
    assert result["arrival"]["delta_minutes"] == -12
    assert result["arrival"]["basis"] == "estimated"
    assert result["arrival"]["comparison"] == "early"
    assert "PRIVATE" not in json.dumps(result)
    assert result["flight_id"] != result["provider_flight_id"]


def test_gate_and_runway_times_are_compared_only_to_matching_event_baseline():
    result = status(scheduled_out=None, actual_out="2026-10-31T17:05:00Z", estimated_out=None,
                    scheduled_off="2026-10-31T17:15:00Z", actual_off="2026-10-31T17:20:00Z")
    assert result["departure"]["event"] == "off" and result["departure"]["delta_minutes"] == 5
    result = status(scheduled_out=None, scheduled_off=None, actual_out="2026-10-31T17:05:00Z")
    assert result["departure"]["comparison"] == "unknown" and result["departure"]["delta_minutes"] is None


def test_missing_estimate_and_actual_never_proves_ontime():
    result = status(actual_out=None, estimated_out=None, estimated_in=None)
    assert result["departure"]["comparison"] == result["arrival"]["comparison"] == "unknown"
    assert result["departure"]["time"] is None and alert_card(result, NOW) is None
    explicit = status(actual_out=None, estimated_out="2026-10-31T17:00:00Z", estimated_in="2026-10-31T19:00:00Z")
    assert explicit["departure"]["comparison"] == "on_time"
    assert explicit["arrival"]["comparison"] == "on_time" and alert_card(explicit, NOW) is None


def test_invalid_future_and_inconsistent_actual_clocks_never_claim_arrival():
    for value in ("2026-10-31T17:05:00", "not a date", True, "2026-10-31T19:00:00Z"):
        result = status(actual_out=value)
        assert "actual_out" not in result["times"]
    result = status(actual_off="2026-10-31T17:30:00Z", actual_on="2026-10-31T17:20:00Z", actual_in="2026-10-31T17:40:00Z")
    assert not any(key.startswith("actual_") for key in result["times"])
    assert result["phase"] == "scheduled"


def test_tracking_termination_and_diversion_are_explicit_provider_flags():
    terminated = status(cancelled=True)
    assert terminated["phase"] == "tracking_ended"
    assert alert_card(terminated, NOW)["kind"] == "tracking_ended"
    assert alert_card(status(diverted=True), NOW)["kind"] == "diverted"
    result = status(cancelled="true", diverted=1, actual_out=None, estimated_out=None, estimated_in=None)
    assert result["cancelled"] is None and result["diverted"] is None
    assert alert_card(result, NOW) is None
    assert alert_card(status(blocked=True), NOW) is None


def test_status_and_alert_expire_perframe_without_mutating_cache():
    result = status()
    original = deepcopy(result)
    assert fresh_status(result, NOW + timedelta(seconds=300))["status"] == "fresh"
    assert alert_card(result, NOW + timedelta(seconds=300)) is not None
    for now, available in ((NOW + timedelta(seconds=300, microseconds=1), True),
                           (NOW - timedelta(microseconds=1), True), (NOW, False)):
        aged = fresh_status(result, now, backend_available=available)
        assert aged["status"] == "stale" and alert_card(aged, now) is None
    assert result == original


def test_window_is_bounded_and_demo_deadheads_do_not_become_operating_flights():
    flight = selected()
    assert within_tracking_window(flight, datetime(2026, 10, 31, 13, tzinfo=timezone.utc))
    assert not within_tracking_window(flight, datetime(2026, 10, 31, 12, 59, 59, tzinfo=timezone.utc))
    assert within_tracking_window(flight, datetime(2026, 11, 1, 1, tzinfo=timezone.utc))
    assert not within_tracking_window(flight, datetime(2026, 11, 1, 1, 0, 1, tzinfo=timezone.utc))
    assert not within_tracking_window(selected(deadhead=True), NOW)
    assert choose_tracking_flight({"flights": [flight], "demo": True}, {}, NOW) is None


def test_already_departed_delayed_leg_retains_priority_after_roster_arrival():
    current = status(actual_off="2026-10-31T17:30:00Z")
    future = selected(id="next", number="DL124", start="2026-10-31T21:00:00Z", end="2026-10-31T23:00:00Z")
    snapshot = {"flights": [selected(), future], "demo": False}
    now = NOW + timedelta(hours=2)
    # A stale last-confirmed departure remains a fact, bounded by the six-hour
    # after-arrival window; its timing verdict is still stale on the TV.
    assert choose_tracking_flight(snapshot, {}, now, current)["id"] == "synthetic-own"
    assert choose_tracking_flight(snapshot, {}, now, current | {"phase": "at_gate"})["id"] == "next"
    assert choose_tracking_flight(snapshot, {}, now, None)["id"] == "next"


def test_flight_number_prefix_never_silently_uses_another_airline():
    assert operating_ident(selected(number="1946"), "DAL") == "DAL1946"
    assert operating_ident(selected(number="AA 1946"), "DAL") == "AA1946"
