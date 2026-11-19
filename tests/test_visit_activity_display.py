from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from flightwall.config import DEFAULTS
from flightwall.display import (
    ANIMATED_VIEWS,
    RotationCursor,
    entry_render_state,
    fresh_household_status,
)
from flightwall.household_policy import build_rotation
from flightwall.render import render_frame


MIDNIGHT = datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
NOW = MIDNIGHT - timedelta(minutes=1)
TITLE = "TEST VISIT ACTIVITY"
CONFIG = DEFAULTS | {"demo": False, "timezone": "America/Chicago", "presence_enabled": True,
                     "guest_mode": False, "chris_agenda_enabled": False, "visit_activity_enabled": True}


def fixture():
    return {"backend_available": True, "snapshot": {"demo": False, "flights": []},
            "presence": {"chris": {"state": "home", "stale": False, "last_seen": NOW.isoformat()}},
            "visit_activity": {"title": TITLE, "idea": "Stack paper cups and knock them down.",
                               "bring": "Paper cups", "visit_date": "2026-11-02", "synced": NOW.isoformat()}}


def test_activity_renders_only_in_its_own_slot_even_without_agenda_or_kids_presence():
    status = fresh_household_status(fixture(), CONFIG, NOW)
    entries = build_rotation(status, CONFIG, NOW)
    visit, = [entry for entry in entries if entry["view"] == "visit_activity"]
    assert "item" not in visit
    assert visit["view"] not in ANIMATED_VIEWS
    home = next(entry for entry in entries if entry["view"] == "home")
    assert entry_render_state(status, CONFIG, home, NOW)["visit_activity"] is None
    with patch("flightwall.render._text") as drawing:
        frame = render_frame(entry_render_state(status, CONFIG, visit, NOW), CONFIG)
    labels = [call.args[2] for call in drawing.call_args_list]
    assert frame.size == (512, 256)
    assert TITLE in labels
    assert "TOMORROW / Mon 11/02" in labels
    assert "STACK PAPER CUPS AND KNOCK THEM DOWN." in labels
    assert "PAPER CUPS" in labels


@pytest.mark.parametrize("cause", ["midnight", "stale_sync", "stale_home", "away", "guest", "offline", "opt_out"])
def test_activity_is_removed_from_a_paused_rotation_and_stale_frame(cause):
    config, status, now = CONFIG.copy(), fixture(), NOW
    entries = build_rotation(fresh_household_status(status, config, now), config, now)
    visit_index = next(i for i, entry in enumerate(entries) if entry["view"] == "visit_activity")
    cursor = RotationCursor(entries, 0)
    cursor.select(entries, visit_index, 1)
    paused = entry_render_state(status, config, entries[visit_index], now) | {"rotation_paused": True}
    if cause == "midnight":
        now = MIDNIGHT
    elif cause == "stale_sync":
        status["visit_activity"]["synced"] = (now - timedelta(hours=1)).isoformat()
    elif cause == "stale_home":
        status["presence"]["chris"]["last_seen"] = (now - timedelta(days=1)).isoformat()
    elif cause == "away":
        status["presence"]["chris"]["state"] = "away"
    elif cause == "guest":
        config["guest_mode"] = True
    elif cause == "offline":
        status["backend_available"] = False
    else:
        config["visit_activity_enabled"] = False

    safe = fresh_household_status(status, config, now)
    assert "visit_activity" not in safe
    assert cursor.tick(build_rotation(safe, config, now), 2, automatic=False)["view"] != "visit_activity"

    # Exercise a retained frame with the actual row still present. The final
    # draw must recheck the clock and room before writing any private pixels.
    paused.update(status, now=now.isoformat())
    with patch("flightwall.render._text") as drawing:
        render_frame(paused, config)
    assert TITLE not in [call.args[2] for call in drawing.call_args_list]
