"""Displayed-title choices cross the scoped HTTP bridge without provider calls."""
from datetime import datetime, timezone
import http.client
import json

from flightwall.config import private_json
from flightwall.dinner_options import DinnerOptions
from flightwall.personal import PersonalStore
from flightwall.remote import RemoteServer


NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
GENERATION = "d4" * 12


def test_title_choice_uses_actual_recipe_and_keeps_voice_scope_and_shape(tmp_path, monkeypatch):
    config = {"public_url": "https://board.example", "control_port": 0,
              "timezone": "America/Chicago", "alexa_enabled": True,
              "demo": False, "personal_enabled": True, "dinner_options_enabled": True,
              "dinner_options_monthly_limit_usd": 3, "dinner_options_servings": 8,
              "dinner_options_max_minutes": 60, "dinner_options_preferences": "",
              "dinner_start": "14:30", "dinner_end": "19:00"}
    personal = PersonalStore(tmp_path, lambda: config)
    feed = DinnerOptions(tmp_path, lambda: config, personal)
    rows = [{"number": index, "title": f"Fictional dinner {index}", "sides": f"Fictional side {index}",
             "minutes": 30 if index < 4 else 55, "servings": 8,
             "ingredients": ["8 fictional servings"], "steps": ["Fictional cooking step"],
             "image_data": None} for index in range(1, 5)]
    private_json(feed.path, {"schema": 1, "date": "2026-11-01", "generation": GENERATION,
                             "attempted_at": NOW.isoformat(), "generated_at": NOW.isoformat(),
                             "status": "ready", "options": rows})
    monkeypatch.setattr("flightwall.personal._now", lambda: NOW)
    def no_provider(*args, **kwargs):
        raise AssertionError("Choosing an existing meal must never call a paid provider")
    monkeypatch.setattr("flightwall.dinner_options.request_json", no_provider)
    calls = []
    def select(payload):
        calls.append(payload)
        return feed.select(payload, NOW)
    server = RemoteServer(tmp_path, lambda: config,
                          {"select_dinner_option": select, "get_voice_context": lambda: feed.voice_context(NOW)}).start()
    token = server.alexa_token_path.read_text().strip()
    def request(path, payload=None, authorization=token):
        connection = http.client.HTTPConnection(*server.address, timeout=5)
        try:
            headers = {"Authorization": "Bearer " + authorization}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request("POST" if payload is not None else "GET", path,
                               json.dumps(payload) if payload is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        for number in range(1, 5):
            payload = {"title": f" FICTIONAL--DINNER {number}! ", "date": "2026-11-01",
                       "generation": GENERATION, "request_id": f"http-title-{number}"}
            code, saved = request("/api/v1/dinner-options/select", payload)
            assert code == 200 and saved["item"]["title"] == rows[number - 1]["title"]
            assert saved["item"]["sides"] == rows[number - 1]["sides"]
        items = personal.list_items(NOW)
        assert len(items) == 1 and items[0]["title"] == rows[3]["title"]
        count = len(calls)
        for malformed in (payload | {"option": 4}, payload | {"ingredients": ["not allowed"]},
                          {key: value for key, value in payload.items() if key != "title"}):
            assert request("/api/v1/dinner-options/select", malformed)[0] == 400
        assert len(calls) == count  # Bridge rejects these shapes before invoking storage.
        assert request("/api/v1/dinner-options/select", payload | {"title": "Not a displayed meal"})[0] == 400
        assert request("/api/v1/dinner-options/select", payload | {"generation": "e5" * 12})[0] == 400
        assert personal.list_items(NOW) == items
        assert request("/api/v1/dinner-options/select", payload, "not-authorized")[0] == 401
        assert request("/api/v1/status")[0] == 401
        code, context = request("/api/v1/voice-context")
        assert code == 200 and context == {"dinner_options_date": "2026-11-01",
                                            "dinner_options_generation": GENERATION}
        config["alexa_enabled"] = False
        assert request("/api/v1/dinner-options/select", payload)[0] == 503
        assert personal.list_items(NOW) == items
    finally:
        server.stop()
