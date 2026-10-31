"""Synthetic notes exercise explicit leave labels without retaining prose."""
from copy import deepcopy
from datetime import datetime
import json

from flightwall.brief import attach_brief_leaves, clean_brief_leaves, parse_brief_leave_notes


def overnight(**extra):
    return {"summary": "🏠 Buzz Flight Brief — VPS Home Overnight (Jan 12–13)",
            "start": {"dateTime": "2027-01-12T20:00:00-06:00"},
            "end": {"dateTime": "2027-01-13T09:00:00-06:00"},
            "description": "LEAVE HOME FOR VPS — requested aircraft-arrival formula: RECOMMENDED\n7:35 AM\n"
                           "IMPORTANT REPORT-TIME CONFLICT: To meet scheduled report on time, leave by 7:20 AM.\n"
                           "Crew: PRIVATE-CREW\nHotel: PRIVATE-HOTEL\n555-123-4567",
            **extra}


def flights(**outbound_extra):
    return [{"id": "in", "number": "DL12", "origin": "ATL", "destination": "VPS",
             "start": "2027-01-13T00:00:00Z", "end": "2027-01-13T02:00:00Z"},
            {"id": "out", "number": "DL13", "origin": "VPS", "destination": "ATL",
             "start": "2027-01-13T16:00:00Z", "end": "2027-01-13T17:00:00Z",
             "report": "2027-01-13T15:00:00Z", **outbound_extra}]


def test_only_minimal_distinct_clocks_are_retained_and_notes_unchanged():
    event = overnight(id="PRIVATE-ID", attendees=[{"email": "private@example.invalid"}])
    original = deepcopy(event)
    rows = parse_brief_leave_notes([event], "America/Chicago")
    assert rows == [{"airport": "VPS", "arrival": "2027-01-13T02:00:00+00:00", "report": "2027-01-13T15:00:00+00:00",
                     "recommended_at": "2027-01-13T13:35:00+00:00", "report_safe_at": "2027-01-13T13:20:00+00:00"}]
    assert event == original
    assert "PRIVATE" not in json.dumps(rows)
    assert "555" not in json.dumps(rows)


def test_conflict_default_never_selects_a_clock_and_explicit_policy_keeps_warning():
    notes = parse_brief_leave_notes([overnight()], "America/Chicago")
    for policy, expected in (("conflict", None), ("unknown", None),
                             ("recommended", "2027-01-13T13:35:00+00:00"),
                             ("report_safe", "2027-01-13T13:20:00+00:00")):
        legs = flights()
        attach_brief_leaves(legs, notes, policy)
        assert legs[1].get("leave_at") == expected
        assert legs[1]["leave_conflict"] is True
        assert "leave_at" not in legs[0]


def test_report_safe_soft_wrap_and_single_nonconflicting_time():
    event = overnight(description="To meet scheduled report on time,\nleave by 7:20 AM. PRIVATE-CREW")
    notes = parse_brief_leave_notes([event], "America/Chicago")
    legs = flights()
    attach_brief_leaves(legs, notes)
    assert legs[1]["leave_at"] == "2027-01-13T13:20:00+00:00"
    assert "leave_conflict" not in legs[1]
    event["description"] += "\nLEAVE HOME FOR VPS — formula: RECOMMENDED 7:20 AM"
    legs = flights()
    attach_brief_leaves(legs, parse_brief_leave_notes([event], "America/Chicago"))
    assert legs[1]["leave_at"] == "2027-01-13T13:20:00+00:00"
    assert "leave_conflict" not in legs[1]


def test_partial_conflicts_multiple_values_unrelated_titles_and_bad_dates_hide():
    base = overnight()
    events = [overnight(summary="Report: 0900L"), overnight(summary="Buzz Flight Brief — VPS Home Overnight (Jan 11–13)"),
              overnight(summary="Buzz Flight Brief — PNS Home Overnight (Jan 12–13)"),
              overnight(description="IMPORTANT REPORT-TIME CONFLICT: To meet scheduled report on time, leave by 7:20 AM."),
              overnight(description=base["description"] + "\nTo meet scheduled report on time, leave by 7:10 AM."),
              overnight(description=base["description"].replace("7:35 AM", "12:35 PM")),
              overnight(description="X" * 32769), overnight(description=None),
              overnight(start={"date": "2027-01-12"}), overnight(status="cancelled")]
    for event in events:
        assert parse_brief_leave_notes([event], "America/Chicago") == []
    assert parse_brief_leave_notes([base], "America/New_York") == []


def test_latest_cancellation_or_edit_resolves_before_note_parse():
    old = overnight(id="same", updated="2027-01-01T00:00:00Z")
    cancel = {"id": "same", "status": "cancelled", "updated": "2027-01-02T00:00:00Z"}
    for events in ([old, cancel], [cancel, old]):
        assert parse_brief_leave_notes(events, "America/Chicago") == []
    edit = overnight(id="same", updated="2027-01-03T00:00:00Z", description="To meet scheduled report on time, leave by 7:10 AM.")
    assert parse_brief_leave_notes([edit, old, cancel], "America/Chicago")[0]["report_safe_at"] == "2027-01-13T13:10:00+00:00"


def test_dst_ambiguous_and_nonexistent_clocks_are_not_guessed():
    for title, start, end in [
        ("Buzz Flight Brief — VPS Home Overnight (Oct 31–Nov 1)", "2026-10-31T20:00:00-05:00", "2026-11-01T03:00:00-06:00"),
        ("Buzz Flight Brief — VPS Home Overnight (Mar 7–8)", "2026-03-07T20:00:00-06:00", "2026-03-08T04:00:00-05:00")]:
        clock = "1:30 AM" if "Nov" in title else "2:30 AM"
        event = overnight(summary=title, start={"dateTime": start}, end={"dateTime": end},
                          description="To meet scheduled report on time, leave by " + clock + ".")
        assert parse_brief_leave_notes([event], "America/Chicago") == []


def test_previous_civil_day_leave_requires_single_valid_candidate_inside_interval():
    event = overnight(end={"dateTime": "2027-01-13T00:30:00-06:00"},
                      description="To meet scheduled report on time, leave by 11:50 PM.")
    rows = parse_brief_leave_notes([event], "America/Chicago")
    assert rows[0]["report_safe_at"] == "2027-01-13T05:50:00+00:00"


def test_association_needs_exact_arrival_report_and_uninterrupted_operating_legs():
    notes = parse_brief_leave_notes([overnight()], "America/Chicago")
    cases = [flights(report="2027-01-13T15:01:00Z"), flights(deadhead=True), flights(origin="PNS")]
    inbound_changed = flights()
    inbound_changed[0]["end"] = "2027-01-13T02:01:00Z"
    cases.append(inbound_changed)
    inbound_deadhead = flights()
    inbound_deadhead[0]["deadhead"] = True
    cases.append(inbound_deadhead)
    interrupted = flights()
    interrupted.insert(1, {"id": "interruption", "number": "DL77", "origin": "ATL", "destination": "MEM",
                           "start": "2027-01-13T03:00:00Z", "end": "2027-01-13T04:00:00Z"})
    cases.append(interrupted)
    for legs in cases:
        attach_brief_leaves(legs, notes, "report_safe")
        assert all("leave_at" not in row and "leave_conflict" not in row for row in legs)


def test_cache_rejects_extra_raw_data_bad_clocks_and_missing_timezone():
    row = parse_brief_leave_notes([overnight()], "America/Chicago")[0]
    assert clean_brief_leaves([row]) == [row]
    for changed in [dict(row, description="PRIVATE"), dict(row, airport="PNS"), dict(row, report_safe_at=None),
                    dict(row, recommended_at="2027-01-13T13:35:00"), dict(row, recommended_at="2027-01-13T15:01:00Z")]:
        assert clean_brief_leaves([changed]) == []


def test_no_notes_removes_old_derived_fields():
    legs = flights(leave_at="2027-01-13T13:20:00Z", leave_conflict=True)
    attach_brief_leaves(legs, [])
    assert "leave_at" not in legs[1] and "leave_conflict" not in legs[1]


def test_html_paragraphs_inline_labels_breaks_and_entities_match_plain_text():
    plain = parse_brief_leave_notes([overnight()], "America/Chicago")
    note = ("<p><b>LEAVE HOME FOR VPS</b> &mdash; requested aircraft-arrival formula: "
            "<strong>RECOMMENDED</strong><br/>7:35&nbsp;AM</p>"
            "<p>IMPORTANT REPORT-TIME CONFLICT: To meet scheduled report on time,<br>"
            "leave by <span style='color: red'>7:20 AM</span>.</p>"
            "<p>Crew: PRIVATE-CREW<br>Hotel: PRIVATE-HOTEL</p>")
    source = overnight(description=note)
    original = deepcopy(source)
    assert parse_brief_leave_notes([source], "America/Chicago") == plain
    assert source == original
    assert "PRIVATE" not in json.dumps(parse_brief_leave_notes([source], "America/Chicago"))


def test_html_active_hidden_comment_and_attribute_contents_never_supply_clocks():
    hidden_clock = "To meet scheduled report on time, leave by 5:10 AM."
    hidden = ["<script>" + hidden_clock + "</script>", "<style>" + hidden_clock + "</style>",
              "<template><p>" + hidden_clock + "</p></template>", "<p hidden>" + hidden_clock + "</p>",
              "<p aria-hidden='true'>" + hidden_clock + "</p>",
              "<p style='color:red;display: none !important;'>" + hidden_clock + "</p>",
              "<p style='visibility: hidden'>" + hidden_clock + "</p>", "<!-- " + hidden_clock + " -->",
              "<a href='https://example.invalid/' title='" + hidden_clock + "'>Link</a>"]
    expected = parse_brief_leave_notes([overnight(description="To meet scheduled report on time, leave by 7:20 AM.")], "America/Chicago")
    for prefix in hidden:
        note = prefix + "<p>To meet scheduled report on time, leave by 7:20 AM.</p>"
        assert parse_brief_leave_notes([overnight(description=note)], "America/Chicago") == expected
        assert parse_brief_leave_notes([overnight(description=prefix)], "America/Chicago") == []


def test_html_unknown_malformed_declarations_and_bounds_reject_the_note():
    clock = "To meet scheduled report on time, leave by 7:20 AM."
    for note in ("<unknown>" + clock + "</unknown>", "<p><span>" + clock + "</p></span>",
                 "<p>" + clock, "<!DOCTYPE html><p>" + clock + "</p>", "<?xml version='1.0'?><p>" + clock + "</p>",
                 "<div>" * 33 + clock + "</div>" * 33,
                 "<br>" * 513 + clock, "<p>" + "X" * 32768 + clock + "</p>"):
        assert parse_brief_leave_notes([overnight(description=note)], "America/Chicago") == []
