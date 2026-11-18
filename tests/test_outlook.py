from datetime import datetime, timedelta, timezone

import pytest

from flightwall.config import ConfigStore, PUBLIC_FIELDS, REMOTE_SETTINGS, validate
from flightwall.outlook import (activity_agenda, agenda_focus, custody_outlook, family_travel_labels, local_visits,
                               planned_buzz_days, today_activities, trip_preview)
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


def test_family_travel_alternates_local_day_spans_and_rolls_into_the_ten_day_window(tmp_path):
    store = ConfigStore(tmp_path)
    config = store.update({"timezone": "America/Chicago", "demo": False, "family_travel_plan": {
        "label": "Parker Chicago",
        "spans": [{"start_date": "2027-01-06", "days": 2, "repeat_days": 42},
                  {"start_date": "2027-01-26", "days": 3, "repeat_days": 42}],
    }})
    for observed, dates in (
        ("2027-01-06T18:00:00Z", ("2027-01-06", "2027-01-07")),
        ("2027-01-26T05:59:00Z", ("2027-01-26", "2027-01-27", "2027-01-28")),
        ("2027-01-27T05:59:00Z", ("2027-01-26", "2027-01-27", "2027-01-28")),
        ("2027-01-27T06:00:00Z", ("2027-01-27", "2027-01-28")),
        ("2027-02-08T05:59:00Z", ()),
        ("2027-02-09T05:59:00Z", ("2027-02-17",)),
        ("2027-03-08T18:00:00Z", ("2027-03-09", "2027-03-10", "2027-03-11")),
        ("2027-03-29T18:00:00Z", ("2027-03-31", "2027-04-01")),
    ):
        assert family_travel_labels(config, dt(observed)) == dict.fromkeys(dates, "Parker Chicago")
    assert family_travel_labels(config | {"guest_mode": True}, dt("2027-01-26T18:00:00Z")) == {}
    assert family_travel_labels(config | {"demo": True}, dt("2027-01-26T18:00:00Z")) == {}
    assert "family_travel_plan" not in PUBLIC_FIELDS | REMOTE_SETTINGS
    assert "family_travel_plan" not in store.public()
    with pytest.raises(ValueError, match="requires the local CLI"):
        store.update({"family_travel_plan": {}}, remote=True)


@pytest.mark.parametrize("invalid", [
    {"start_date": "20270106"}, {"start_date": "2027-02-29"}, {"days": True},
    {"days": 11}, {"repeat_days": 0}, {"repeat_days": 367}, {"notes": "Omitted"},
])
def test_family_travel_rejects_noncanonical_dates_unbounded_spans_and_extra_fields(invalid):
    span = {"start_date": "2027-01-06", "days": 2, "repeat_days": 42} | invalid
    with pytest.raises(ValueError):
        validate("family_travel_plan", {"label": "Parker Chicago", "spans": [span]})


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


def test_vps_leave_time_stays_distinct_from_next_day_departure():
    legs = [leg("in", "ATL", "VPS", "2026-11-06T21:00:00Z", "2026-11-06T23:00:00Z"),
            leg("out", "VPS", "ATL", "2026-11-07T14:00:00Z", "2026-11-07T16:00:00Z",
                leave_at="2026-11-07T11:45:00Z")]
    row, = local_visits({"flights": legs}, dt("2026-11-07T01:00:00Z"))
    assert row["departure"] == "2026-11-07T14:00:00+00:00"
    assert row["leave_at"] == "2026-11-07T11:45:00+00:00"
    assert row["duration_minutes"] == 15 * 60
    for value in (None, "2026-11-07T11:45:00", "2026-11-07T14:01:00Z", "2026-11-06T22:59:00Z"):
        legs[1]["leave_at"] = value
        row, = local_visits({"flights": legs}, dt("2026-11-07T01:00:00Z"))
        assert "leave_at" not in row


def test_buzz_plan_marks_source_trip_dates_including_vps_stays_and_partial_days():
    now = dt("2026-11-05T18:00:00Z")
    snapshot = {"last_sync": now.isoformat(),
                "trip_days": [{"start_date": "2026-11-06", "end_date": "2026-11-11"}], "flights": [
        leg("in", "ATL", "VPS", "2026-11-07T03:00:00Z", "2026-11-07T05:00:00Z"),
        leg("out", "VPS", "ATL", "2026-11-07T23:00:00Z", "2026-11-08T01:00:00Z",
            leave_at="2026-11-07T21:00:00Z", report="2026-11-07T22:00:00Z"),
    ]}
    config = {"timezone": "America/Chicago", "brief_leave_times_enabled": True}
    plan = planned_buzz_days(snapshot, config, now)
    assert plan == {f"2026-11-{day:02}": {"gone": True} for day in range(6, 11)}
    snapshot["flights"][1]["leave_conflict"] = True
    assert planned_buzz_days(snapshot, config, now) == plan
    assert not planned_buzz_days(snapshot, config, now + timedelta(hours=1))
    assert not planned_buzz_days(snapshot | {"demo": True}, config, now)
    assert not planned_buzz_days(snapshot | {"error": "Refresh failed"}, config, now)


def test_buzz_plan_old_caches_mark_known_duties_without_filling_uncertain_gaps():
    now = dt("2026-11-05T18:00:00Z")
    snapshot = {"last_sync": now.isoformat(), "flights": [
        leg("in", "ATL", "VPS", "2026-11-07T03:00:00Z", "2026-11-07T05:00:00Z"),
        leg("out", "ATL", "LAX", "2026-11-07T23:00:00Z", "2026-11-08T01:00:00Z"),
        leg("dh", "LAX", "ATL", "2026-11-10T00:00:00Z", "2026-11-10T03:00:00Z", deadhead=True),
    ]}
    plan = planned_buzz_days(snapshot, {"timezone": "America/Chicago", "show_deadheads": False}, now)
    assert plan == {f"2026-11-{day:02}": {"gone": True} for day in (6, 7, 9)}


def test_owner_trip_dates_fill_only_explicit_dates_and_remain_local_private_settings(tmp_path):
    now = dt("2026-11-05T18:00:00Z")
    snapshot = {"last_sync": now.isoformat(), "flights": [
        leg("out", "ATL", "VPS", "2026-11-06T20:00:00Z", "2026-11-06T21:00:00Z"),
        leg("return", "VPS", "ATL", "2026-11-10T15:00:00Z", "2026-11-10T16:00:00Z"),
    ]}
    store = ConfigStore(tmp_path)
    store.update({"timezone": "America/Chicago"})
    assert set(planned_buzz_days(snapshot, store.get(), now)) == {"2026-11-06", "2026-11-10"}
    store.update({"planned_trip_days": '[{"start_date":"2026-11-06","end_date":"2026-11-11"}]'})
    assert planned_buzz_days(snapshot, store.get(), now) == {
        f"2026-11-{day:02}": {"gone": True} for day in range(6, 11)}
    assert "planned_trip_days" not in PUBLIC_FIELDS | REMOTE_SETTINGS
    assert "planned_trip_days" not in store.public()
    assert ConfigStore(tmp_path).get()["planned_trip_days"] == store.get()["planned_trip_days"]


@pytest.mark.parametrize("value", [
    '{}', '[{"start_date":"2026-11-06"}]',
    '[{"start_date":"2026-11-06","end_date":"2026-11-11","name":"Private"}]',
    '[{"start_date":"2026-11-06","end_date":"2026-11-06"}]',
    '[{"start_date":"2026-11-06","end_date":"2026-12-08"}]',
    '[{"start_date":"2026-11-31","end_date":"2026-12-02"}]',
    '[{"start_date":"2026-11-06","end_date":false}]',
    '[{"start_date":"2026-11-06","end_date":"2026-11-11"},'
    '{"start_date":"2026-11-06","end_date":"2026-11-11"}]',
    '[' + ','.join('{"start_date":"2026-11-06","end_date":"2026-11-11"}' for _ in range(33)) + ']',
    'x' * 8193,
])
def test_owner_trip_dates_reject_malformed_or_oversized_settings(value):
    with pytest.raises(ValueError):
        validate("planned_trip_days", value)


def test_buzz_plan_covers_ten_dates_and_uses_exact_midnight_boundaries_across_dst():
    now = dt("2026-10-31T18:00:00Z")
    snapshot = {"last_sync": now.isoformat(), "flights": [
        leg("night", "ATL", "VPS", "2026-11-01T05:15:00Z", "2026-11-01T06:00:00Z",
            report="2026-11-01T04:30:00Z"),
    ], "trip_days": [{"start_date": "2026-11-09", "end_date": "2026-11-11"}]}
    plan = planned_buzz_days(snapshot, {"timezone": "America/Chicago"}, now)
    assert plan == {"2026-10-31": {"gone": True}, "2026-11-01": {"gone": True}, "2026-11-09": {"gone": True}}
    snapshot["flights"][0]["end"] = "2026-11-01T05:00:00Z"
    snapshot["flights"][0]["start"] = "2026-11-01T04:45:00Z"
    assert "2026-11-01" not in planned_buzz_days(snapshot, {"timezone": "America/Chicago"}, now)


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
    assert [entry["view"] for entry in rotation[:2]] == ["details", "map"]
    assert [entry["duration_seconds"] for entry in rotation[:2]] == [60, 60]
    assert all(entry["view"] not in {"details", "map", "trip_map"} for entry in rotation[2:])


def test_activities_rotate_separately_and_page_without_exposing_guest_details():
    now = dt("2026-10-31T12:00:00Z")
    activity = {"title": "Soccer Game", "start": now.isoformat(), "end": "2026-10-31T13:00:00Z"}
    outlook = {"days": [{"date": "2026-10-31", "state": "home"}], "stale": False,
               "last_sync": now.isoformat(), "activity_last_sync": now.isoformat(),
               "activities": [dict(activity, title=f"Activity {index}") for index in range(7)]}
    status = {"snapshot": {"demo": False}, "custody_outlook": outlook}
    deck = build_rotation(status, {}, now)
    assert [entry["agenda_page"] for entry in deck if entry["view"] == "today_activities"] == [0, 1]
    guest = build_rotation(status, {"guest_mode": True}, now)
    assert {entry["view"] for entry in guest} == {"home", "custody_outlook"}


def test_agenda_focus_marks_concurrent_rows_and_advances_at_exact_boundaries():
    now = dt("2026-10-31T18:00:00Z")
    activities = [
        {"title": "All day", "start": "2026-10-31T05:00:00Z", "end": "2026-11-01T05:00:00Z", "all_day": True},
        {"title": "Completed", "start": "2026-10-31T17:00:00Z", "end": "2026-10-31T18:00:00Z"},
        {"title": "Soccer", "start": "2026-10-31T17:45:00Z", "end": "2026-10-31T18:30:00Z"},
        {"title": "Dance", "start": "2026-10-31T18:00:00Z", "end": "2026-10-31T18:30:00Z"},
        {"title": "Violin", "start": "2026-10-31T18:20:00Z", "end": "2026-10-31T18:40:00Z"},
        {"title": "Viola", "start": "2026-10-31T18:20:00Z", "end": "2026-10-31T18:50:00Z"},
        {"title": "Later", "start": "2026-10-31T19:00:00Z", "end": "2026-10-31T19:30:00Z"},
    ]
    outlook = {"stale": False, "last_sync": now.isoformat(), "activity_last_sync": now.isoformat(),
               "activities": activities}
    rows = today_activities(outlook, {"timezone": "America/Chicago"}, now)
    assert agenda_focus(rows, now) == ["", "", "NOW", "NOW", "NEXT", "NEXT", ""]
    assert agenda_focus(rows, now + timedelta(minutes=30)) == ["", "", "", "", "NOW", "NOW", "NEXT"]
    assert agenda_focus(rows, now + timedelta(hours=2)) == [""] * len(rows)
    assert agenda_focus([], now) == []
    expiry = now + timedelta(minutes=30)
    assert "Completed" in [row[2] for row in today_activities(outlook, {}, expiry - timedelta(seconds=1))]
    at_expiry = today_activities(outlook, {}, expiry)
    assert "Completed" not in [row[2] for row in at_expiry]
    assert "All day" in [row[2] for row in at_expiry]


def test_agenda_focus_orders_repeated_wall_times_by_instant():
    now = dt("2026-11-01T06:30:00Z")
    outlook = {"stale": False, "last_sync": now.isoformat(), "activity_last_sync": now.isoformat(),
               "activities": [{"title": "Earlier", "start": "2026-11-01T06:20:00Z", "end": "2026-11-01T06:40:00Z"},
                              {"title": "Later", "start": "2026-11-01T07:20:00Z", "end": "2026-11-01T07:40:00Z"}]}
    rows = today_activities(outlook, {"timezone": "America/Chicago"}, now)
    assert rows[0][0].strftime("%H:%M") == rows[1][0].strftime("%H:%M")
    assert agenda_focus(rows, rows[0][0] + timedelta(minutes=10)) == ["NOW", "NEXT"]
    assert agenda_focus(rows, now + timedelta(hours=1)) == ["", "NOW"]


def test_agenda_switches_only_when_last_activity_and_its_grace_are_over():
    config = {"timezone": "America/Chicago"}
    source = [
        {"title": "Rory Soccer Practice", "start": "2026-10-31T21:00:00Z", "end": "2026-10-31T22:00:00Z"},
        {"title": "Nora Dance", "start": "2026-11-01T01:00:00Z", "end": "2026-11-01T02:00:00Z"},
        {"title": "Violin", "start": "2026-11-01T20:00:00Z", "end": "2026-11-01T21:00:00Z"},
    ]

    def at(clock):
        now = dt(clock)
        outlook = {"stale": False, "last_sync": now.isoformat(), "activity_last_sync": now.isoformat(),
                   "activity_dates": ["2026-10-31", "2026-11-01"], "activities": source}
        return activity_agenda(outlook, config, now), outlook, now

    for clock in ("2026-10-31T23:00:00Z", "2026-11-01T02:29:59Z"):
        agenda, _, _ = at(clock)
        assert agenda["date"].isoformat() == "2026-10-31"
        assert agenda["tomorrow"] is False
        assert [row[2] for row in agenda["rows"]] == ["Nora Dance"]
    agenda, outlook, now = at("2026-11-01T02:30:00Z")
    assert agenda["date"].isoformat() == "2026-11-01"
    assert agenda["tomorrow"] is True
    assert [row[2] for row in agenda["rows"]] == ["Violin"]
    assert len([row for row in build_rotation({"snapshot": {"demo": False}, "custody_outlook": outlook}, config, now)
                if row["view"] == "today_activities"]) == 1


def test_tomorrow_agenda_requires_explicit_coverage_and_preserves_privacy_gates():
    now = dt("2026-11-01T02:30:00Z")
    config = {"timezone": "America/Chicago"}
    outlook = {"stale": False, "last_sync": now.isoformat(), "activity_last_sync": now.isoformat(),
               "activities": [{"title": "Violin", "start": "2026-11-01T20:00:00Z", "end": "2026-11-01T21:00:00Z"}]}
    assert activity_agenda(outlook, config, now)["tomorrow"] is False
    for coverage in (None, [], ["2026-10-31"], ["2026-11-01"]):
        assert activity_agenda(outlook | {"activity_dates": coverage}, config, now)["tomorrow"] is False
    outlook["activity_dates"] = ["2026-10-31", "2026-11-01"]
    assert activity_agenda(outlook, config, now)["tomorrow"] is True
    for change in ({"guest_mode": True}, {"demo": True}, {"kids_filter_enabled": False}):
        assert activity_agenda(outlook, config | change, now) is None
    assert activity_agenda(outlook, config, now, backend_available=False) is None
    assert activity_agenda(outlook | {"stale": True}, config, now) is None
    assert activity_agenda(outlook, config, now + timedelta(minutes=31)) is None


def test_tomorrow_agenda_uses_local_dates_through_dst_and_becomes_today_at_midnight():
    config = {"timezone": "America/Chicago"}
    source = [{"title": "Nora Dance", "start": "2026-11-01T20:00:00Z", "end": "2026-11-01T21:00:00Z"}]
    for clock, tomorrow in (("2026-11-01T04:59:59Z", True), ("2026-11-01T05:00:00Z", False),
                            ("2026-11-01T07:30:00Z", False)):
        now = dt(clock)
        outlook = {"stale": False, "last_sync": now.isoformat(), "activity_last_sync": now.isoformat(),
                   "activity_dates": ["2026-10-31", "2026-11-01"], "activities": source}
        agenda = activity_agenda(outlook, config, now)
        assert agenda["date"].isoformat() == "2026-11-01"
        assert agenda["tomorrow"] is tomorrow
        assert [row[2] for row in agenda["rows"]] == ["Nora Dance"]
