"""Synthetic menu details and tomorrow previews preserve dates and expiry."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

from flightwall import render
from flightwall.card_preferences import CARD_DEFAULTS
from flightwall.household_policy import build_rotation, filter_personal_items
from flightwall.personal import PersonalError, PersonalStore, dinner_meal_date, personal_card_visible

NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)  # 15:00 CST after fall-back.


@pytest.fixture
def household(tmp_path):
    config = {"timezone": "America/Chicago", "personal_enabled": True, "kids_filter_enabled": True,
              "appointment_duration_minutes": 60, "dinner_start": "14:30", "dinner_end": "19:00"}
    store = PersonalStore(tmp_path, lambda: dict(config))
    with patch("flightwall.personal._now", return_value=NOW) as clock:
        yield store, config, clock


def meal(store, **changes):
    return store.create({"kind": "dinner", "title": "Fictional tacos", "date": "2026-11-01", **changes})["item"]


def status(items, kids="home"):
    return {"snapshot": {"demo": False, "flights": []}, "personal_items": items,
            "kids": {"state": kids, "stale": False}, "presence": {}}


def labels_for(item, config, now=NOW, **extra):
    labels = []
    state = {"view": "personal", "now": now.isoformat(), "personal_items": [item], "selected_personal_item": item,
             "household_context": {"kids_home": True}, **extra}
    before = deepcopy(state)
    original = render._text
    def capture(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=capture):
        image = render.render_frame(state, config)
    assert state == before
    return labels, image


def test_optional_sides_and_ready_time_do_not_move_or_extend_the_dinner_window(household):
    store, config, clock = household
    item = meal(store, sides="Fictional rice and beans", ready_time="19:15")
    assert item["sides"] == "Fictional rice and beans"
    assert item["ready_at"] == "2026-11-02T01:15:00.000000Z"
    assert item["start"] == item["display_start"] == "2026-11-01T20:30:00.000000Z"
    assert item["end"] == item["display_end"] == "2026-11-02T01:00:00.000000Z"
    assert "ready_until" not in item
    assert store.visible(datetime(2026, 11, 2, 0, 59, 59, tzinfo=timezone.utc)) == [item]
    assert store.visible(datetime(2026, 11, 2, 1, tzinfo=timezone.utc)) == []
    assert json.loads(store.path.read_text())["schema_version"] == 1


def test_ready_window_is_explicit_dated_metadata_and_survives_reload(household):
    store, config, clock = household
    item = meal(store, date="2026-11-02", ready_window_start="18:00", ready_window_end="18:45")
    assert item["ready_at"] == "2026-11-03T00:00:00.000000Z"
    assert item["ready_until"] == "2026-11-03T00:45:00.000000Z"
    assert dinner_meal_date(item, config).isoformat() == "2026-11-02"
    reloaded = PersonalStore(store.data_dir, lambda: config).list_items(NOW)
    assert reloaded == [item]


def test_old_card_fields_are_preserved_and_metadata_replacement_never_resurrects_retry(household):
    store, config, clock = household
    old_payload = {"kind": "dinner", "title": "Fictional old meal", "request_id": "old-request", "source": "alexa"}
    old = store.create(old_payload)["item"]
    assert not {"sides", "ready_at", "ready_until"}.intersection(old)
    tomorrow = meal(store, date="2026-11-02", title="Fictional tomorrow meal")
    new = meal(store, request_id="new-request", sides="Fictional corn", ready_time="18:45")
    replay = store.create(old_payload)
    assert replay["created"] is False and replay["item"]["cancelled"] is True
    assert replay["item"]["id"] == old["id"]
    assert [row["id"] for row in store.visible(NOW)] == [new["id"], tomorrow["id"]]
    assert next(row for row in store.list_items() if row["id"] == tomorrow["id"]) == tomorrow


def test_retry_hash_covers_details_and_keeps_original_ready_date_after_config_changes(household):
    store, config, clock = household
    payload = {"kind": "dinner", "title": "Fictional pasta", "sides": "Fictional peas", "ready_time": "18:45",
               "request_id": "same-save"}
    item = store.create(payload)["item"]
    before = store.path.read_bytes()
    with pytest.raises(PersonalError, match="different card details"):
        store.create(payload | {"ready_time": "18:50"})
    assert store.path.read_bytes() == before
    config.update(timezone="Pacific/Auckland", dinner_start="16:00", dinner_end="20:00")
    clock.return_value = NOW + timedelta(days=1)
    assert store.create(payload)["item"] == item


@pytest.mark.parametrize("changes", [
    {"sides": ""}, {"sides": "x" * 121}, {"sides": "unsafe\nPRIVATE"}, {"sides": ["rice"]},
    {"ready_time": "6:45"}, {"ready_time": "24:00"}, {"ready_time": "18:00", "ready_window_start": "18:00", "ready_window_end": "18:30"},
    {"ready_window_start": "18:00"}, {"ready_window_end": "18:30"},
    {"ready_window_start": "19:00", "ready_window_end": "18:00"},
    {"ready_window_start": "23:00", "ready_window_end": "00:30"},
])
def test_unsupported_or_incomplete_details_fail_without_writing(household, changes):
    store, config, clock = household
    meal(store)
    before = store.path.read_bytes()
    with pytest.raises(PersonalError) as error:
        meal(store, **changes)
    assert "PRIVATE" not in str(error.value)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("day, ready", [("2026-03-08", "02:30"), ("2026-11-01", "01:30")])
def test_ready_clock_dst_gap_and_fold_require_clarification(household, day, ready):
    store, config, clock = household
    clock.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(PersonalError, match="clocks change"):
        meal(store, date=day, ready_time=ready)
    assert not store.path.exists()


def test_appointment_schema_does_not_accept_dinner_details(household):
    store, config, clock = household
    with pytest.raises(PersonalError, match="only to dinner"):
        store.create({"kind": "appointment", "title": "Fictional appointment", "date": "2026-11-02", "time": "10:00",
                      "sides": "Fictional rice"})
    assert not store.path.exists()


def test_today_and_tomorrow_menu_visibility_share_only_the_current_afternoon_window(household):
    store, config, clock = household
    today = meal(store)
    tomorrow = meal(store, date="2026-11-02", title="Fictional tomorrow meal")
    later = meal(store, date="2026-11-03", title="Fictional later meal")
    original = deepcopy((today, tomorrow, later))
    for stamp, expected in (("2026-11-01T14:29:59.999999-06:00", []),
                            ("2026-11-01T14:30:00-06:00", [today, tomorrow]),
                            ("2026-11-01T18:59:59.999999-06:00", [today, tomorrow]),
                            ("2026-11-01T19:00:00-06:00", []),
                            ("2026-11-02T14:30:00-06:00", [tomorrow, later])):
        instant = datetime.fromisoformat(stamp)
        assert store.visible(instant) == expected
        assert [row for row in (today, tomorrow, later) if personal_card_visible(row, config, instant)] == expected
    assert (today, tomorrow, later) == original


def test_tomorrow_projection_respects_custom_window_without_changing_saved_date(household):
    store, config, clock = household
    config.update(dinner_start="16:00", dinner_end="18:30")
    item = meal(store, date="2026-11-02", ready_time="19:15")
    assert store.visible(datetime.fromisoformat("2026-11-01T15:59:59-06:00")) == []
    assert store.visible(datetime.fromisoformat("2026-11-01T16:00:00-06:00")) == [item]
    assert store.visible(datetime.fromisoformat("2026-11-01T18:30:00-06:00")) == []
    assert dinner_meal_date(item, config).isoformat() == "2026-11-02"
    assert item["ready_at"].startswith("2026-11-03T01:15")


def test_kids_home_requirement_remains_fresh_and_does_not_infer_from_phones(household):
    store, config, clock = household
    items = [meal(store), meal(store, date="2026-11-02")]
    for value in ("away", "unknown"):
        result = status(items, value)
        result["presence"] = {"lisa": {"state": "home", "stale": False}, "chris": {"state": "away", "stale": False}}
        assert filter_personal_items(items, result, config, NOW) == []
    stale = status(items)
    stale["kids"]["stale"] = True
    assert filter_personal_items(items, stale, config, NOW) == []
    assert filter_personal_items(items, status(items), config, NOW) == items
    assert filter_personal_items(items, status(items, "unknown"), config | {"kids_filter_enabled": False}, NOW) == items


def test_voice_context_exposes_policy_only_and_never_current_kids_or_calendar_data(household):
    store, config, clock = household
    config.update(family_calendar_id="PRIVATE-CALENDAR", kids={"state": "away"})
    context = store.voice_context()
    assert context["dinner_tomorrow_preview"] is True and context["dinner_requires_kids_home"] is True
    assert set(context) == {"timezone", "appointment_duration_minutes", "dinner_start", "dinner_end",
                            "dinner_tomorrow_preview", "dinner_requires_kids_home"}
    assert "PRIVATE" not in json.dumps(context) and "away" not in json.dumps(context)


def test_tomorrow_render_has_clear_date_sides_ready_clock_and_todays_clear_time(household):
    store, config, clock = household
    item = meal(store, date="2026-11-02", title="Fictional roast", sides="Fictional rice and beans", ready_time="19:15")
    labels, image = labels_for(item, config)
    assert "TOMORROW / Mon 02 Nov" in labels and "TOMORROW'S DINNER" in labels
    assert "FICTIONAL ROAST" in labels and "SIDES / FICTIONAL RICE AND BEANS" in labels
    assert "EXPECTED READY / 7:15 PM CST" in labels and "CLEARS AT 7:00 PM CST" in labels
    assert "SNACKS OK" in labels and image.size == (512, 256)
    expired, _ = labels_for(item, config, datetime.fromisoformat("2026-11-01T19:00:00-06:00"), backend_error=True)
    assert "FICTIONAL ROAST" not in expired and "YOUR HOME BOARD IS CLEAR" in expired
    away, _ = labels_for(item, config, household_context={"kids_home": False})
    assert "FICTIONAL ROAST" not in away


def test_ready_window_render_is_expected_time_and_never_the_visibility_deadline(household):
    store, config, clock = household
    item = meal(store, ready_window_start="18:00", ready_window_end="18:45")
    labels, _ = labels_for(item, config)
    assert "EXPECTED READY / 6:00 PM - 6:45 PM CST" in labels
    assert "CLEARS AT 7:00 PM CST" in labels


def test_card_preferences_filter_before_priority_and_leave_a_usable_home_fallback(household):
    store, config, clock = household
    dinner = meal(store)
    appointment = store.create({"kind": "appointment", "title": "Fictional visit", "date": "2026-11-01", "time": "18:00"})["item"]
    current = {"id": "f", "number": "DL123", "origin": "ATL", "destination": "MEM",
               "start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}
    data = status([dinner, appointment])
    data["snapshot"]["flights"] = [current]
    cfg = config | {key: False for key in CARD_DEFAULTS}
    rows = build_rotation(data, cfg, NOW)
    assert [row["view"] for row in rows] == ["home"]
    rows = build_rotation(data, cfg | {"appointment_cards_visible": True}, NOW)
    assert [row["item"]["kind"] for row in rows if row["view"] == "personal"] == ["appointment"]
    assert not any(row["view"] in {"details", "map", "schedule"} for row in rows)


def test_radar_card_is_immediately_after_home_weather_outside_flight_priority():
    cfg = {"weather_enabled": True, "national_radar_enabled": True}
    data = {"snapshot": {"demo": False, "flights": []}, "national_radar": {"status": "ready"}}
    views = [row["view"] for row in build_rotation(data, cfg, NOW)]
    assert views[views.index("weather") + 1] == "national_radar"
    assert "national_radar" not in [row["view"] for row in build_rotation(data, cfg | {"radar_card_visible": False}, NOW)]


def test_cached_disabled_dinner_and_home_minicard_never_repaint_the_hidden_meal(household):
    store, config, clock = household
    item = meal(store, title="Fictional hidden meal", sides="Fictional hidden side")
    cfg = config | {"dinner_cards_visible": False}
    labels, _ = labels_for(item, cfg)
    assert "HOME / TODAY" in labels
    assert not any("HIDDEN MEAL" in value or "HIDDEN SIDE" in value for value in labels)
    home, _ = labels_for(item, cfg, view="home")
    assert not any("HIDDEN MEAL" in value for value in home)
