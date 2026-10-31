from datetime import datetime, timedelta, timezone

from flightwall.outlook import custody_outlook, local_visits, trip_preview
from flightwall.household_policy import build_rotation


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_custody_outlook_dst_transition_and_exact_boundaries():
    now = dt("2026-11-01T05:30:00Z")
    intervals = [{"parent": "Mom", "start": "2026-11-01T05:00:00Z", "end": "2026-11-01T18:00:00Z"},
                 {"parent": "Dad", "start": "2026-11-01T18:00:00Z", "end": "2026-11-02T06:00:00Z"}]
    result = custody_outlook(intervals, now.isoformat(), {"timezone": "America/Chicago"}, now)
    first = result["days"][0]
    assert len(result["days"]) == 10
    assert first["state"] == "mixed"
    assert [row["state"] for row in first["segments"]] == ["home", "away"]
    assert dt(first["segments"][-1]["end"]) - dt(first["segments"][0]["start"]) == timedelta(hours=25)
    assert result["days"][1]["state"] == "unknown"


def test_custody_outlook_conflicts_gaps_and_failed_sync_do_not_guess():
    now = dt("2026-10-31T15:00:00Z")
    rows = [{"parent": "Mom", "start": "2026-10-31T05:00:00Z", "end": "2026-11-01T05:00:00Z"},
            {"parent": "Dad", "start": "2026-10-31T20:00:00Z", "end": "2026-10-31T21:00:00Z"}]
    result = custody_outlook(rows, now.isoformat(), {"timezone": "America/Chicago"}, now)
    assert result["days"][0]["state"] == "unknown"
    assert result["stale"] is False
    for failed, last_sync in [(True, now.isoformat()), (False, (now - timedelta(hours=1)).isoformat())]:
        result = custody_outlook(rows, last_sync, {}, now, failed=failed)
        assert result["stale"] and all(row["state"] == "unknown" for row in result["days"])


def leg(id, origin, destination, start, end, **extra):
    return dict(id=id, number="DL123", origin=origin, destination=destination, start=start, end=end, **extra)


def test_local_vps_stay_matches_immediate_leg_across_dst():
    legs = [leg("in", "ATL", "VPS", "2026-10-31T23:00:00Z", "2026-11-01T01:00:00Z"),
            leg("out", "VPS", "ATL", "2026-11-01T17:00:00Z", "2026-11-01T18:00:00Z")]
    now = dt("2026-11-01T16:00:00Z")
    row, = local_visits({"flights": legs}, now)
    assert row["duration_minutes"] == 16 * 60
    assert row["departure"] == "2026-11-01T17:00:00+00:00"
    assert not local_visits({"flights": legs}, dt(row["display_end"]))
    assert not local_visits({"flights": legs, "demo": True}, now)


def test_local_visits_dont_match_across_other_legs_or_invent_pns_departure():
    legs = [leg("in", "ATL", "VPS", "2026-10-31T11:00:00Z", "2026-10-31T12:00:00Z"),
            leg("elsewhere", "ATL", "PNS", "2026-10-31T13:00:00Z", "2026-10-31T14:00:00Z"),
            leg("late", "VPS", "ATL", "2026-10-31T15:00:00Z", "2026-10-31T16:00:00Z")]
    rows = local_visits({"flights": legs}, dt("2026-10-31T12:00:00Z"))
    assert len(rows) == 2
    assert all("departure" not in row and "duration_minutes" not in row for row in rows)


def test_deadhead_breaks_assumed_vps_layover():
    legs = [leg("in", "ATL", "VPS", "2026-10-31T11:00:00Z", "2026-10-31T12:00:00Z"),
            leg("dh", "VPS", "ATL", "2026-10-31T13:00:00Z", "2026-10-31T14:00:00Z", deadhead=True),
            leg("later", "VPS", "ATL", "2026-11-01T13:00:00Z", "2026-11-01T14:00:00Z")]
    row, = local_visits({"flights": legs}, dt("2026-10-31T12:00:00Z"))
    assert "departure" not in row


def test_trip_lead_in_hides_distant_roster_and_pages_each_leg():
    now = dt("2026-10-31T12:00:00Z")
    flights = [leg("distant", "ATL", "VPS", "2026-11-20T12:00:00Z", "2026-11-20T14:00:00Z")]
    status = {"snapshot": {"flights": flights, "demo": False}}
    assert not trip_preview(status["snapshot"], {}, now)
    assert [entry["view"] for entry in build_rotation(status, {}, now)] == ["home"]
    flights = [leg(str(i), "ATL", "VPS", f"2026-11-0{day}T{10+i:02}:00:00Z", f"2026-11-0{day}T{11+i:02}:00:00Z")
               for i, day in enumerate([1, 1, 2, 2, 2, 3, 3])]
    snapshot = {"flights": flights, "demo": False}
    preview = trip_preview(snapshot, {"timezone": "America/Chicago"}, now)
    assert [leg["leg_number"] for leg in preview["days"][1]["legs"]] == [1, 2, 3]
    assert len(preview["flights"]) == 7
    rotation = build_rotation({"snapshot": snapshot}, {}, now)
    assert [entry["trip_page"] for entry in rotation if entry["view"] == "trip_map"] == [0, 1, 2]
    assert not any(entry["view"] in {"details", "map", "schedule"} for entry in rotation)


def test_operating_priority_precedes_new_household_views():
    now = dt("2026-10-31T12:00:00Z")
    flight = leg("in", "ATL", "VPS", "2026-10-31T11:00:00Z", "2026-10-31T13:00:00Z")
    snapshot = {"flights": [flight], "demo": False}
    status = {"snapshot": snapshot, "local_visits": local_visits(snapshot, now),
              "custody_outlook": {"days": [{"date": "2026-10-31", "state": "unknown"}]}}
    rotation = build_rotation(status, {}, now)
    assert {"local_visit", "custody_outlook"} <= {entry["view"] for entry in rotation}
    for index, entry in enumerate(rotation):
        if entry["view"] not in {"details", "map"}:
            assert rotation[index - 1]["view"] in {"details", "map"}
            assert rotation[index - 1]["duration_seconds"] == 60
