"""Clock labels remain readable and date-aware across the Central DST change."""
from copy import deepcopy
from unittest.mock import patch

from flightwall import render


def labels_for(view, *, now="2026-11-01T04:59:00Z"):
    flight = {"id": "fictional-dst", "number": "DEMO 12", "origin": "ATL", "destination": "VPS",
              "start": "2026-11-01T05:00:00Z", "end": "2026-11-01T18:00:00Z",
              "report": "2026-11-01T04:30:00Z", "deadhead": False}
    state = {"now": now, "view": view, "demo": True, "flights": [flight], "selected_flight": flight}
    before = deepcopy(state)
    calls = []
    real = render._text

    def capture(draw, xy, value, *args, **kwargs):
        calls.append((xy, str(value), args, kwargs))
        return real(draw, xy, value, *args, **kwargs)

    with patch.object(render, "_text", side_effect=capture):
        image = render.render_frame(state, {"timezone": "America/Chicago"})
    assert state == before
    return calls, image


def test_details_distinguish_midnight_noon_and_dst_without_altering_stored_instants():
    calls, image = labels_for("details")
    labels = [value for _, value, _, _ in calls]
    assert "11:59 PM CDT" in labels
    assert "12:00 AM" in labels
    assert "12:00 PM" in labels
    assert "DEPART / CDT" in labels
    assert "ARRIVE / CST" in labels
    assert "REPORT 11:30 PM CDT" in labels
    assert "01 Nov" in labels
    assert image.size == (512, 256)


def test_schedule_times_keep_both_ampm_markers_in_the_existing_columns():
    calls, _ = labels_for("schedule")
    depart = next(row for row in calls if row[0] == (376, 86))
    arrive = next(row for row in calls if row[0] == (489, 86))
    assert depart[1] == "12:00 AM"
    assert arrive[1] == "12:00 PM"
    # The full AM/PM clocks leave a gap between the two schedule columns.
    assert depart[0][0] + render._width(depart[1]) < arrive[0][0] - render._width(arrive[1])


def test_home_clock_includes_period_without_overlapping_household_cards():
    calls, _ = labels_for("home", now="2026-11-01T18:59:00Z")
    clock = next(row for row in calls if row[0] == (26, 73))
    assert clock[1] == "12:59 PM"
    scale = clock[2][1]
    assert render._width(clock[1], scale) <= 204
    assert clock[0][0] + render._width(clock[1], scale) < 245
