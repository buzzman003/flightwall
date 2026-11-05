from datetime import datetime, timedelta, timezone

from flightwall.flight_alerts import normalize_operating_status
from flightwall.flight_context import build_flight_context


NOW = datetime(2026, 11, 4, 23, tzinfo=timezone.utc)
CONFIG = {"demo": False, "flightaware_enabled": True, "airport_alerts_enabled": True,
          "trip_weather_enabled": True, "local_visits_enabled": True, "brief_leave_times_enabled": True}
FLIGHT = {"id": "inbound", "number": "DL123", "origin": "ATL", "destination": "VPS",
          "start": "2026-11-04T21:40:00Z", "end": "2026-11-04T23:10:00Z"}
OUTBOUND = {"id": "outbound", "number": "DL456", "origin": "VPS", "destination": "LGA",
            "report": "2026-11-05T19:10:00Z", "start": "2026-11-05T20:15:00Z", "end": "2026-11-05T23:10:00Z",
            "leave_at": "2026-11-05T18:35:00Z", "leave_conflict": True,
            "description": "private roster notes"}


def state(*, current=None, **provider):
    return {"snapshot": {"demo": False, "flights": [FLIGHT, OUTBOUND]}, "operating_flight": FLIGHT,
            "operating_status": current if current is not None else normalize_operating_status({
                "fa_flight_id": "internal-provider-id", "actual_out": "2026-11-04T21:45:00Z",
                "scheduled_out": "2026-11-04T21:40:00Z", **provider}, FLIGHT, NOW),
            "airport_alerts": {"source": "FAA airport status", "status": "ready",
                               "issued_at": NOW.isoformat(), "fetched_at": NOW.isoformat(), "events": [
                {"airport": airport, "kind": kind, "reason": "omit source text"}
                for airport, kind in [("ATL", "departure_delay"), ("ATL", "ground_stop"), ("VPS", "ground_stop"),
                                      ("VPS", "arrival_delay"), ("LGA", "arrival_delay")]]}}


def test_scope_airport_advisories_to_leg_direction_and_provider_phase():
    taxi_out = build_flight_context(state(), CONFIG, NOW)
    assert taxi_out["phase"] == "taxi_out"
    assert taxi_out["provider"]["departure"]["delta_minutes"] == 5
    assert {(row["airport"], row["kind"]) for row in taxi_out["airport_advisories"]} == {
        ("ATL", "departure_delay"), ("VPS", "ground_stop"), ("VPS", "arrival_delay")}
    airborne = build_flight_context(state(actual_off="2026-11-04T21:58:00Z"), CONFIG, NOW)
    assert airborne["phase"] == "airborne"
    assert airborne["provider"]["taxi_out_minutes"] == 13
    assert [(row["airport"], row["kind"]) for row in airborne["airport_advisories"]] == [("VPS", "arrival_delay")]
    landing = state(actual_on="2026-11-04T22:55:00Z", estimated_in="2026-11-04T23:07:00Z")
    taxi_in = build_flight_context(landing, CONFIG, NOW)
    assert taxi_in["phase"] == "taxi_in" and taxi_in["airport_advisories"] == []
    assert "taxi_in_minutes" not in taxi_in["provider"] and "gate_in" not in taxi_in["provider"]["events"]
    at_gate = build_flight_context(state(actual_on="2026-11-04T22:55:00Z", actual_in="2026-11-04T22:59:00Z"), CONFIG, NOW)
    assert at_gate["phase"] == "at_gate" and at_gate["provider"]["taxi_in_minutes"] == 4
    diverted = build_flight_context(landing | {"operating_status": landing["operating_status"] | {"diverted": True}}, CONFIG, NOW)
    assert diverted["phase"] == "diverted" and not diverted["provider"]["events"] and not diverted["airport_advisories"]


def test_stale_or_mismatched_provider_cannot_project_phase_or_event():
    fixture = state()
    for row, now in ((fixture["operating_status"] | {"flight_id": "other"}, NOW),
                     (fixture["operating_status"], NOW + timedelta(minutes=16))):
        result = build_flight_context(fixture | {"operating_status": row}, CONFIG, now)
        assert result["phase"] == "schedule_only" and result["provider"] is None
    assert build_flight_context(fixture | {"backend_available": False}, CONFIG, NOW) is None
    assert build_flight_context(fixture | {"snapshot": {"demo": False, "flights": []}}, CONFIG, NOW) is None
    assert build_flight_context(fixture, CONFIG | {"demo": True}, NOW) is None


def test_vps_note_stays_distinct_and_requires_consent_and_unique_association():
    fixture = state()
    result = build_flight_context(fixture, CONFIG, NOW)
    assert result["vps_visit"] == {"scheduled_arrival_at": "2026-11-04T23:10:00+00:00",
                                   "scheduled_departure_at": "2026-11-05T20:15:00+00:00",
                                   "scheduled_report_at": "2026-11-05T19:10:00+00:00",
                                   "brief_leave_at": "2026-11-05T18:35:00+00:00", "brief_leave_conflict": True}
    assert "private roster notes" not in repr(result)
    assert "internal-provider-id" not in repr(result)
    assert "omit source text" not in repr(result)
    off = build_flight_context(fixture, CONFIG | {"brief_leave_times_enabled": False}, NOW)
    assert "brief_leave_at" not in off["vps_visit"]
    snapshot = {"demo": False, "flights": [FLIGHT, FLIGHT | {"id": "duplicate"}, OUTBOUND]}
    assert build_flight_context(fixture | {"snapshot": snapshot}, CONFIG, NOW)["vps_visit"] is None
    snapshot = {"demo": False, "flights": [FLIGHT, OUTBOUND, OUTBOUND | {"id": "duplicate"}]}
    assert build_flight_context(fixture | {"snapshot": snapshot}, CONFIG, NOW)["vps_visit"] is None


def test_metar_summary_includes_only_matching_recent_station_observations():
    metar = {"icao": "KVPS", "status": "ready", "stale": False, "source": "AviationWeather.gov",
             "flight_category": "MVFR", "observed_at": NOW.isoformat(), "checked_at": NOW.isoformat(),
             "summary": "omit source text"}
    airport = {"iata": "VPS", "icao": "KVPS", "metar": metar, "taf": {"flight_category": "IFR"}}
    fixture = state() | {"trip_weather": {"airports": [airport, airport | {"iata": "LGA"},
        {"iata": "ATL", "icao": "KATL", "metar": metar | {"icao": "KATL", "stale": True}}]}}
    assert build_flight_context(fixture, CONFIG, NOW)["weather"] == [
        {"airport": "VPS", "flight_category": "MVFR", "observed_at": NOW.isoformat()}]
    assert not build_flight_context(fixture, CONFIG | {"trip_weather_enabled": False}, NOW)["weather"]
