"""Real private menu storage drives the TV deck without provider requests."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from flightwall.backend import Runtime
from flightwall.config import private_json
from flightwall.display import entry_render_state, fresh_household_status
from flightwall.household_policy import build_rotation

NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)  # Before the 14:30 CST meal window.
ZONE = ZoneInfo("America/Chicago")


def ready_document(now=NOW):
    stream = BytesIO()
    Image.new("RGB", (256, 256), (30, 90, 140)).save(stream, format="PNG")
    thumbnail = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
    return {"schema": 1, "date": now.astimezone(ZONE).date().isoformat(), "generation": "b" * 24,
            "attempted_at": (now - timedelta(minutes=2)).isoformat(),
            "generated_at": (now - timedelta(minutes=1)).isoformat(), "status": "ready",
            "options": [{"number": number, "title": f"Fictional idea {number}", "sides": "Fictional peas",
                         "minutes": 30 if number < 4 else 55, "servings": 8,
                         "ingredients": ["PRIVATE RECIPE INGREDIENT SENTINEL"],
                         "steps": ["PRIVATE RECIPE METHOD SENTINEL"], "image_data": thumbnail}
                        for number in range(1, 5)]}


@pytest.fixture
def household(tmp_path):
    runtime = Runtime(tmp_path / "state", tmp_path / "install")
    runtime.config.update({"demo": False, "timezone": ZONE.key, "kids_filter_enabled": True,
                           "personal_enabled": True, "dinner_options_enabled": True,
                           "dinner_options_preferences": "PRIVATE PREFERENCE SENTINEL",
                           "dinner_options_monthly_limit_usd": 0})
    runtime.snapshot = lambda: {"demo": False, "flights": [], "last_sync": NOW.isoformat()}
    def household_status(now=None):
        now = now or NOW
        return {"kids": {"state": "home", "stale": False, "last_sync": now.isoformat(),
                         "until": (now + timedelta(hours=8)).isoformat()}, "custody_outlook": {}}
    document = ready_document()
    private_json(runtime.dinner_options.path, document)
    with patch.object(runtime.household, "status", side_effect=household_status), \
            patch("flightwall.personal._now", return_value=NOW), \
            patch("flightwall.dinner_options.request_json") as provider:
        yield runtime, document
        provider.assert_not_called()
    runtime.stop()


def views(status, now=NOW):
    return [row["view"] for row in build_rotation(status, status["config"], now)]


def test_actual_public_gui_config_carries_opt_in_without_preferences_or_billing(household):
    runtime, document = household
    data = runtime.display_status(NOW)
    assert data["config"]["dinner_options_enabled"] is True
    assert not {"dinner_options_preferences", "dinner_options_monthly_limit_usd", "openai_key"}.intersection(data["config"])
    assert "PRIVATE PREFERENCE" not in json.dumps(data["config"])
    assert data["dinner_options"]["status"] == "ready" and data["dinner_today_selected"] is False
    assert "dinner_options" in views(data)
    assert not runtime.personal.path.exists()  # A missing core file means a fresh empty store.


def test_saved_today_dinner_hides_morning_ideas_even_before_it_is_visible(household):
    runtime, document = household
    item = runtime.create_personal_item({"kind": "dinner", "title": "Fictional chosen meal", "date": "2026-11-02",
                                         "sides": "Fictional rice", "ready_time": "18:45", "request_id": "chosen-morning"})["item"]
    assert runtime.personal.list_items(NOW) == [item]
    data = runtime.display_status(NOW)
    assert data["personal_items"] == []
    assert data["dinner_today_selected"] is True and "dinner_options" not in views(data)
    # Cancelling the selected meal restores the current known generation.
    runtime.cancel_personal_item({"id": item["id"]})
    data = runtime.display_status(NOW)
    assert data["dinner_today_selected"] is False and "dinner_options" in views(data)


def test_tomorrows_saved_menu_does_not_suppress_todays_unselected_ideas(household):
    runtime, document = household
    item = runtime.create_personal_item({"kind": "dinner", "title": "Fictional tomorrow meal", "date": "2026-11-03"})["item"]
    data = runtime.display_status(NOW)
    assert data["dinner_today_selected"] is False and data["personal_items"] == []
    assert "dinner_options" in views(data)
    afternoon = NOW.replace(hour=21)
    data = runtime.display_status(afternoon)
    assert [row["id"] for row in data["personal_items"]] == [item["id"]]
    assert "dinner_options" in views(data, afternoon)


@pytest.mark.parametrize("damage", ["broken", "directory"])
def test_unreadable_core_store_suppresses_ideas_instead_of_claiming_no_dinner(household, damage):
    runtime, document = household
    if damage == "directory":
        runtime.personal.path.mkdir()
    else:
        runtime.personal.path.write_text("PRIVATE invalid JSON")
    data = runtime.display_status(NOW)
    assert data["personal_error"] and "PRIVATE" not in data["personal_error"]
    assert data["dinner_today_selected"] is True and "dinner_options" not in views(data)


@pytest.mark.parametrize("damage", ["missing", "broken", "deep"])
def test_missing_or_corrupt_optional_ideas_cache_does_not_break_the_rest_of_the_board(household, damage):
    runtime, document = household
    if damage == "missing":
        runtime.dinner_options.path.unlink()
    else:
        runtime.dinner_options.path.write_text("[" * 2000 + "]" * 2000 if damage == "deep" else "PRIVATE invalid ideas")
    data = runtime.display_status(NOW)
    assert data["dinner_options"]["status"] != "ready" and "dinner_options" not in views(data)
    assert data["personal_error"] is None and "PRIVATE" not in json.dumps(data)
    assert "snapshot" in data and "weather" in data


def test_broken_optional_sidecar_still_counts_the_actual_selected_core_menu(household):
    runtime, document = household
    item = runtime.create_personal_item({"kind": "dinner", "title": "Fictional selected menu",
                                         "sides": "Fictional optional side", "ready_time": "18:45"})["item"]
    runtime.personal.details_path.write_text("PRIVATE invalid details")
    data = runtime.display_status(NOW)
    assert data["personal_error"] is None and data["dinner_today_selected"] is True
    assert "dinner_options" not in views(data)
    assert runtime.personal.list_items(NOW)[0]["id"] == item["id"]


def test_maintenance_removes_photos_recipes_and_preferences_while_owner_display_keeps_photos(household):
    runtime, document = household
    tv = runtime.display_status(NOW)
    assert all(row["image_data"].startswith("data:image/png;base64,") for row in tv["dinner_options"]["options"])
    assert "PRIVATE RECIPE" not in json.dumps(tv)
    recipes = runtime.dinner_options.status(NOW, include_recipes=True)
    assert "PRIVATE RECIPE" in json.dumps(recipes)
    maintenance = runtime.status(NOW)
    assert maintenance["dinner_options"] == {"status": "ready", "date": document["date"], "option_count": 4}
    encoded = json.dumps(maintenance)
    for private in ("PRIVATE RECIPE", "PRIVATE PREFERENCE", "image_data", "data:image/", "ingredients", "steps"):
        assert private not in encoded
    assert runtime.display_status(NOW)["dinner_options"] == tv["dinner_options"]


def test_global_personal_off_and_cached_backend_outage_both_remove_ideas(household):
    runtime, document = household
    data = runtime.display_status(NOW)
    entry = next(row for row in build_rotation(data, data["config"], NOW) if row["view"] == "dinner_options")
    state = entry_render_state(data, data["config"], entry, NOW)
    assert state["kids"]["state"] == "home"
    offline = fresh_household_status(data, data["config"], NOW, backend_available=False)
    assert "dinner_options" not in views(offline)
    stale = entry_render_state(offline, data["config"], entry, NOW)
    assert stale["backend_available"] is False and stale["kids"]["state"] == "unknown"
    runtime.config.update({"personal_enabled": False})
    hidden = runtime.display_status(NOW)
    assert hidden["dinner_options"]["status"] == "off" and "dinner_options" not in views(hidden)
