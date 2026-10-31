"""Fictional overnight leave clocks stay distinct from scheduled departure."""
from copy import deepcopy
from datetime import datetime
from unittest.mock import patch

from flightwall import render
from flightwall.outlook import local_visits, trip_preview


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sample(*, conflict=True, chosen=True):
    flights = [{"id": "fictional-in", "number": "DL12", "origin": "ATL", "destination": "VPS",
                "start": "2027-01-13T00:00:00Z", "end": "2027-01-13T02:00:00Z", "deadhead": False},
               {"id": "fictional-out", "number": "DL13", "origin": "VPS", "destination": "ATL",
                "start": "2027-01-13T16:00:00Z", "end": "2027-01-13T17:00:00Z", "deadhead": False,
                "report": "2027-01-13T15:00:00Z"}]
    if chosen:
        flights[1]["leave_at"] = "2027-01-13T13:20:00Z"
    if conflict:
        flights[1]["leave_conflict"] = True
    return {"flights": flights, "demo": False, "last_sync": "2027-01-12T17:00:00Z", "source": "Fictional schedule"}


def state_for(view, snapshot):
    config = {"timezone": "America/Chicago", "brief_leave_times_enabled": True}
    if view == "trip_summary":
        now = stamp("2027-01-12T17:00:00Z")
        state = {**snapshot, "now": now.isoformat(), "view": view, "trip_preview": trip_preview(snapshot, config, now)}
    elif view == "local_visit":
        now = stamp("2027-01-13T03:00:00Z")
        state = {**snapshot, "now": now.isoformat(), "view": view,
                 "selected_card": local_visits(snapshot, now)[0]}
    else:
        state = {**snapshot, "now": "2027-01-13T03:00:00Z", "view": view, "selected_flight": snapshot["flights"][1]}
    return state, config


def rendered(view, *, conflict=True, chosen=True, enabled=True):
    state, config = state_for(view, sample(conflict=conflict, chosen=chosen))
    config["brief_leave_times_enabled"] = enabled
    # Display timezone must beat stale source metadata even during a backend outage.
    state["backend_error"] = True
    if view == "local_visit":
        state["selected_card"]["timezone"] = "Pacific/Honolulu"
    if view == "trip_summary":
        state["trip_preview"]["timezone"] = "Pacific/Honolulu"
    original = deepcopy(state)
    labels = []
    real_text = render._text
    def capture(draw, xy, value, *args, **kwargs):
        labels.append(render._clean(value))
        return real_text(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=capture):
        image = render.render_frame(state, config)
    assert state == original
    assert image.size == (512, 256)
    return labels, image


def test_details_separates_brief_leave_from_departure_and_keeps_date_and_warning():
    labels, _ = rendered("details")
    assert "10:00 AM" in labels  # Scheduled flight departure, not the 07:20 brief clock.
    assert "13 JAN" in labels
    assert "BRIEF LEAVE 13 JAN 7:20 AM CST" in labels
    assert "VERIFY REPORT" in labels
    assert "REPORT 9:00 AM CST" not in labels


def test_local_visit_next_day_departure_and_brief_clock_are_both_visible():
    labels, _ = rendered("local_visit")
    assert "8:00 PM" in labels
    assert "TUE 12 JAN / CST" in labels
    assert "NEXT DEPARTURE" in labels
    assert "10:00 AM" in labels
    assert "WED 13 JAN / CST" in labels
    assert "STAY / 14H 00M" in labels
    assert "BRIEF LEAVE / WED 13 JAN 7:20 AM CST" in labels
    assert "VERIFY" in labels


def test_trip_summary_inbound_row_shows_next_day_departure_and_leave_date():
    labels, _ = rendered("trip_summary")
    assert "DEP 01/13 10:00 AM CST" in labels
    assert "LEAVE 01/13 7:20 AM CST" in labels
    assert "BRIEF / VERIFY REPORT" in labels
    assert "CST / 14H00M STAY" in labels


def test_unselected_conflicting_time_warns_without_inventing_a_leave_clock():
    for view, warning in (("details", "BRIEF CONFLICT / CHECK LEAVE TIME"),
                          ("local_visit", "BRIEF CONFLICT / CHECK LEAVE TIME AND REPORT"),
                          ("trip_summary", "LEAVE TIME CONFLICT")):
        labels, _ = rendered(view, chosen=False)
        assert warning in labels
        assert not any("7:20 AM" in value for value in labels)
        assert any("10:00 AM" in value for value in labels)


def test_disabled_note_opt_in_hides_cached_clock_and_conflict_in_every_view():
    for view in ("details", "local_visit", "trip_summary"):
        labels, _ = rendered(view, enabled=False)
        assert not any("BRIEF" in value or "LEAVE" in value or "VERIFY" in value for value in labels)
        assert any("10:00 AM" in value for value in labels)
    labels, _ = rendered("details", enabled=False)
    assert "REPORT 9:00 AM CST" in labels
    labels, _ = rendered("local_visit", enabled=False)
    assert "PLANNED STAY / 14H 00M" in labels


def test_unconflicted_chosen_time_does_not_show_a_false_report_warning():
    for view in ("details", "local_visit", "trip_summary"):
        labels, _ = rendered(view, conflict=False)
        assert any("7:20 AM" in value for value in labels)
        assert not any("CONFLICT" in value or "VERIFY" in value for value in labels)


def test_trip_warning_preserves_complete_existing_stay_and_timezone_glyphs():
    _, plain = rendered("trip_summary", conflict=False)
    _, warning = rendered("trip_summary", conflict=True)
    # Adding the warning beneath the first inbound row's existing date/zone
    # line must not erase that line's last pixel row.
    assert warning.crop((333, 88, 493, 95)).tobytes() == plain.crop((333, 88, 493, 95)).tobytes()


def test_visit_end_exclusive_hides_cached_brief_information():
    state, config = state_for("local_visit", sample())
    state["now"] = state["selected_card"]["display_end"]
    labels = []
    original = render._text
    def capture(draw, xy, value, *args, **kwargs):
        labels.append(render._clean(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=capture):
        render.render_frame(state, config)
    assert "NO LOCAL VISIT TO DISPLAY" in labels
    assert not any("BRIEF" in value or "7:20 AM" in value for value in labels)
