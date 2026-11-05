from contextlib import nullcontext
from datetime import timedelta
import json
from unittest.mock import patch

from PIL import Image, ImageDraw

from flightwall.brief_focus import BriefFocus, _parse
from flightwall.display import entry_render_state
from flightwall.flight_brief_view import render_brief
from flightwall.flight_context import build_flight_context
from flightwall.household_policy import build_rotation
from test_flight_context import CONFIG, NOW, state


def _response(choice):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps({"focus": choice})}]}]}


def test_optional_selection_sends_only_category_codes_and_reserves_before_request(tmp_path):
    config = {"brief_focus_enabled": False, "demo": False, "guest_mode": False, "flight_cards_visible": True}
    focus = BriefFocus(tmp_path, lambda: config, nullcontext, lambda: "example-key")
    day = "2026-11-04"
    categories = ["arrival_change", "leave_time", "DL123 ATL VPS PRIVATE"]

    def request(path, key, payload):
        assert path == "/v1/responses" and key == "example-key"
        assert payload["store"] is False
        assert json.loads(payload["input"]) == {"available_categories": ["arrival_change", "leave_time"]}
        assert "DL123" not in json.dumps(payload)
        assert json.loads(focus.path.read_text())["categories"] == [["arrival_change", "leave_time"]]
        return _response("leave_time")

    with patch("flightwall.brief_focus.request_json", side_effect=request) as call:
        focus.poll(categories, day)
        call.assert_not_called()
        config["brief_focus_enabled"] = True
        focus.poll(categories, day)
        assert focus.selection(categories, day) == "leave_time"
        assert focus.selection(["arrival_change", "weather"], day) is None
        restarted = BriefFocus(tmp_path, lambda: config, nullcontext, lambda: "example-key")
        restarted.poll(categories, day)
        call.assert_called_once()
        config["guest_mode"] = True
        assert focus.selection(categories, day) is None


def test_request_cap_is_shared_across_instances_and_failures_are_charged(tmp_path):
    config = {"brief_focus_enabled": True, "demo": False, "guest_mode": False, "flight_cards_visible": True}
    day = "2026-11-04"
    candidates = [("leave_time", choice) for choice in ("arrival_change", "departure_change", "weather", "airport_delay")]
    candidates.append(("weather", "airport_delay"))
    with patch("flightwall.brief_focus.request_json", side_effect=TimeoutError) as call:
        for categories in candidates:
            focus = BriefFocus(tmp_path, lambda: config, nullcontext, lambda: "example-key")
            focus.poll(categories, day)
        assert call.call_count == 4
        assert focus.selection(candidates[0], day) is None
        focus.poll(candidates[0], "2026-11-05")
        assert call.call_count == 5
        focus.path.write_text("invalid JSON")
        focus.poll(candidates[1], "2026-11-05")
        assert call.call_count == 5


def test_parse_rejects_refusal_and_unavailable_choices():
    codes = ("weather", "airport_delay")
    assert _parse(_response("weather"), codes) == "weather"
    assert _parse(_response("leave_time"), codes) is None
    assert _parse(_response("weather") | {"status": "incomplete"}, codes) is None
    response = _response("weather")
    response["output"][0]["content"][0]["type"] = "refusal"
    assert _parse(response, codes) is None


def test_rotation_renders_verified_sources_and_preserves_critical_row_priority():
    config = CONFIG | {"timezone": "America/Chicago", "guest_mode": False}
    status = state(estimated_in="2026-11-04T23:36:00Z", scheduled_in="2026-11-04T23:10:00Z")
    status["flight_context"] = build_flight_context(status, config, NOW)
    status["brief_focus"] = "arrival_change"
    entry = next(entry for entry in build_rotation(status, config, NOW) if entry["view"] == "flight_brief")
    rendered = entry_render_state(status, config, entry, NOW)
    draw = ImageDraw.Draw(Image.new("RGB", (512, 256)))
    with patch("flightwall.render._text") as text:
        render_brief(draw, rendered, config, NOW)
    labels = [call.args[2] for call in text.call_args_list]
    positions = {call.args[2]: call.args[1][1] for call in text.call_args_list}
    assert positions["FLIGHT BRIEF"] + 7 <= positions["DL123  ATL > VPS"] < 55
    assert labels.index("VPS GROUND STOP") < labels.index("GATE IN / 26 MIN LATE")
    assert any("FLIGHTAWARE ESTIMATED" in label and "5:36 PM CST" in label for label in labels)
    for private_config in (config | {"guest_mode": True}, config | {"flight_cards_visible": False}):
        assert "flight_brief" not in {slot["view"] for slot in build_rotation(status, private_config, NOW)}
    outage = entry_render_state(status | {"backend_available": False}, config, entry, NOW)
    assert outage["flight_context"] is None
    stale = entry_render_state(status, config, entry, NOW + timedelta(minutes=16))
    assert stale["flight_context"]["provider"] is None
    assert stale["flight_context"]["airport_advisories"] == []
