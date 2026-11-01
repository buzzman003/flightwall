"""Selection binds a spoken number to one dated board generation, not a title."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from flightwall.personal import PersonalStore
from integrations.alexa import lambda_function as alexa


NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
GENERATION = "a1" * 12
SKILL = "amzn1.ask.skill.synthetic-dinner-options"


def event(name="SelectDinnerOptionIntent", number="3", previous=None, request_id="synthetic-select"):
    slots = {} if number is None else {"OptionNumber": {"value": number}}
    return {"context": {"System": {"application": {"applicationId": SKILL}}},
            "session": {"application": {"applicationId": SKILL},
                        "attributes": previous.get("sessionAttributes", {}) if previous else {}},
            "request": {"type": "IntentRequest", "locale": "en-US", "requestId": request_id,
                        "intent": {"name": name, "slots": slots}}}


def speech(response):
    return response["response"]["outputSpeech"]["text"]


class Board:
    def __init__(self, store):
        self.store, self.calls = store, []
        self.context_value = store.voice_context() | {"dinner_options_date": "2026-11-01",
                                                     "dinner_options_generation": GENERATION}
        self.fail_after_save = False

    def context(self):
        return copy.deepcopy(self.context_value)

    def select_dinner_option(self, payload):
        self.calls.append(copy.deepcopy(payload))
        # This fixture represents the server-owned mapping. The adapter must
        # read back this returned meal and send no caller-supplied title.
        result = self.store.create({"kind": "dinner", "title": "Fictional board-selected curry",
                                    "date": payload["date"], "request_id": payload["request_id"]})
        if self.fail_after_save:
            self.fail_after_save = False
            raise alexa.BoardError()
        return result


@pytest.fixture
def board(tmp_path):
    config = {"timezone": "America/Chicago", "dinner_start": "14:30", "dinner_end": "19:00",
              "kids_filter_enabled": True}
    store = PersonalStore(tmp_path, lambda: config)
    with patch("flightwall.personal._now", return_value=NOW), patch.object(alexa, "_now", return_value=NOW), \
            patch.dict(os.environ, {"ALEXA_SKILL_ID": SKILL}):
        result = Board(store)
        with patch.object(alexa, "BoardClient", return_value=result):
            yield result


def test_option_select_sends_only_pinned_identity_and_reads_actual_server_title(board):
    response = alexa.lambda_handler(event(), None)
    assert board.calls == [{"option": 3, "date": "2026-11-01", "generation": GENERATION,
                            "request_id": "synthetic-select"}]
    assert "Fictional board-selected curry" in speech(response)
    assert "Kids are Home" in speech(response)
    assert "Anything else?" in speech(response)
    assert response["sessionAttributes"] == {}
    assert response["response"]["shouldEndSession"] is False


def test_uncertain_selection_retry_keeps_original_date_generation_option_and_request(board):
    board.fail_after_save = True
    first = alexa.lambda_handler(event(), None)
    assert "could not confirm" in speech(first)
    assert len(board.store.list_items(NOW)) == 1
    # A regenerated set or midnight cannot silently retarget this save.
    board.context_value.update(dinner_options_date="2026-11-02", dinner_options_generation="b2" * 12)
    with patch.object(alexa, "_now", return_value=NOW + timedelta(days=1)):
        changed = alexa.lambda_handler(event(number="2", previous=first, request_id="different-turn"), None)
        assert "unchanged dinner option" in speech(changed)
        assert len(board.calls) == 1
        retried = alexa.lambda_handler(event("AMAZON.YesIntent", previous=first, request_id="retry-turn"), None)
    assert board.calls[0] == board.calls[1]
    assert len(board.store.list_items(NOW)) == 1
    assert "Already saved" in speech(retried)
    assert "not moved it to tomorrow" in speech(retried)


def test_unknown_option_reasks_without_saving_and_pins_generation_at_first_request(board):
    for value in (None, "0", "5", "-1", "1.0", "three", True):
        response = alexa.lambda_handler(event(number=value), None)
        assert "Which dinner option?" in speech(response)
        assert not board.calls
    first = alexa.lambda_handler(event(number=None), None)
    board.context_value["dinner_options_generation"] = "c3" * 12
    alexa.lambda_handler(event(number="1", previous=first, request_id="number-turn"), None)
    assert board.calls[0]["generation"] == GENERATION
    assert board.calls[0]["request_id"] == "synthetic-select"


@pytest.mark.parametrize("change", [
    {"dinner_options_date": None, "dinner_options_generation": None},
    {"dinner_options_date": "2026-10-31"},
    {"dinner_options_generation": "invalid"},
])
def test_unready_stale_or_invalid_generation_does_not_save(board, change):
    board.context_value.update(change)
    response = alexa.lambda_handler(event(), None)
    assert "not ready" in speech(response)
    assert not board.calls


def test_server_rejects_stale_generation_without_false_saved_message(board):
    message = "Those dinner ideas changed. Check today's options and try again."
    with patch.object(board, "select_dinner_option", side_effect=alexa.BoardError(retryable=False,
                                                                               public_message=message)):
        response = alexa.lambda_handler(event(), None)
    assert message in speech(response) and "Saved" not in speech(response)


class Response(io.BytesIO):
    status = 200

    def __init__(self, result, path):
        super().__init__(json.dumps(result).encode())
        self.path = path

    def geturl(self):
        return "https://board.example" + self.path


def test_dedicated_route_is_bounded_and_credential_scope_unchanged():
    environment = {"FLIGHTWALL_BASE_URL": "https://board.example",
                   "FLIGHTWALL_ALEXA_TOKEN": "synthetic-dedicated-voice-credential"}
    payload = {"option": 3, "date": "2026-11-01", "generation": GENERATION, "request_id": "synthetic-select"}
    with patch.dict(os.environ, environment), patch.object(alexa, "build_opener") as opener:
        opener.return_value.open.return_value = Response({"created": True, "item": {}}, "/api/v1/dinner-options/select")
        alexa.BoardClient().select_dinner_option(payload)
        args, kwargs = opener.return_value.open.call_args
        assert args[0].full_url == "https://board.example/api/v1/dinner-options/select"
        assert json.loads(args[0].data) == payload
        assert args[0].get_header("Authorization") == "Bearer " + environment["FLIGHTWALL_ALEXA_TOKEN"]
        assert kwargs["timeout"] == alexa.HTTP_TIMEOUT_SECONDS


@pytest.mark.parametrize("option_context,accepted", [
    ({}, True),
    ({"dinner_options_date": None, "dinner_options_generation": None}, True),
    ({"dinner_options_date": "2026-11-01", "dinner_options_generation": GENERATION}, True),
    ({"dinner_options_date": "2026-11-01"}, False),
    ({"dinner_options_date": "2026-11-01", "dinner_options_generation": "x" * 25}, False),
])
def test_voice_context_accepts_only_complete_bounded_option_identity(option_context, accepted):
    result = {"timezone": "America/Chicago", "appointment_duration_minutes": 60,
              "dinner_start": "14:30", "dinner_end": "19:00", **option_context}
    with patch.dict(os.environ, {"FLIGHTWALL_BASE_URL": "https://board.example",
                               "FLIGHTWALL_ALEXA_TOKEN": "synthetic-dedicated-voice-credential"}), \
            patch.object(alexa.BoardClient, "_request", return_value=result):
        if accepted:
            context = alexa.BoardClient().context()
            assert ("dinner_options_date" in context) == bool(option_context.get("dinner_options_date"))
        else:
            with pytest.raises(alexa.BoardError):
                alexa.BoardClient().context()


def test_selection_model_uses_bounded_number_slot_and_clear_phrase():
    model_path = Path(__file__).resolve().parents[1] / "integrations/alexa/interaction-model-en-US.json"
    intents = json.loads(model_path.read_text())["interactionModel"]["languageModel"]["intents"]
    intent = next(row for row in intents if row["name"] == "SelectDinnerOptionIntent")
    assert intent["slots"] == [{"name": "OptionNumber", "type": "AMAZON.NUMBER"}]
    assert "select dinner option {OptionNumber}" in intent["samples"]
