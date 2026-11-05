"""Private synthetic meal generation: no provider or household data required."""
import base64
import copy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import threading
from unittest.mock import Mock

from PIL import Image, PngImagePlugin
import pytest

from flightwall import dinner_options as meals
from flightwall.config import private_json
from flightwall.personal import PersonalError, PersonalStore


NOW = datetime(2026, 11, 1, 16, tzinfo=timezone.utc)  # 10 AM after Central DST change.
KEY = "sk-" + "fictional_only_" * 3


def recipes():
    return [{"number": n, "title": f"Synthetic meal {n}", "sides": "Rice" if n != 2 else "",
             "minutes": 30 if n < 4 else 55, "servings": 8,
             "ingredients": ["8 fictional portions"], "steps": ["Cook ingredients safely."]}
            for n in range(1, 5)]


def response(rows=None):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": json.dumps({"options": recipes() if rows is None else rows})}]}]}


def photo():
    image = Image.new("RGB", (512, 512))
    for box, color in zip(((0, 0, 256, 256), (256, 0, 512, 256), (0, 256, 256, 512), (256, 256, 512, 512)),
                          ("red", "green", "blue", "yellow")):
        image.paste(color, box)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("PrivateExample", "must not survive sanitation")
    out = BytesIO()
    image.save(out, "PNG", pnginfo=metadata)
    return {"data": [{"b64_json": base64.b64encode(out.getvalue()).decode()}]}


def kids(now=NOW):
    return {"state": "home", "stale": False, "last_sync": now.isoformat(),
            "until": (now + timedelta(days=2)).isoformat()}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = {"demo": False, "timezone": "America/Chicago", "dinner_options_enabled": True,
              "personal_enabled": True, "dinner_options_monthly_limit_usd": 3,
              "dinner_options_servings": 8, "dinner_options_max_minutes": 60,
              "dinner_options_preferences": "Easy varied meals; optional spice.", "kids_stale_minutes": 30,
              "dinner_start": "14:30", "dinner_end": "19:00"}
    data = tmp_path / "data"
    data.mkdir()
    (data / "openai.key").write_text(KEY, encoding="ascii")
    personal = PersonalStore(data, lambda: config)
    feed = meals.DinnerOptions(data, lambda: config, personal)
    calls = Mock(side_effect=lambda path, *_: response() if path == "/v1/responses" else photo())
    monkeypatch.setattr(meals, "request_json", calls)
    monkeypatch.setattr(meals.time, "monotonic", lambda: 0)
    monkeypatch.setattr("flightwall.personal._now", lambda: NOW)
    return feed, config, calls, personal


def test_ready_has_four_sanitized_images_safe_fields_and_private_recipe_detail(setup):
    feed, config, calls, _ = setup
    result = feed.poll(kids(), NOW)
    assert result["status"] == "ready" and len(result["generation"]) == 24
    assert len(result["options"]) == 4
    colors = [(255, 0, 0), (0, 128, 0), (0, 0, 255), (255, 255, 0)]
    for option, color in zip(result["options"], colors):
        assert set(option) == {"number", "title", "sides", "minutes", "image_data"}
        with Image.open(BytesIO(base64.b64decode(option["image_data"].split(",")[1]))) as image:
            assert image.size == (256, 256) and image.mode == "RGB"
            assert image.getpixel((128, 128)) == color and not image.info
    assert feed.status(NOW, include_recipes=True)["options"][0]["servings"] == 8
    assert feed.voice_context(NOW) == {"dinner_options_date": "2026-11-01", "dinner_options_generation": result["generation"]}
    assert calls.call_count == 2
    text = calls.call_args_list[0].args[2]
    assert text["model"] == "gpt-5.4-mini-2026-03-17" and text["store"] is False and text["max_output_tokens"] == 6000
    assert text["reasoning"] == {"effort": "low"}
    assert text["text"]["format"]["strict"] is True
    assert config["dinner_options_preferences"] in text["input"]
    assert not {"kids", "calendar", "presence", "openai_enabled"}.intersection(text)
    image = calls.call_args_list[1].args[2]
    assert image["model"] == "gpt-image-2" and image["n"] == 1 and image["quality"] == "low"
    assert image["size"] == "1024x1024" and image["output_format"] == "png"
    assert "preferences" not in image["prompt"]
    usage = json.loads(feed.usage_path.read_text())
    assert usage["reserved_cents"] == 10 and len(usage["attempts"]) == 1


def test_select_projects_saved_picture_and_notifies_only_after_creating_dinner(setup):
    feed, _, calls, _ = setup
    ready = feed.poll(kids(), NOW)
    chosen = {"option": 3, "date": ready["date"], "generation": ready["generation"], "request_id": "voice-request"}
    callback = Mock()
    result = feed.select(chosen, NOW, on_selected=callback)
    assert feed.picture_for(result["item"], NOW) == ready["options"][2]["image_data"]
    assert feed.picture_for(result["item"] | {"title": "Different menu"}, NOW) is None
    feed.select(chosen, NOW, on_selected=callback)
    callback.assert_called_once()
    assert callback.call_args.args[0]["ingredients"]
    assert calls.call_count == 2  # Reusing an existing image makes no provider call.


@pytest.mark.parametrize("changed,status", [({"demo": True}, "demo"), ({"demo": 0}, "demo"),
    ({"dinner_options_enabled": False}, "off"), ({"personal_enabled": False}, "off"),
    ({"dinner_options_monthly_limit_usd": 0}, "budget_limited"),
    ({"dinner_options_monthly_limit_usd": 3.01}, "unavailable"),
    ({"dinner_options_max_minutes": 44}, "unavailable"),
    ({"dinner_options_servings": True}, "unavailable")])
def test_disabled_or_invalid_never_calls_provider(setup, changed, status):
    feed, config, calls, _ = setup
    config.update(changed)
    assert feed.poll(kids(), NOW)["status"] == status
    assert not feed.usage_path.exists()
    calls.assert_not_called()


def test_food_consent_is_independent_of_inspiration_consent(setup):
    feed, config, _, _ = setup
    config["openai_enabled"] = False
    assert feed.poll(kids(), NOW)["status"] == "ready"


@pytest.mark.parametrize("changed", [{"state": "away"}, {"stale": True}, {"last_sync": None},
    {"last_sync": (NOW - timedelta(minutes=30)).isoformat()},
    {"last_sync": (NOW + timedelta(seconds=1)).isoformat()}, {"until": NOW.isoformat()}, {"until": None}])
def test_requires_explicit_fresh_kids_home(setup, changed):
    feed, _, calls, _ = setup
    feed.poll(kids() | changed, NOW)
    calls.assert_not_called()
    assert not feed.path.exists()


@pytest.mark.parametrize("hour", [4, 19, 23])
def test_local_generation_window_is_end_exclusive(setup, hour):
    feed, _, calls, _ = setup
    now = NOW.replace(hour=hour + 6) if hour < 18 else NOW.replace(hour=hour - 18) + timedelta(days=1)
    feed.poll(kids(now), now)
    calls.assert_not_called()


def test_first_launch_status_is_read_only_and_does_not_create_directory(tmp_path):
    data = tmp_path / "absent"
    config = {"demo": True}
    feed = meals.DinnerOptions(data, lambda: config, PersonalStore(data, lambda: config))
    assert feed.status(NOW)["status"] == "demo" and not data.exists()
    config.update(demo=False, dinner_options_enabled=True, dinner_options_monthly_limit_usd=3)
    assert feed.status(NOW)["status"] == "unconfigured" and not data.exists()


def test_missing_key_does_not_reserve_or_attempt(setup):
    feed, _, calls, _ = setup
    (feed.data_dir / "openai.key").unlink()
    feed.poll(kids(), NOW)
    assert feed.status(NOW)["status"] == "unconfigured"
    assert not feed.path.exists() and not feed.usage_path.exists()
    calls.assert_not_called()


def test_failed_attempt_is_reserved_once_even_after_restart(setup):
    feed, config, calls, personal = setup
    calls.side_effect = OSError("synthetic provider failure must never be exposed")
    assert feed.poll(kids(), NOW)["status"] == "failed"
    again = meals.DinnerOptions(feed.data_dir, lambda: config, personal)
    assert again.poll(kids(), NOW)["status"] == "failed"
    assert calls.call_count == 1
    assert json.loads(feed.usage_path.read_text())["reserved_cents"] == 10
    assert "provider failure" not in json.dumps(again.status(NOW))


def test_image_failure_keeps_honest_text_options(setup):
    feed, _, calls, _ = setup
    calls.side_effect = [response(), OSError("unavailable")]
    result = feed.poll(kids(), NOW)
    assert result["status"] == "ready" and all(item["image_data"] is None for item in result["options"])


@pytest.mark.parametrize("response_value", [{"status": "incomplete", "output": []},
    {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "No"}]}]},
    {"status": "completed", "output": [{"type": "function_call"}]}])
def test_incomplete_refusal_or_tool_output_is_failed_without_image_call(setup, response_value):
    feed, _, calls, _ = setup
    calls.side_effect = None
    calls.return_value = response_value
    assert feed.poll(kids(), NOW)["status"] == "failed" and calls.call_count == 1


@pytest.mark.parametrize("mutation", [lambda rows: rows.pop(), lambda rows: rows[0].update(title="<b>Unsafe</b>"),
    lambda rows: rows[1].update(title=rows[0]["title"].upper()), lambda rows: rows[3].update(minutes=44),
    lambda rows: rows[0].update(minutes=46), lambda rows: rows[0].update(servings=7),
    lambda rows: rows[0].update(extra="not allowed"), lambda rows: rows[0].update(number=True)])
def test_model_recipe_schema_is_validated_beyond_provider_schema(mutation):
    rows = recipes()
    mutation(rows)
    with pytest.raises(ValueError):
        meals.parse_text(response(rows), 8, 60)


def test_decimal_allowance_is_reserved_before_call_and_next_day_is_limited(setup):
    feed, config, calls, _ = setup
    config["dinner_options_monthly_limit_usd"] = "0.10"
    original = calls.side_effect
    def verify(path, key, payload):
        assert json.loads(feed.usage_path.read_text())["reserved_cents"] == 10
        return original(path, key, payload)
    calls.side_effect = verify
    assert feed.poll(kids(), NOW)["status"] == "ready"
    tomorrow = NOW + timedelta(days=1)
    assert feed.poll(kids(tomorrow), tomorrow)["status"] == "budget_limited"
    assert calls.call_count == 2


def test_utc_month_rollover_resets_reservation_but_retains_daily_history(setup):
    feed, config, calls, _ = setup
    config["dinner_options_monthly_limit_usd"] = .10
    before = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)
    assert feed.poll(kids(before), before)["status"] == "ready"
    assert feed.poll(kids(), NOW)["status"] == "ready"
    usage = json.loads(feed.usage_path.read_text())
    assert usage["month"] == "2026-11" and usage["reserved_cents"] == 10 and len(usage["attempts"]) == 2
    assert calls.call_count == 4


@pytest.mark.parametrize("new_cap", [0, 3])
def test_in_flight_batch_cannot_cross_utc_reservation_month(setup, monkeypatch, new_cap):
    feed, config, calls, _ = setup
    before = datetime(2026, 11, 30, 23, 59, 59, tzinfo=timezone.utc)  # Still 6 PM Central.
    elapsed = [0]
    monkeypatch.setattr(meals.time, "monotonic", lambda: elapsed[0])
    def provider(*_):
        elapsed[0] = 2
        config["dinner_options_monthly_limit_usd"] = new_cap
        return response()
    calls.side_effect = provider
    result = feed.poll(kids(before), before)
    assert result["status"] == "failed" and calls.call_count == 1
    assert json.loads(feed.path.read_text())["status"] == "failed"
    assert json.loads(feed.usage_path.read_text())["month"] == "2026-11"


@pytest.mark.parametrize("change", [lambda value: value.update(reserved_cents=0),
    lambda value: value.update(month="2027-01"),
    lambda value: next(iter(value["attempts"].values())).update(at=(NOW + timedelta(days=1)).isoformat())])
def test_tampered_or_future_allowance_fails_closed(setup, change):
    feed, _, calls, _ = setup
    feed.poll(kids(), NOW)
    usage = json.loads(feed.usage_path.read_text())
    change(usage)
    private_json(feed.usage_path, usage)
    assert feed.status(NOW)["status"] == "unavailable"
    feed.poll(kids(), NOW)
    assert calls.call_count == 2


@pytest.mark.parametrize("changed", [{"demo": True}, {"dinner_options_enabled": False},
    {"personal_enabled": False}, {"dinner_options_monthly_limit_usd": 0}])
def test_consent_or_allowance_revoked_after_text_stops_image_and_commit(setup, changed):
    feed, config, calls, _ = setup
    def revoke(*_):
        config.update(changed)
        return response()
    calls.side_effect = revoke
    feed.poll(kids(), NOW)
    assert calls.call_count == 1
    assert json.loads(feed.path.read_text())["status"] == "failed"
    assert json.loads(feed.usage_path.read_text())["reserved_cents"] == 10


def test_key_rotation_after_text_prevents_image_and_commit(setup):
    feed, _, calls, _ = setup
    def rotate(*_):
        (feed.data_dir / "openai.key").write_text(KEY + "changed", encoding="ascii")
        return response()
    calls.side_effect = rotate
    assert feed.poll(kids(), NOW)["status"] == "failed" and calls.call_count == 1


def test_revocation_during_image_still_discards_result(setup):
    feed, config, calls, _ = setup
    def provider(path, *_):
        if path == "/v1/responses":
            return response()
        config["demo"] = True
        return photo()
    calls.side_effect = provider
    assert feed.poll(kids(), NOW)["status"] == "demo"
    assert json.loads(feed.path.read_text())["status"] == "failed"


def test_existing_actual_today_meal_prevents_generation_even_before_display_window(setup):
    feed, _, calls, personal = setup
    personal.create({"kind": "dinner", "title": "Already decided", "date": "2026-11-01"})
    feed.poll(kids(), NOW)
    calls.assert_not_called()


def test_poll_concurrency_reserves_one_batch(setup):
    feed, _, calls, _ = setup
    entered, release = threading.Event(), threading.Event()
    def provider(path, *_):
        if path == "/v1/responses":
            entered.set()
            assert release.wait(5)
            return response()
        return photo()
    calls.side_effect = provider
    thread = threading.Thread(target=lambda: feed.poll(kids(), NOW))
    thread.start()
    assert entered.wait(5)
    try:
        assert feed.poll(kids(), NOW)["status"] == "generating"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and calls.call_count == 2


def test_cached_status_is_read_only_avoids_repeat_image_validation_and_returns_copy(setup, monkeypatch):
    feed, _, _, _ = setup
    feed.poll(kids(), NOW)
    validator = Mock(wraps=meals._thumbnail)
    monkeypatch.setattr(meals, "_thumbnail", validator)
    before = {path.name: path.stat().st_mtime_ns for path in feed.data_dir.iterdir()}
    result = feed.status(NOW)
    result["options"][0]["title"] = "caller mutation"
    assert feed.status(NOW)["options"][0]["title"] == "Synthetic meal 1"
    assert {path.name: path.stat().st_mtime_ns for path in feed.data_dir.iterdir()} == before
    validator.assert_not_called()
    document = json.loads(feed.path.read_text())
    document["options"][0]["title"] = "New stored meal"
    private_json(feed.path, document)
    assert feed.status(NOW)["options"][0]["title"] == "New stored meal"
    assert validator.call_count == 4


def test_corrupted_recursive_cache_cannot_break_status_or_start_paid_call(setup):
    feed, _, calls, _ = setup
    feed.path.write_text("[" * 2000 + "0" + "]" * 2000)
    assert feed.status(NOW)["status"] == "unavailable"
    assert feed.poll(kids(), NOW)["status"] == "unavailable"
    calls.assert_not_called()


def test_recursive_provider_payload_fails_attempt_instead_of_remaining_generating(setup):
    feed, _, calls, _ = setup
    calls.side_effect = RecursionError("synthetic nested provider JSON")
    assert feed.poll(kids(), NOW)["status"] == "failed"
    assert calls.call_count == 1


def test_corrupt_cached_photo_bomb_is_a_bounded_status_failure(setup, monkeypatch):
    feed, config, _, personal = setup
    feed.poll(kids(), NOW)
    fresh = meals.DinnerOptions(feed.data_dir, lambda: config, personal)
    monkeypatch.setattr(meals.Image, "open", Mock(side_effect=Image.DecompressionBombError("synthetic oversized photo")))
    assert fresh.status(NOW)["status"] == "unavailable"


def test_select_binds_actual_saved_fields_deduplicates_and_replaces_same_day(setup):
    feed, _, calls, personal = setup
    result = feed.poll(kids(), NOW)
    payload = {"option": 3, "date": result["date"], "generation": result["generation"], "request_id": "synthetic-choice"}
    first = feed.select(payload, NOW)
    replay = feed.select(payload, NOW)
    assert first["item"]["title"] == "Synthetic meal 3" and first["item"]["sides"] == "Rice"
    assert replay["created"] is False and len(personal.list_items(NOW)) == 1
    with pytest.raises(PersonalError):
        feed.select(payload | {"option": 2}, NOW)
    second = feed.select(payload | {"option": 2, "request_id": "synthetic-new-choice"}, NOW)
    assert second["item"]["title"] == "Synthetic meal 2" and "sides" not in second["item"]
    assert len(personal.list_items(NOW)) == 1 and calls.call_count == 2


@pytest.mark.parametrize("number", range(1, 5))
def test_select_each_displayed_title_preserves_recipe_and_numeric_retry_identity(setup, number):
    feed, _, calls, personal = setup
    result = feed.poll(kids(), NOW)
    option = result["options"][number - 1]
    payload = {"title": f"  SYNTHETIC\u2014MEAL {number}!  ", "date": result["date"],
               "generation": result["generation"], "request_id": f"title-choice-{number}"}
    first = feed.select(payload, NOW)
    assert first["item"]["title"] == option["title"]
    assert first["item"].get("sides", "") == option["sides"]
    numeric = {key: value for key, value in payload.items() if key != "title"} | {"option": number}
    replay = feed.select(numeric, NOW)
    assert replay["created"] is False and replay["item"]["id"] == first["item"]["id"]
    assert len(personal.list_items(NOW)) == 1 and calls.call_count == 2


def test_title_selection_normalizes_unicode_typography_without_changing_saved_title(setup):
    feed, _, calls, _ = setup
    rows = recipes()
    rows[1]["title"] = "Cr\u00e8me-fra\u00eeche chicken"
    rows[1]["sides"] = "Roasted carrots"
    calls.side_effect = lambda path, *_: response(rows) if path == "/v1/responses" else photo()
    result = feed.poll(kids(), NOW)
    chosen = feed.select({"title": "  CR\u00c8ME  FRA\u00ceCHE, CHICKEN! ", "date": result["date"],
                          "generation": result["generation"], "request_id": "unicode-title"}, NOW)
    assert chosen["item"]["title"] == rows[1]["title"]
    assert chosen["item"]["sides"] == rows[1]["sides"]


def test_ambiguous_normalized_titles_require_a_number_without_writing(setup):
    feed, _, calls, personal = setup
    rows = recipes()
    rows[0]["title"], rows[1]["title"] = "Chicken-rice", "Chicken rice"
    calls.side_effect = lambda path, *_: response(rows) if path == "/v1/responses" else photo()
    result = feed.poll(kids(), NOW)
    assert result["status"] == "ready"
    with pytest.raises(PersonalError, match="select menu one, two, three, or four"):
        feed.select({"title": "CHICKEN, RICE", "date": result["date"],
                     "generation": result["generation"], "request_id": "ambiguous-title"}, NOW)
    assert personal.list_items(NOW) == []
    assert calls.call_count == 2


@pytest.mark.parametrize("title", ["Chicken meal that is not displayed", "Synthetic meal", "", " !? ",
                                    "X" * 161, "Synthetic\nmeal 1", "<Synthetic meal 1>", 3, None])
def test_title_selection_refuses_unmatched_partial_or_malformed_titles(setup, title):
    feed, _, calls, personal = setup
    result = feed.poll(kids(), NOW)
    with pytest.raises(PersonalError):
        feed.select({"title": title, "date": result["date"], "generation": result["generation"],
                     "request_id": "unmatched-title"}, NOW)
    assert personal.list_items(NOW) == [] and calls.call_count == 2


@pytest.mark.parametrize("changed", [{"date": "2026-10-31"}, {"generation": "f" * 24},
                                     {"option": 2}, {"request_id": ""}])
def test_title_selection_refuses_stale_set_or_multiple_selectors_without_writing(setup, changed):
    feed, _, calls, personal = setup
    result = feed.poll(kids(), NOW)
    payload = {"title": result["options"][0]["title"], "date": result["date"],
               "generation": result["generation"], "request_id": "stale-title"}
    with pytest.raises(PersonalError):
        feed.select(payload | changed, NOW)
    assert personal.list_items(NOW) == [] and calls.call_count == 2


@pytest.mark.parametrize("changed", [{"title": "forged"}, {"generation": "f" * 24}, {"date": "2026-10-31"},
    {"option": True}, {"option": 5}, {"request_id": ""}])
def test_selection_rejects_forged_stale_or_malformed_request(setup, changed):
    feed, _, _, personal = setup
    result = feed.poll(kids(), NOW)
    payload = {"option": 1, "date": result["date"], "generation": result["generation"], "request_id": "test"}
    with pytest.raises(PersonalError):
        feed.select(payload | changed, NOW)
    assert personal.list_items(NOW) == []


def test_selection_and_voice_context_close_at_7pm(setup):
    feed, _, _, _ = setup
    result = feed.poll(kids(), NOW)
    later = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
    assert feed.voice_context(later) == {}
    with pytest.raises(PersonalError):
        feed.select({"option": 1, "date": result["date"], "generation": result["generation"], "request_id": "late"}, later)


@pytest.mark.parametrize("bad", [{"data": [{"url": "https://untrusted.example/photo"}]}, {"data": []},
    {"data": [{"b64_json": "not base64"}]}])
def test_photo_url_or_invalid_payload_is_never_fetched(bad):
    with pytest.raises(ValueError):
        meals.parse_photos(bad)


def test_http_transport_uses_fixed_url_no_proxy_or_redirect_and_bounded_read(monkeypatch):
    class Reply:
        headers = {}
        def geturl(self): return "https://api.openai.com/v1/responses"
        def read(self, limit):
            assert limit == 256001
            return b'{"status":"completed"}'
        def __enter__(self): return self
        def __exit__(self, *_): pass
    opener = Mock()
    opener.open.return_value = Reply()
    builder = Mock(return_value=opener)
    monkeypatch.setattr(meals.urllib.request, "build_opener", builder)
    assert meals.request_json("/v1/responses", KEY, {}) == {"status": "completed"}
    assert builder.call_args.args[0].proxies == {}
    assert builder.call_args.args[1].redirect_request(None) is None
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://api.openai.com/v1/responses"
    assert request.get_header("Authorization") == "Bearer " + KEY
    assert opener.open.call_args.kwargs["timeout"] == 45
    with pytest.raises(ValueError):
        meals.request_json("https://evil.invalid", KEY, {})
    Reply.geturl = lambda self: "https://evil.invalid/redirect"
    with pytest.raises(ValueError):
        meals.request_json("/v1/responses", KEY, {})
