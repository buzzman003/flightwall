"""Four fictional dinner ideas are dated and gated again for every frame."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from flightwall import display, render
from flightwall.household_policy import build_rotation, dinner_options_visible

ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)  # 09:00 CST.
COLORS = [(180, 40, 30), (30, 150, 80), (30, 80, 180), (180, 140, 30)]


def test_food_photos_bypass_led_gaps_and_recheck_expiry_and_kids_home():
    state, config = fixture()
    config.update(led_effect=True, brightness=.5, dot_gap=.5)
    image = Image.new('RGB', (512, 256))
    display.attach_dinner_photos(image, state, config, (1536, 768), NOW)
    assert len(image.info['meal_patches']) == 4
    shown = display._present_frame(image, config, (1536, 768))
    x, y = 22 * 3 + 30, 113 * 3 + 30
    assert {shown.getpixel((x + dx, y + dy)) for dx in range(9) for dy in range(9)} == {(90, 20, 15)}
    for change, instant in (({'kids': state['kids'] | {'state': 'away'}}, NOW),
                             ({}, NOW + timedelta(minutes=31)),
                             ({'backend_available': False}, NOW)):
        blocked = Image.new('RGB', (512, 256))
        display.attach_dinner_photos(blocked, state | change, config, (1536, 768), instant)
        assert 'meal_patches' not in blocked.info


def picture(color):
    stream = BytesIO()
    Image.new("RGB", (100, 61), color).save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def fixture(now=NOW):
    config = {"timezone": ZONE.key, "demo": False, "dinner_options_enabled": True,
              "dinner_options_card_visible": True, "kids_filter_enabled": True, "kids_stale_minutes": 30}
    rows = [{"number": i + 1, "title": "Fictional meal " + str(i + 1), "sides": "Fictional side",
             "minutes": 10 + i * 5, "image_data": picture(color)} for i, color in enumerate(COLORS)]
    state = {"view": "dinner_options", "now": now.isoformat(), "demo": False, "backend_available": True,
             "snapshot": {"demo": False, "flights": []}, "personal_items": [], "dinner_today_selected": False,
             "kids": {"state": "home", "stale": False, "last_sync": now.isoformat(),
                      "until": (now + timedelta(hours=12)).isoformat()},
             "household_context": {"kids_home": True},
             "dinner_options": {"status": "ready", "date": now.astimezone(ZONE).date().isoformat(),
                                "generation": "a" * 24, "generated_at": now.isoformat(), "options": rows}}
    return state, config


def frame(state, config, now=None):
    state = deepcopy(state)
    if now:
        state["now"] = now.isoformat()
    before = deepcopy(state)
    labels = []
    original = render._text
    def capture(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)
    with patch.object(render, "_text", side_effect=capture):
        image = render.render_frame(state, config)
    assert state == before
    return image, labels


def test_exact_five_am_and_seven_pm_boundaries_in_household_time_across_dst():
    for day in ("2026-03-08", "2026-11-01", "2026-11-02"):
        for clock, expected in (("04:59:59.999999", False), ("05:00:00", True),
                                ("18:59:59.999999", True), ("19:00:00", False)):
            instant = datetime.fromisoformat(day + "T" + clock).replace(tzinfo=ZONE).astimezone(timezone.utc)
            state, config = fixture(instant)
            assert dinner_options_visible(state, config, instant) is expected
            views = [row["view"] for row in build_rotation(state, config, instant)]
            assert ("dinner_options" in views) is expected


def test_four_numbered_columns_paste_only_current_sanitized_picture_pixels():
    state, config = fixture()
    before = deepcopy(state)
    image, labels = frame(state, config)
    for index, color in enumerate(COLORS):
        x = 15 + index * 124
        assert image.getpixel((x + 40, 135)) == color
        assert str(index + 1) in labels
        assert f"FICTIONAL MEAL {index + 1}" in labels
        assert f"{10 + index * 5} MIN" in labels
    assert "FOUR EASY DINNER IDEAS" in labels
    assert image.size == (512, 256) and image.mode == "RGB" and image.info == {}
    assert state == before
    assert "dinner_options" not in display.ANIMATED_VIEWS


def test_lunch_boundary_only_warms_background_without_an_alert():
    before_time = datetime.fromisoformat("2026-11-02T14:29:59.999999-06:00")
    after_time = datetime.fromisoformat("2026-11-02T14:30:00-06:00")
    state, config = fixture(before_time)
    before, labels = frame(state, config)
    after, changed = frame(state, config, after_time)
    assert before.getpixel((0, 100)) == render.BG
    assert after.getpixel((0, 100)) == (27, 16, 12)
    assert "CHOOSE TONIGHT" in changed and "FOUR EASY DINNER IDEAS" in labels
    assert not any("ALERT" in label or "WARNING" in label or "OVERDUE" in label for label in changed)


@pytest.mark.parametrize("kids", [{"state": "away", "stale": False}, {"state": "unknown", "stale": True},
                                  {"state": "home", "stale": True}, {"state": "home", "stale": False}, None])
def test_no_options_without_a_complete_fresh_kids_home_record_even_if_cached_boolean_is_true(kids):
    state, config = fixture()
    state["kids"] = kids
    assert not dinner_options_visible(state, config, NOW)
    image, labels = frame(state, config)
    assert "WHAT'S FOR DINNER?" not in labels and "HOME / TODAY" in labels
    assert not any("FICTIONAL MEAL" in label for label in labels)


def test_custody_expiry_and_sync_ttl_clear_between_polls_during_pause():
    state, config = fixture()
    state["kids"]["until"] = (NOW + timedelta(seconds=1)).isoformat()
    assert dinner_options_visible(state, config, NOW + timedelta(seconds=.999999))
    assert not dinner_options_visible(state, config, NOW + timedelta(seconds=1))
    image, labels = frame(state, config, NOW + timedelta(seconds=1))
    assert "HOME / TODAY" in labels and not any("FICTIONAL MEAL" in label for label in labels)
    state, config = fixture()
    assert dinner_options_visible(state, config, NOW + timedelta(minutes=30))
    assert not dinner_options_visible(state, config, NOW + timedelta(minutes=30, microseconds=1))
    assert not dinner_options_visible(state, config, NOW - timedelta(minutes=5, microseconds=1))


@pytest.mark.parametrize("changes", [{"backend_available": False}, {"dinner_today_selected": True},
                                     {"dinner_today_selected": None}, {"demo": True}])
def test_outage_selected_or_unknown_selection_and_demo_clear_options(changes):
    state, config = fixture()
    state.update(changes)
    assert not dinner_options_visible(state, config, NOW)
    image, labels = frame(state, config)
    assert not any("FICTIONAL MEAL" in label for label in labels)


@pytest.mark.parametrize("flag", ["personal_enabled", "dinner_options_enabled", "dinner_options_card_visible"])
def test_hiding_the_feature_or_card_cannot_be_overridden_by_cached_ready_data(flag):
    state, config = fixture()
    config[flag] = False
    assert not dinner_options_visible(state, config, NOW)
    assert "dinner_options" not in [row["view"] for row in build_rotation(state, config, NOW)]
    image, labels = frame(state, config)
    assert "HOME / TODAY" in labels and not any("FICTIONAL MEAL" in label for label in labels)


def test_only_today_ready_generation_is_valid_and_exactly_four_unique_numbers_are_required():
    original, config = fixture()
    for changes in ({"status": "unavailable"}, {"date": "2026-11-01"}, {"generation": "../../private"},
                    {"generated_at": (NOW + timedelta(seconds=1)).isoformat()},
                    {"generated_at": (NOW - timedelta(days=1)).isoformat()}, {"options": original["dinner_options"]["options"][:3]}):
        state = deepcopy(original)
        state["dinner_options"].update(changes)
        assert not dinner_options_visible(state, config, NOW)
    for changes in ({"number": True}, {"number": 2}, {"title": "x" * 81}, {"minutes": True}, {"minutes": 121},
                    {"image_data": "https://untrusted.invalid/private.png"}, {"image_path": "PRIVATE_PATH"}):
        state = deepcopy(original)
        state["dinner_options"]["options"][0].update(changes)
        assert not dinner_options_visible(state, config, NOW)
        image, labels = frame(state, config)
        assert not any("PRIVATE" in label or "FICTIONAL MEAL" in label for label in labels)


def test_optional_missing_or_invalid_png_uses_an_honest_placeholder_without_path_reads():
    state, config = fixture()
    state["dinner_options"]["options"][0].pop("image_data")
    state["dinner_options"]["options"][1]["image_data"] = "data:image/png;base64," + base64.b64encode(b"not png").decode()
    assert dinner_options_visible(state, config, NOW)
    with patch("pathlib.Path.open", side_effect=AssertionError("No image file access")):
        image, labels = frame(state, config)
    assert labels.count("PHOTO") == labels.count("UNAVAILABLE") == 2
    assert image.getpixel((15 + 2 * 124 + 40, 135)) == COLORS[2]


def test_invalid_ideas_fallback_does_not_show_a_tomorrow_meal_after_kids_expire():
    state, config = fixture(datetime.fromisoformat("2026-11-02T15:00:00-06:00"))
    start = datetime.fromisoformat("2026-11-03T14:30:00-06:00")
    end = datetime.fromisoformat("2026-11-03T19:00:00-06:00")
    state["personal_items"] = [{"id": "fictional-menu", "kind": "dinner", "title": "Fictional tomorrow private meal",
                                "timezone": ZONE.key, "start": start.isoformat(), "end": end.isoformat(),
                                "display_start": start.isoformat(), "display_end": end.isoformat()}]
    state["kids"]["until"] = (datetime.fromisoformat(state["now"]) + timedelta(seconds=1)).isoformat()
    expired = datetime.fromisoformat(state["now"]) + timedelta(seconds=1)
    image, labels = frame(state, config, expired)
    assert not any("FICTIONAL MEAL" in label or "PRIVATE MEAL" in label for label in labels)
