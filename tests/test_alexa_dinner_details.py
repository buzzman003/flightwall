"""Synthetic dinner conversations; no AWS, household data, or live speech."""
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flightwall.personal import PersonalError, PersonalStore
from integrations.alexa import lambda_function as alexa

SKILL = "amzn1.ask.skill.synthetic-dinner-details"
NOW = datetime(2026, 11, 1, 20, tzinfo=timezone.utc)


def event(name, values=None, previous=None, request_id="original-dinner"):
    return {"context": {"System": {"application": {"applicationId": SKILL}}},
            "session": {"application": {"applicationId": SKILL},
                        "attributes": previous.get("sessionAttributes", {}) if previous else {}},
            "request": {"type": "IntentRequest", "locale": "en-US", "requestId": request_id,
                        "intent": {"name": name, "slots": {key: {"value": value} for key, value in (values or {}).items()}}}}


def speech(response):
    return response["response"]["outputSpeech"]["text"]


class Board:
    def __init__(self, store):
        self.store, self.calls, self.results = store, [], []
        self.fail_after_save = False

    def context(self):
        return self.store.voice_context()

    def save(self, payload):
        self.calls.append(copy.deepcopy(payload))
        try:
            result = self.store.create(payload)
        except PersonalError as error:
            raise alexa.BoardError(retryable=False, public_message=str(error)) from None
        self.results.append(result)
        if self.fail_after_save:
            self.fail_after_save = False
            raise alexa.BoardError()
        return result

    def list_timers(self):
        return []


class DinnerDetailTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = {"timezone": "America/Chicago", "appointment_duration_minutes": 60,
                       "dinner_start": "14:30", "dinner_end": "19:00", "kids_filter_enabled": True}
        self.store = PersonalStore(self.folder.name, lambda: self.config)
        self.board = Board(self.store)
        for replacement in (patch.dict(os.environ, {"ALEXA_SKILL_ID": SKILL}),
                            patch.object(alexa, "_now", return_value=NOW),
                            patch("flightwall.personal._aware_now", side_effect=lambda *_: alexa._now()),
                            patch.object(alexa, "BoardClient", return_value=self.board)):
            replacement.start()
            self.addCleanup(replacement.stop)

    def call(self, name, values=None, previous=None, request_id="original-dinner"):
        return alexa.lambda_handler(event(name, values, previous, request_id), None)

    def meal(self, **kwargs):
        return self.call("AddDinnerTodayIntent", {"Meal": "Chicken teriyaki"}, **kwargs)

    def test_dinner_card_alias_starts_meal_question_without_saving(self):
        path = Path(__file__).resolve().parents[1] / "integrations/alexa/interaction-model-en-US.json"
        intents = json.loads(path.read_text())["interactionModel"]["languageModel"]["intents"]
        matches = [row for row in intents if "set dinner card" in row.get("samples", [])]
        self.assertEqual([row["name"] for row in matches], ["AddDinnerIntent"])
        self.assertIn("set dinner card to {Meal}", matches[0]["samples"])
        first = self.call(matches[0]["name"], request_id="dinner-card-command")
        self.assertEqual(speech(first), "What is for dinner?")
        self.assertEqual(first["response"]["directives"][0]["slotToElicit"], "Meal")
        self.assertFalse(self.board.calls)
        second = self.call("AddDinnerIntent", {"Meal": "Chicken teriyaki"}, first, "meal-reply")
        self.assertIn("What sides?", speech(second))
        self.assertFalse(self.board.calls)
        cancelled = self.call("AMAZON.CancelIntent", previous=second)
        self.assertIn("did not save", speech(cancelled))
        self.assertFalse(self.board.calls)
        self.assertFalse(self.store.list_items(NOW))

    def test_sides_then_window_save_once_preserving_original_id_and_display_end(self):
        first = self.meal()
        self.assertFalse(self.board.calls)
        self.assertIn("What sides?", speech(first))
        directive = first["response"]["directives"][0]
        self.assertEqual((directive["type"], directive["slotToElicit"]), ("Dialog.ElicitSlot", "Sides"))
        second = self.call("SetDinnerSidesIntent", {"Sides": "rice and broccoli"}, first, "sides-turn")
        self.assertFalse(self.board.calls)
        self.assertIn("When should dinner be ready", speech(second))
        done = self.call("SetDinnerReadyWindowIntent", {"ReadyStart": "18:45", "ReadyEnd": "19:15"}, second, "time-turn")
        self.assertEqual(self.board.calls, [{"kind": "dinner", "title": "Chicken teriyaki", "date": "2026-11-01",
            "request_id": "original-dinner", "source": "alexa", "sides": "rice and broccoli",
            "ready_window_start": "18:45", "ready_window_end": "19:15"}])
        item = self.board.results[-1]["item"]
        self.assertEqual(item["ready_at"], "2026-11-02T00:45:00.000000Z")
        self.assertEqual(item["ready_until"], "2026-11-02T01:15:00.000000Z")
        self.assertEqual(item["display_end"], "2026-11-02T01:00:00.000000Z")
        self.assertIn("6:45 P.M. and 7:15 P.M.", speech(done))
        self.assertIn("Kids are Home", speech(done))
        self.assertFalse(done["response"]["shouldEndSession"])
        self.assertEqual(done["sessionAttributes"], {})
        self.assertEqual(done["response"]["reprompt"]["outputSpeech"]["text"], "Anything else? Say no to finish.")

    def test_no_skips_optional_details_but_stop_cancels_unsaved_dinner(self):
        first = self.meal()
        second = self.call("AMAZON.NoIntent", previous=first)
        self.assertFalse(self.board.calls)
        done = self.call("AMAZON.NoIntent", previous=second)
        self.assertIn("Saved Chicken teriyaki", speech(done))
        self.assertNotIn("sides", self.board.calls[-1])
        self.assertNotIn("ready_time", self.board.calls[-1])
        draft = self.meal(request_id="cancelled-draft")
        cancelled = self.call("AMAZON.StopIntent", previous=draft)
        self.assertTrue(cancelled["response"]["shouldEndSession"])
        self.assertIn("did not save", speech(cancelled))
        self.assertEqual(len(self.board.calls), 1)

    def test_save_dinner_shortcut_and_following_commands_clear_completed_pending(self):
        first = self.meal()
        done = self.call("SaveDinnerIntent", previous=first)
        self.assertEqual(len(self.board.calls), 1)
        timers = self.call("ListTimersIntent", previous=done, request_id="another-command")
        self.assertIn("no Flight Wall timers", speech(timers))
        self.assertFalse(timers["response"]["shouldEndSession"])
        goodbye = self.call("AMAZON.NoIntent", previous=timers)
        self.assertEqual(speech(goodbye), "Okay.")
        self.assertTrue(goodbye["response"]["shouldEndSession"])
        self.assertNotIn("did not save", speech(goodbye))

    def test_elicited_free_text_control_words_are_not_saved_as_sides(self):
        first = self.meal()
        second = self.call("SetDinnerSidesIntent", {"Sides": "skip"}, previous=first)
        self.assertFalse(self.board.calls)
        self.assertEqual(second["sessionAttributes"]["pending"]["phase"], "ready")
        done = self.call("SetDinnerReadyTimeIntent", {"ReadyTime": "save dinner"}, previous=second)
        self.assertIn("Saved Chicken teriyaki", speech(done))
        self.assertNotIn("sides", self.board.calls[-1])
        self.assertNotIn("ready_time", self.board.calls[-1])

    def test_explicit_side_label_is_split_but_sauce_is_not_guessed_as_side(self):
        first = self.call("AddDinnerTodayIntent", {"Meal": "chicken with sides of rice and peas"})
        self.assertIn("When should dinner be ready", speech(first))
        self.call("SaveDinnerIntent", previous=first)
        self.assertEqual(self.board.calls[-1]["title"], "chicken")
        self.assertEqual(self.board.calls[-1]["sides"], "rice and peas")
        second = self.call("AddDinnerTomorrowIntent", {"Meal": "chicken with garlic sauce"}, request_id="another-dinner")
        self.assertIn("What sides?", speech(second))
        self.assertEqual(second["sessionAttributes"]["pending"]["title"], "chicken with garlic sauce")

    def test_missing_meal_elicitation_keeps_tomorrow_date_after_midnight(self):
        first = self.call("AddDinnerTomorrowIntent")
        self.assertFalse(self.board.calls)
        self.assertEqual(first["response"]["directives"][0]["slotToElicit"], "Meal")
        with patch.object(alexa, "_now", return_value=datetime(2026, 11, 2, 6, 1, tzinfo=timezone.utc)):
            second = self.call("AddDinnerIntent", {"Meal": "pasta"}, first, "meal-after-midnight")
            done = self.call("SaveDinnerIntent", previous=second)
        self.assertEqual(self.board.calls[-1]["date"], "2026-11-02")
        self.assertEqual(self.board.calls[-1]["request_id"], "original-dinner")
        self.assertIn("dinner today", speech(done))

    def test_uncertain_write_retries_frozen_payload_and_rejects_detail_mutation(self):
        first = self.meal()
        second = self.call("SetDinnerSidesIntent", {"Sides": "rice"}, first)
        self.board.fail_after_save = True
        retry = self.call("SetDinnerReadyTimeIntent", {"ReadyTime": "18:30"}, second)
        self.assertIn("could not confirm whether", speech(retry))
        refused = self.call("SetDinnerSidesIntent", {"Sides": "changed"}, retry, "change-during-retry")
        self.assertIn("unchanged dinner", speech(refused))
        self.assertEqual(len(self.board.calls), 1)
        done = self.call("AMAZON.YesIntent", previous=refused, request_id="retry-turn")
        self.assertEqual(self.board.calls[0], self.board.calls[1])
        self.assertEqual(len(self.store.list_items(NOW)), 1)
        self.assertIn("Already saved", speech(done))

    def test_morning_clock_is_read_back_and_no_requests_correction_without_saving(self):
        first = self.call("SkipDinnerDetailIntent", previous=self.meal())
        question = self.call("SetDinnerReadyWindowIntent", {"ReadyStart": "06:45", "ReadyEnd": "07:15"}, first)
        self.assertIn("6:45 A.M. and 7:15 A.M.", speech(question))
        self.assertFalse(self.board.calls)
        corrected = self.call("AMAZON.NoIntent", previous=question)
        self.assertFalse(self.board.calls)
        self.assertEqual(corrected["sessionAttributes"]["pending"]["phase"], "ready")
        self.call("SetDinnerReadyWindowIntent", {"ReadyStart": "18:45", "ReadyEnd": "19:15"}, corrected)
        self.assertEqual(len(self.board.calls), 1)

    def test_morning_confirmation_or_save_without_unconfirmed_clock(self):
        first = self.call("SkipDinnerDetailIntent", previous=self.meal())
        question = self.call("SetDinnerReadyTimeIntent", {"ReadyTime": "06:30"}, first)
        self.call("AMAZON.YesIntent", previous=question)
        self.assertEqual(self.board.calls[-1]["ready_time"], "06:30")
        first = self.call("SkipDinnerDetailIntent", previous=self.meal(request_id="second-dinner"))
        question = self.call("SetDinnerReadyTimeIntent", {"ReadyTime": "06:45"}, first)
        self.call("SaveDinnerIntent", previous=question)
        self.assertNotIn("ready_time", self.board.calls[-1])

    def test_invalid_incomplete_reversed_and_clock_change_times_never_write(self):
        first = self.call("SkipDinnerDetailIntent", previous=self.meal())
        for name, values in (("SetDinnerReadyTimeIntent", {"ReadyTime": "EV"}),
                             ("SetDinnerReadyWindowIntent", {"ReadyStart": "18:45"}),
                             ("SetDinnerReadyWindowIntent", {"ReadyStart": "19:15", "ReadyEnd": "18:45"})):
            with self.subTest(values=values):
                result = self.call(name, values, first)
                self.assertIn("exact same-day", speech(result))
                self.assertFalse(self.board.calls)
        for day, clock in ((datetime(2027, 3, 14, 12, tzinfo=timezone.utc), "02:30"),
                           (datetime(2027, 11, 7, 12, tzinfo=timezone.utc), "01:30")):
            with self.subTest(day=day), patch.object(alexa, "_now", return_value=day):
                draft = self.call("SkipDinnerDetailIntent", previous=self.meal())
                result = self.call("SetDinnerReadyTimeIntent", {"ReadyTime": clock}, draft)
                self.assertIn("Clock-change", speech(result))
                self.assertFalse(self.board.calls)

    def test_bounded_plaintext_sides_and_interrupted_session_save_nothing(self):
        first = self.meal()
        for sides in ("x" * 121, "private\nside", ""):
            with self.subTest(size=len(sides)):
                result = self.call("SetDinnerSidesIntent", {"Sides": sides}, first)
                self.assertFalse(self.board.calls)
                self.assertIn("120 characters", speech(result))
        result = self.call("SetDinnerSidesIntent", {"Sides": "rice <audio src='ignored'/> & peas"}, first)
        output = io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", output):
            self.call("AMAZON.RepeatIntent", previous=result)
            self.call("AMAZON.FallbackIntent", previous=result)
            ended = event("FinishSkillIntent", previous=result)
            self.call("FinishSkillIntent", previous=result)
            ended["request"]["type"] = "SessionEndedRequest"
            self.assertEqual(alexa.lambda_handler(ended, None), {"version": "1.0", "response": {}})
        self.assertFalse(self.board.calls)
        self.assertEqual(output.getvalue(), "")

    def test_different_timezone_requires_ready_time_again(self):
        first = self.call("SkipDinnerDetailIntent", previous=self.meal())
        self.config["timezone"] = "America/New_York"
        result = self.call("SetDinnerReadyTimeIntent", {"ReadyTime": "18:45"}, first)
        self.assertIn("time zone changed", speech(result))
        self.assertFalse(self.board.calls)
        self.assertNotIn("ready_time", result["sessionAttributes"]["pending"])

    def test_invalid_time_correction_cannot_reuse_an_earlier_checked_flag(self):
        first = self.meal()
        with_time = self.call("SetDinnerReadyTimeIntent", {"ReadyTime": "18:30"}, first)
        self.assertEqual(with_time["sessionAttributes"]["pending"]["phase"], "sides")
        invalid = self.call("SetDinnerReadyTimeIntent", {"ReadyTime": "EV"}, with_time)
        with_side = self.call("SetDinnerSidesIntent", {"Sides": "rice"}, invalid)
        self.assertFalse(self.board.calls)
        self.assertEqual(with_side["sessionAttributes"]["pending"]["phase"], "ready")


class DinnerDialogModelTests(unittest.TestCase):
    def test_manual_elicitation_matches_slots_prompts_and_does_not_extend_listening(self):
        path = Path(__file__).resolve().parents[1] / "integrations/alexa/interaction-model-en-US.json"
        model = json.loads(path.read_text())["interactionModel"]
        self.assertEqual(model["languageModel"]["invocationName"], "flight wall")
        self.assertEqual(model["dialog"]["delegationStrategy"], "SKILL_RESPONSE")
        intents = {row["name"]: row for row in model["languageModel"]["intents"]}
        prompts = {row["id"] for row in model["prompts"]}
        for row in model["dialog"]["intents"]:
            slots = {slot["name"]: slot for slot in intents[row["name"]]["slots"]}
            for slot in row["slots"]:
                self.assertEqual(slot["type"], slots[slot["name"]]["type"])
                self.assertNotIn("samples", slot)  # Samples belong to the languageModel Slot schema.
                if slot["elicitationRequired"]:
                    self.assertIn(slot["prompts"]["elicitation"], prompts)
                    self.assertIn("{" + slot["name"] + "}", slots[slot["name"]]["samples"])
        response = alexa._complete("Saved dinner.")
        self.assertNotIn("directives", response["response"])
        self.assertEqual(response["response"]["outputSpeech"]["type"], "PlainText")
        self.assertNotIn("ssml", json.dumps(response).lower())


if __name__ == "__main__":
    unittest.main()
