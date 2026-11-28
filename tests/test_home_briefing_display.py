from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from flightwall.config import DEFAULTS
from flightwall.display import ANIMATED_VIEWS, RotationCursor, entry_render_state, fresh_household_status
from flightwall.household_policy import HOME_BRIEFING_VIEWS, build_rotation
from flightwall.render import render_frame


NOW = datetime(2026, 11, 27, 18, tzinfo=timezone.utc)
CONFIG = {**DEFAULTS, "demo": False, "presence_enabled": True, "timezone": "America/Chicago",
          "home_briefing_enabled": True, "guest_ops_enabled": True}


def status_with_briefing():
    today = NOW.date()
    synced = NOW.isoformat()
    return {"backend_available": True, "snapshot": {"demo": False, "flights": []},
            "presence": {"chris": {"state": "home", "stale": False, "last_seen": synced}},
            "home_briefing": {
                "headlines": {"synced": synced, "rows": [
                    {"date": today.isoformat(), "title": f"Headline {i}", "source": "Bureau"}
                    for i in range(5)]},
                "events": {"synced": synced, "rows": [
                    {"date": today.isoformat(), "title": "Library open house", "source": "Library"}]},
                "guest_ops": {"synced": synced, "rows": [
                    {"date": (today + timedelta(days=i)).isoformat(), "arrivals": 2 + i, "departures": i}
                    for i in range(4)]}}}


def briefing_entries(status, config, now):
    return [entry for entry in build_rotation(status, config, now) if entry["view"] in HOME_BRIEFING_VIEWS]


def text_on_frame(state, config):
    with patch("flightwall.render._text") as draw_text:
        image = render_frame(state, config)
    assert image.size == (512, 256)
    return [str(call.args[2]).upper() for call in draw_text.call_args_list]


def test_home_briefing_deck_and_individually_projected_cards():
    status = fresh_household_status(status_with_briefing(), CONFIG, NOW)
    entries = briefing_entries(status, CONFIG, NOW)
    assert [entry["key"] for entry in entries] == [
        "home_headlines:0", "home_headlines:1", "home_headlines:2", "home_events", "guest_ops"]
    assert not ANIMATED_VIEWS.intersection(HOME_BRIEFING_VIEWS)
    assert "home_briefing" not in entry_render_state(status, CONFIG, {"view": "home"}, NOW)
    for entry in entries:
        state = entry_render_state(status, CONFIG, entry, NOW)
        assert set(state["home_briefing"]) == {HOME_BRIEFING_VIEWS[entry["view"]][0]}
    headline = text_on_frame(entry_render_state(status, CONFIG, entries[1], NOW), CONFIG)
    assert "HEADLINES" in headline and "HEADLINE 2" in headline and "HEADLINE 0" not in headline
    assert "BUREAU / 11/27" in headline
    events = text_on_frame(entry_render_state(status, CONFIG, entries[-2], NOW), CONFIG)
    assert "LOCAL EVENTS" in events and "LIBRARY OPEN HOUSE" in events
    ops = text_on_frame(entry_render_state(status, CONFIG, entries[-1], NOW), CONFIG)
    assert "GUEST OPERATIONS" in ops and "TODAY 11/27" in ops and "MON 11/30" in ops
    assert "ARRIVALS" in ops and "DEPARTURES" in ops and "0" in ops


@pytest.mark.parametrize("reason", ["away", "unknown", "phone_expiry", "guest", "offline", "demo", "disabled"])
def test_home_briefing_exits_paused_deck_and_cannot_redraw_filtered_data(reason):
    status, config, now = status_with_briefing(), CONFIG.copy(), NOW
    entries = build_rotation(status, config, now)
    index = next(i for i, entry in enumerate(entries) if entry["view"] == "guest_ops")
    cursor = RotationCursor(entries, 0)
    cursor.select(entries, index, 1)
    retained = entry_render_state(status, config, entries[index], now)
    if reason in {"away", "unknown"}:
        status["presence"]["chris"]["state"] = reason
    elif reason == "phone_expiry":
        status["presence"]["chris"]["last_seen"] = (now - timedelta(minutes=config["presence_ttl_minutes"])).isoformat()
    elif reason == "guest":
        config["guest_mode"] = True
    elif reason == "offline":
        status["backend_available"] = False
    elif reason == "demo":
        status["snapshot"]["demo"] = True
    else:
        config.update(home_briefing_enabled=False, guest_ops_enabled=False)
    assert not briefing_entries(status, config, now)
    safe = fresh_household_status(status, config, now)
    assert "home_briefing" not in safe
    assert cursor.tick(build_rotation(safe, config, now), 2, automatic=False)["view"] not in HOME_BRIEFING_VIEWS
    retained.update(backend_available=status["backend_available"], demo=status["snapshot"]["demo"],
                    presence=status["presence"], rotation_paused=True)
    assert "GUEST OPERATIONS" not in text_on_frame(retained, config)


def test_guest_ops_has_its_own_flag_and_expires_with_its_data():
    status = status_with_briefing()
    disabled = {**CONFIG, "guest_ops_enabled": False}
    assert "guest_ops" not in fresh_household_status(status, disabled, NOW)["home_briefing"]
    status["home_briefing"]["guest_ops"]["synced"] = (NOW - timedelta(days=3)).isoformat()
    fresh = fresh_household_status(status, CONFIG, NOW)
    assert set(fresh["home_briefing"]) == {"headlines", "events"}
    assert not any(entry["view"] == "guest_ops" for entry in briefing_entries(status, CONFIG, NOW))


def test_guest_ops_does_not_carry_yesterdays_window_into_the_new_local_day():
    status = status_with_briefing()
    midnight = datetime(2026, 11, 28, 6, tzinfo=timezone.utc)
    synced = (midnight - timedelta(minutes=1)).isoformat()
    status["presence"]["chris"]["last_seen"] = synced
    for section in status["home_briefing"].values():
        section["synced"] = synced
    before = fresh_household_status(status, CONFIG, midnight - timedelta(seconds=1))
    assert "guest_ops" in before["home_briefing"]
    after = fresh_household_status(status, CONFIG, midnight)
    assert set(after["home_briefing"]) == {"headlines"}
