"""Offline voice dialogues for board timers and public flight-watch requests."""
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flightwall.timers import TimerError, TimerStore
from integrations.alexa import lambda_function as alexa


SKILL = "amzn1.ask.skill.synthetic-household-tests"
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def event(name, slots=None, previous=None, request_id="synthetic-initial"):
    return {"context": {"System": {"application": {"applicationId": SKILL}}},
            "session": {"application": {"applicationId": SKILL}, "attributes": previous or {}},
            "request": {"type": "IntentRequest", "locale": "en-US", "requestId": request_id,
                        "intent": {"name": name, "slots": {key: {"value": value} for key, value in (slots or {}).items()}}}}


def speech(response):
    return response["response"]["outputSpeech"]["text"]


class Adapter:
    def __init__(self, store):
        self.store = store
        self.starts, self.cancels, self.watches = [], [], []
        self.fail_after_timer = self.fail_after_watch = False
        self.tracking_available = False
        self.watch_requests = {}
        self.items, self.shopping_updates, self.shopping_requests = [], [], {}
        self.visible = False
        self.fail_after_shopping = False

    def start_timer(self, payload):
        self.starts.append(copy.deepcopy(payload))
        try:
            result = self.store.start(payload)
        except TimerError as exc:
            raise alexa.BoardError(retryable=False, public_message=str(exc)) from None
        if self.fail_after_timer:
            self.fail_after_timer = False
            raise alexa.BoardError()
        return result

    def list_timers(self):
        return self.store.list_timers()

    def cancel_timer(self, item_id):
        self.cancels.append(item_id)
        return self.store.cancel(item_id)

    def watch_flight(self, payload):
        self.watches.append(copy.deepcopy(payload))
        key = payload["request_id"]
        created = key not in self.watch_requests
        result = {"created": created, "watch": {"id": "a" * 24,
                  "ident": payload["airline"] + payload["flight_number"], "date": payload["date"]},
                  "tracking_available": self.tracking_available}
        self.watch_requests[key] = result
        if self.fail_after_watch:
            self.fail_after_watch = False
            raise alexa.BoardError()
        return result

    def shopping_list(self):
        return {"items": copy.deepcopy(self.items), "visible": self.visible}

    def update_shopping(self, action, payload):
        self.shopping_updates.append((action, copy.deepcopy(payload)))
        key = payload["request_id"]
        if key in self.shopping_requests:
            return {**copy.deepcopy(self.shopping_requests[key]), "changed": False}
        before = self.shopping_list()
        if action == "add" and not any(row["name"].casefold() == payload["item"].casefold() for row in self.items):
            self.items.append({"id": str(len(self.items) + 1), "name": payload["item"]})
        elif action == "remove":
            self.items = [row for row in self.items if row["name"].casefold() != payload["item"].casefold()]
        elif action == "clear":
            assert payload["confirmed"] is True
            self.items = []
        elif action == "show":
            assert payload["visible"] is True
            self.visible = True
        result = {**self.shopping_list(), "changed": self.shopping_list() != before}
        self.shopping_requests[key] = copy.deepcopy(result)
        if self.fail_after_shopping:
            self.fail_after_shopping = False
            raise alexa.BoardError()
        return result


class HouseholdVoiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.board = Adapter(TimerStore(self.folder.name))
        self.patches = [patch.dict(os.environ, {"ALEXA_SKILL_ID": SKILL}),
                        patch("flightwall.timers._now", return_value=NOW),
                        patch.object(alexa, "BoardClient", return_value=self.board)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    def call(self, name, slots=None, previous=None, request_id="synthetic-initial"):
        return alexa.lambda_handler(event(name, slots, previous and previous["sessionAttributes"], request_id), None)

    def test_launch_is_short_and_help_is_only_on_request(self):
        envelope = event("unused")
        envelope["request"]["type"] = "LaunchRequest"
        self.assertEqual(speech(alexa.lambda_handler(envelope, None)), "Flight Wall. What would you like?")
        fallback = self.call("AMAZON.FallbackIntent")
        self.assertNotIn("dinner", speech(fallback))
        help_response = self.call("AMAZON.HelpIntent")
        self.assertIn("timer", speech(help_response))
        self.assertNotIn("chicken", speech(help_response))
        dinner_help = speech(self.call("DinnerHelpIntent"))
        self.assertIn("set dinner card to chicken", dinner_help)
        self.assertLessEqual(len(dinner_help.split()), 25)
        self.assertNotIn("or", dinner_help.split())
        alexa.BoardClient.assert_not_called()

    def test_start_timer_gathers_missing_values_and_preserves_first_request_id(self):
        first = self.call("StartTimerIntent", {"TimerDuration": "PT45S"})
        self.assertIn("What should I call", speech(first))
        self.assertFalse(self.board.starts)
        saved = self.call("SetTimerLabelIntent", {"TimerLabel": "Tea"}, first, "label-turn")
        self.assertIn("45 seconds", speech(saved))
        self.assertEqual(self.board.starts, [{"label": "Tea", "duration_seconds": 45,
                                            "source": "alexa", "request_id": "synthetic-initial"}])
        self.assertEqual(len(self.board.store.list_timers()), 1)

    def test_timer_uses_existing_for_duration_followup(self):
        first = self.call("StartTimerIntent", {"TimerLabel": "Pasta"})
        saved = self.call("SetDurationIntent", {"Duration": "PT1.5M"}, first)
        self.assertIn("1 minute, 30 seconds", speech(saved))
        self.assertEqual(self.board.store.list_timers()[0]["duration_seconds"], 90)

    def test_invalid_duration_and_label_do_not_write(self):
        for duration in ("PT0S", "PT0.5S", "P2D", "P1M", "unknown"):
            response = self.call("StartTimerIntent", {"TimerLabel": "Tea", "TimerDuration": duration})
            self.assertIn("How long", speech(response))
        response = self.call("StartTimerIntent", {"TimerLabel": "x" * 81, "TimerDuration": "PT5M"})
        self.assertIn("What should I call", speech(response))
        self.assertFalse(self.board.starts)

    def test_timer_limit_is_reported_without_retry_prompt(self):
        for index in range(5):
            self.call("StartTimerIntent", {"TimerLabel": "Tea", "TimerDuration": "PT5M"}, request_id=str(index))
        response = self.call("StartTimerIntent", {"TimerLabel": "Pasta", "TimerDuration": "PT5M"})
        self.assertIn("Five timers", speech(response))
        self.assertTrue(response["response"]["shouldEndSession"])
        self.assertEqual(len(self.board.store.list_timers()), 5)

    def test_timed_out_timer_retry_does_not_restart(self):
        self.board.fail_after_timer = True
        pending = self.call("StartTimerIntent", {"TimerLabel": "Tea", "TimerDuration": "PT5M"})
        self.assertEqual(pending["sessionAttributes"]["pending"]["phase"], "retry")
        saved = self.call("AMAZON.YesIntent", previous=pending, request_id="retry-turn")
        self.assertIn("already running", speech(saved))
        self.assertEqual(self.board.starts[0], self.board.starts[1])
        self.assertEqual(len(self.board.store.list_timers()), 1)

    def test_list_and_cancel_are_scoped_and_duplicate_labels_are_not_guessed(self):
        for index in range(2):
            self.call("StartTimerIntent", {"TimerLabel": "Tea", "TimerDuration": "PT5M"}, request_id=str(index))
        self.assertIn("Timer 2, Tea", speech(self.call("ListTimersIntent")))
        ambiguous = self.call("CancelTimerIntent", {"TimerLabel": "Tea"})
        self.assertIn("More than one", speech(ambiguous))
        self.assertFalse(self.board.cancels)
        expected = self.board.store.list_timers()[1]["id"]
        self.call("CancelTimerIntent", {"TimerNumber": "2"})
        self.assertEqual(self.board.cancels, [expected])
        self.assertEqual(len(self.board.store.list_timers()), 1)

    def test_cancel_mismatched_name_number_does_not_cancel(self):
        self.call("StartTimerIntent", {"TimerLabel": "Tea", "TimerDuration": "PT5M"})
        self.assertIn("do not match", speech(self.call("CancelTimerIntent", {"TimerNumber": "1", "TimerLabel": "Pasta"})))
        self.assertFalse(self.board.cancels)

    def test_no_after_uncertain_timer_does_not_claim_no_write(self):
        self.board.fail_after_timer = True
        first = self.call("StartTimerIntent", {"TimerLabel": "Tea", "TimerDuration": "PT1M"})
        self.assertIn("earlier save may have completed", speech(self.call("AMAZON.NoIntent", previous=first)))

    def test_public_flight_requires_airline_date_and_confirmation(self):
        first = self.call("TrackFlightIntent", {"FlightNumber": "123"})
        self.assertIn("Which airline", speech(first))
        second = self.call("SetFlightAirlineIntent", {"Airline": "Allegiant"}, first, "airline-turn")
        self.assertIn("What date", speech(second))
        third = self.call("SetDateIntent", {"Date": "2026-11-03"}, second, "date-turn")
        self.assertIn("G4 flight 123", speech(third))
        self.assertFalse(self.board.watches)
        saved = self.call("AMAZON.YesIntent", previous=third)
        self.assertIn("Live flight data is not configured", speech(saved))
        self.assertEqual(self.board.watches, [{"airline": "G4", "flight_number": "123", "date": "2026-11-03",
                                             "source": "alexa", "request_id": "synthetic-initial"}])

    def test_airline_aliases_do_not_confuse_allegiant_and_air_arabia(self):
        for name, code in {"Delta": "DL", "American Airlines": "AA", "United": "UA", "Allegiant": "G4",
                           "Frontier": "F9", "Air Arabia": "G9", "G9": "G9", "G4": "G4"}.items():
            self.assertEqual(alexa._airline(name), code)
        unknown = self.call("TrackFlightIntent", {"Airline": "unknown airline", "FlightNumber": "123", "Date": "2026-11-03"})
        self.assertIn("Which airline", speech(unknown))
        self.assertFalse(self.board.watches)

    def test_ambiguous_date_is_not_defaulted_or_written(self):
        response = self.call("TrackFlightIntent", {"Airline": "Delta", "FlightNumber": "123", "Date": "2026-W44"})
        self.assertIn("What date", speech(response))
        self.call("AMAZON.YesIntent", previous=response)
        self.assertFalse(self.board.watches)

    def test_flight_retry_preserves_request_and_configured_is_not_live_proof(self):
        self.board.fail_after_watch = True
        self.board.tracking_available = True
        confirmation = self.call("TrackFlightIntent", {"Airline": "United", "FlightNumber": "123", "Date": "2026-11-03"})
        retry = self.call("AMAZON.YesIntent", previous=confirmation)
        saved = self.call("AMAZON.YesIntent", previous=retry, request_id="retry-turn")
        self.assertEqual(self.board.watches[0], self.board.watches[1])
        self.assertIn("Already added", speech(saved))
        self.assertIn("when available", speech(saved))
        self.assertNotIn("tracking live", speech(saved).lower())

    def test_timer_and_flight_labels_are_not_logged(self):
        stream = io.StringIO()
        with patch("sys.stdout", stream), patch("sys.stderr", stream):
            response = self.call("StartTimerIntent", {"TimerLabel": "<test label>", "TimerDuration": "PT1M"})
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(response["response"]["outputSpeech"]["type"], "PlainText")

    def test_shopping_add_show_read_remove_use_only_scoped_methods(self):
        self.assertIn("Added olives", speech(self.call("AddShoppingItemIntent", {"Item": "olives"}, request_id="add")))
        shown = self.call("ShowShoppingListIntent", request_id="show")
        self.assertIn("Showing the shopping list", speech(shown))
        self.assertTrue(self.board.visible)
        before = len(self.board.shopping_updates)
        self.assertIn("olives", speech(self.call("ReadShoppingListIntent")))
        self.assertEqual(len(self.board.shopping_updates), before)
        self.assertIn("Removed olives", speech(self.call("RemoveShoppingItemIntent", {"Item": "olives"}, request_id="remove")))
        self.assertEqual(self.board.items, [])
        self.assertEqual(self.board.shopping_updates[0], ("add", {"item": "olives", "source": "alexa", "request_id": "add"}))

    def test_shopping_clear_requires_confirmation_and_no_preserves_list(self):
        self.call("AddShoppingItemIntent", {"Item": "olives"}, request_id="add")
        confirm = self.call("ClearShoppingListIntent", request_id="clear")
        self.assertIn("Say yes to clear", speech(confirm))
        self.assertEqual(len(self.board.shopping_updates), 1)
        self.call("AMAZON.NoIntent", previous=confirm)
        self.assertEqual(len(self.board.items), 1)
        self.call("AMAZON.YesIntent", previous=confirm, request_id="confirmation-turn")
        self.assertEqual(self.board.items, [])
        self.assertEqual(self.board.shopping_updates[-1], ("clear", {"request_id": "clear", "confirmed": True}))

    def test_uncertain_shopping_write_retries_same_request_without_duplicate(self):
        self.board.fail_after_shopping = True
        retry = self.call("AddShoppingItemIntent", {"Item": "olives"})
        self.assertEqual(retry["sessionAttributes"]["pending"]["phase"], "retry")
        saved = self.call("AMAZON.YesIntent", previous=retry, request_id="retry-turn")
        self.assertEqual(len(self.board.items), 1)
        self.assertEqual(self.board.shopping_updates[0], self.board.shopping_updates[1])
        self.assertIn("already on", speech(saved))

    def test_shopping_missing_or_oversized_item_never_writes(self):
        for item in (None, "", "a" * 81, "a\nb"):
            response = self.call("AddShoppingItemIntent", {} if item is None else {"Item": item})
            self.assertIn("What item", speech(response))
        self.assertEqual(self.board.shopping_updates, [])

    def test_shopping_read_is_bounded_and_does_not_log_contents(self):
        self.board.items = [{"id": str(i), "name": "synthetic item " + str(i)} for i in range(100)]
        stream = io.StringIO()
        with patch("sys.stdout", stream), patch("sys.stderr", stream):
            response = self.call("ReadShoppingListIntent")
        self.assertEqual(stream.getvalue(), "")
        self.assertIn("90 more items", speech(response))
        self.assertNotIn("synthetic item 99", speech(response))


class ModelConsistencyTests(unittest.TestCase):
    def test_reused_slot_names_always_use_one_type(self):
        model = json.loads((Path(__file__).parents[1] / "integrations/alexa/interaction-model-en-US.json").read_text())
        slot_types = {}
        for intent in model["interactionModel"]["languageModel"]["intents"]:
            for slot in intent.get("slots", []):
                name = slot["name"].casefold()
                self.assertEqual(slot_types.setdefault(name, slot["type"]), slot["type"], name)
        self.assertEqual(slot_types["title"], "APPOINTMENT_TITLE")

    def test_board_client_never_allows_operating_calendar_or_admin_routes(self):
        environment = {"ALEXA_SKILL_ID": SKILL, "FLIGHTWALL_BASE_URL": "https://board.example.test",
                       "FLIGHTWALL_ALEXA_TOKEN": "synthetic-dedicated-token-long-enough"}
        with patch.dict(os.environ, environment), patch.object(alexa, "build_opener") as opener:
            client = alexa.BoardClient()
            for path in ("/api/v1/status", "/api/v1/display", "/api/v1/settings", "/mcp", "/api/v1/calendar/refresh"):
                with self.assertRaises(alexa.BoardError):
                    client._request(path, {})
            opener.return_value.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
