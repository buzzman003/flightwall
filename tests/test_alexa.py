"""Offline Alexa envelope, HTTP-boundary, NLU-model, and real-store checks."""
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from zoneinfo import ZoneInfo

from integrations.alexa import lambda_function as alexa
from flightwall.personal import PersonalError, PersonalStore


SKILL_ID = "amzn1.ask.skill.00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
CONTEXT = {"timezone": "America/New_York", "appointment_duration_minutes": 60,
           "dinner_start": "14:30", "dinner_end": "19:00"}


def event(name="AddDinnerIntent", slots=None, attributes=None, request_id="amzn1.echo-api.request.initial"):
    return {"version": "1.0", "context": {"System": {"application": {"applicationId": SKILL_ID}}},
            "session": {"application": {"applicationId": SKILL_ID}, "attributes": attributes or {}},
            "request": {"type": "IntentRequest", "requestId": request_id, "locale": "en-US",
                        "intent": {"name": name, "slots": {key: {"name": key, "value": value}
                                                            for key, value in (slots or {}).items()}}}}


def speech(response):
    return response["response"]["outputSpeech"]["text"]


class StoreAdapter:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.calls = []
        self.fail_after_save = False
        self.display_enabled = True
        self.results = []

    def context(self):
        return self.store.voice_context()

    def save(self, payload):
        self.calls.append(copy.deepcopy(payload))
        try:
            result = self.store.create(payload)
        except PersonalError as exc:
            raise alexa.BoardError(retryable=False, public_message=str(exc)) from None
        result["display_enabled"] = self.display_enabled
        self.results.append(result)
        if self.fail_after_save:
            self.fail_after_save = False
            raise alexa.BoardError()
        return result


class AlexaConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = dict(CONTEXT)
        self.store = PersonalStore(self.temporary.name, lambda: self.config)
        self.board = StoreAdapter(self.store, self.config)
        self.patches = [patch.dict(os.environ, {"ALEXA_SKILL_ID": SKILL_ID}),
                        patch.object(alexa, "_now", return_value=NOW),
                        patch("flightwall.personal._aware_now", side_effect=lambda *_: alexa._now()),
                        patch.object(alexa, "BoardClient", return_value=self.board)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def call(self, name="AddDinnerIntent", slots=None, previous=None, request_id="amzn1.echo-api.request.initial"):
        response = alexa.lambda_handler(event(name, slots, previous and previous["sessionAttributes"], request_id), None)
        # These legacy preservation cases omit optional dinner fields. Complete
        # the new explicit skip turns while retaining the original request ID.
        # The separate dinner-dialog tests exercise every real intermediate turn.
        for step in range(2):
            draft = response.get("sessionAttributes", {}).get("pending", {})
            if draft.get("kind") != "dinner" or draft.get("phase") not in {"sides", "ready"}:
                break
            response = alexa.lambda_handler(event("SkipDinnerDetailIntent", attributes=response["sessionAttributes"],
                                                   request_id=f"optional-skip-{step}"), None)
        return response

    def appointment(self, **values):
        slots = {"Title": "dentist", "Date": "2026-10-23", "Time": "09:00", **values}
        return self.call("AddAppointmentIntent", slots)

    def test_appointment_requires_explicit_readback_confirmation(self):
        response = self.appointment()
        self.assertFalse(self.board.calls)
        self.assertIn("dentist", speech(response))
        self.assertIn("Friday, October 23, 2026", speech(response))
        self.assertIn("9 A.M.", speech(response))
        self.assertIn("60 minutes", speech(response))
        self.assertIn("America New York", speech(response))
        self.assertIn("Should I save", speech(response))
        result = self.call("AMAZON.YesIntent", previous=response, request_id="request.confirmation")
        self.assertEqual(len(self.board.calls), 1)
        self.assertEqual(self.board.calls[0], {"kind": "appointment", "title": "dentist", "date": "2026-10-23",
                                             "time": "09:00", "duration_minutes": 60, "source": "alexa",
                                             "request_id": "amzn1.echo-api.request.initial"})
        self.assertIn("when the appointment ends", speech(result))
        self.assertNotIn("one hour after", speech(result))
        item = self.board.results[-1]["item"]
        self.assertEqual(item["display_end"], item["end"])
        self.assertEqual(datetime.fromisoformat(item["display_start"].replace("Z", "+00:00")).hour, 4)

    def test_multiturn_free_title_then_date_then_time_then_yes(self):
        first = self.call("SetAppointmentTitleIntent", {"Title": "annual eye checkup"})
        self.assertIn("What day", speech(first))
        second = self.call("SetDateIntent", {"Date": "2026-10-23"}, first, "request.date")
        self.assertIn("exact time", speech(second))
        third = self.call("SetTimeIntent", {"Time": "15:00"}, second, "request.time")
        self.assertIn("Should I save", speech(third))
        self.assertFalse(self.board.calls)
        self.call("AMAZON.YesIntent", previous=third, request_id="request.yes")
        self.assertEqual(self.board.calls[-1]["request_id"], "amzn1.echo-api.request.initial")
        self.assertEqual(self.board.calls[-1]["title"], "annual eye checkup")

    def test_missing_title_is_elicited_without_guessing(self):
        response = self.call("AddAppointmentIntent", {"Date": "2026-10-23", "Time": "09:00"})
        self.assertIn("What is the appointment called", speech(response))
        self.assertFalse(self.board.calls)

    def test_ambiguous_date_and_time_period_are_elicited(self):
        response = self.appointment(Date="2026-W44", Time="EV")
        self.assertIn("What day", speech(response))
        response = self.call("SetDateIntent", {"Date": "2026-10-23"}, response)
        self.assertIn("exact time", speech(response))
        self.assertFalse(self.board.calls)

    def test_duration_fractional_hours_and_invalid_seconds(self):
        response = self.appointment(Duration="PT1.5H")
        self.assertIn("90 minutes", speech(response))
        response = self.appointment(Duration="PT45S")
        self.assertIn("How long", speech(response))
        self.assertFalse(self.board.calls)

    def test_date_and_time_changes_require_a_new_readback(self):
        response = self.appointment()
        changed = self.call("SetTimeIntent", {"Time": "15:30"}, response)
        self.assertIn("3:30 P.M.", speech(changed))
        self.assertFalse(self.board.calls)
        self.call("AMAZON.YesIntent", previous=changed)
        self.assertEqual(self.board.calls[-1]["time"], "15:30")

    def test_gap_and_fold_times_are_not_guessed(self):
        for day, clock in (("2027-03-14", "02:30"), ("2026-11-01", "01:30")):
            with self.subTest(day=day):
                response = self.appointment(Date=day, Time=clock)
                self.assertIn("clock change", speech(response))
                self.assertFalse(self.board.calls)

    def test_already_ended_appointment_requests_a_future_date(self):
        response = self.appointment(Date="2026-10-20")
        self.assertIn("already have ended", speech(response))
        self.assertFalse(self.board.calls)

    def test_changed_board_timezone_requires_another_yes(self):
        response = self.appointment()
        self.config["timezone"] = "America/Los_Angeles"
        changed = self.call("AMAZON.YesIntent", previous=response)
        self.assertIn("time zone changed", speech(changed))
        self.assertFalse(self.board.calls)
        self.call("AMAZON.YesIntent", previous=changed)
        self.assertEqual(len(self.board.calls), 1)

    def test_dinner_defaults_to_board_local_today_not_lambda_utc_date(self):
        self.config["timezone"] = "Pacific/Honolulu"
        with patch.object(alexa, "_now", return_value=datetime(2026, 10, 22, 1, tzinfo=timezone.utc)):
            response = self.call(slots={"Meal": "teriyaki chicken"})
        self.assertEqual(self.board.calls[-1]["date"], "2026-10-21")
        self.assertEqual(self.board.calls[-1]["kind"], "dinner")
        self.assertNotIn("duration_minutes", self.board.calls[-1])
        self.assertEqual(speech(response), "Saved teriyaki chicken for dinner today. Anything else?")
        item = self.board.results[-1]["item"]
        zone = ZoneInfo(self.config["timezone"])
        self.assertEqual(datetime.fromisoformat(item["display_start"]).astimezone(zone).strftime("%H:%M"), "14:30")
        self.assertEqual(datetime.fromisoformat(item["display_end"]).astimezone(zone).strftime("%H:%M"), "19:00")

    def test_dinner_date_can_be_selected_before_free_text_meal(self):
        first = self.call(slots={"Date": "2026-10-23"})
        self.assertIn("What is for dinner", speech(first))
        self.assertFalse(self.board.calls)
        result = self.call(slots={"Meal": "pasta"}, previous=first, request_id="request.meal")
        self.assertEqual(self.board.calls[-1]["date"], "2026-10-23")
        self.assertIn("Saved pasta", speech(result))

    def test_dinner_after_seven_pm_stays_today_and_explains_expired_window(self):
        with patch.object(alexa, "_now", return_value=datetime(2026, 10, 23, 1, tzinfo=timezone.utc)):
            response = self.call(slots={"Meal": "teriyaki chicken"})
        self.assertEqual(self.board.calls[-1]["date"], "2026-10-22")
        self.assertIn("display window has already ended", speech(response))
        self.assertIn("will not appear", speech(response))
        self.assertIn("not moved it to tomorrow", speech(response))

    def test_short_dinner_response_preserves_changed_window_during_write(self):
        save = self.board.save
        def changed_settings(payload):
            self.config.update(dinner_start="16:00", dinner_end="20:00")
            return save(payload)
        with patch.object(self.board, "save", side_effect=changed_settings):
            response = self.call(slots={"Meal": "pasta"})
        self.assertEqual(speech(response), "Saved pasta for dinner today. Anything else?")
        item = self.board.results[-1]["item"]
        zone = ZoneInfo(self.config["timezone"])
        self.assertEqual(datetime.fromisoformat(item["display_start"]).astimezone(zone).strftime("%H:%M"), "16:00")
        self.assertEqual(datetime.fromisoformat(item["display_end"]).astimezone(zone).strftime("%H:%M"), "20:00")

    def test_explicit_dinner_days_use_board_calendar_day_across_dst_and_year_boundary(self):
        self.config["timezone"] = "America/Chicago"
        cases = (
            (datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc), "2026-10-31", "2026-11-01", "20:30"),
            (datetime(2026, 3, 8, 5, 30, tzinfo=timezone.utc), "2026-03-07", "2026-03-08", "19:30"),
            (datetime(2027, 1, 1, 5, 30, tzinfo=timezone.utc), "2026-12-31", "2027-01-01", "20:30"),
        )
        for now, today, tomorrow, tomorrow_utc_start in cases:
            for name, expected in (("AddDinnerTodayIntent", today), ("AddDinnerTomorrowIntent", tomorrow)):
                with self.subTest(now=now, intent=name), patch.object(alexa, "_now", return_value=now):
                    response = self.call(name, {"Meal": "Chicken teriyaki & rice"}, request_id=f"{name}.{today}")
                    self.assertEqual(self.board.calls[-1]["date"], expected)
                    self.assertEqual(self.board.calls[-1]["title"], "Chicken teriyaki & rice")
                    self.assertEqual(response["response"]["shouldEndSession"], name != "AddDinnerTomorrowIntent")
                    if name == "AddDinnerTomorrowIntent":
                        item = self.board.results[-1]["item"]
                        self.assertEqual(datetime.fromisoformat(item["display_start"]).astimezone(timezone.utc).strftime("%H:%M"), tomorrow_utc_start)
                        self.assertEqual(speech(response), "Saved Chicken teriyaki & rice for dinner tomorrow. Anything else?")
                    else:
                        self.assertIn("display window has already ended", speech(response))

    def test_explicit_dinner_missing_meal_retains_selected_date_on_followup(self):
        self.config["timezone"] = "America/Chicago"
        with patch.object(alexa, "_now", return_value=datetime(2026, 11, 1, 4, 59, tzinfo=timezone.utc)):
            first = self.call("AddDinnerTomorrowIntent")
        self.assertFalse(self.board.calls)
        self.assertIn("What is for dinner", speech(first))
        self.assertEqual(first["sessionAttributes"]["pending"]["date"], "2026-11-01")
        with patch.object(alexa, "_now", return_value=datetime(2026, 11, 1, 6, 1, tzinfo=timezone.utc)):
            response = self.call(slots={"Meal": "Chicken Tonight"}, previous=first, request_id="meal.followup")
        self.assertEqual(self.board.calls[-1]["date"], "2026-11-01")
        self.assertEqual(self.board.calls[-1]["title"], "Chicken Tonight")
        self.assertEqual(speech(response), "Saved Chicken Tonight for dinner today. Anything else?")

    def test_explicit_dinner_replaces_old_pending_date_and_reads_fresh_board_timezone(self):
        first = self.call(slots={"Date": "2026-12-25"})
        first["sessionAttributes"]["pending"]["title"] = "Old meal"
        self.config["timezone"] = "America/Chicago"
        with patch.object(alexa, "_now", return_value=datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)):
            response = self.call("AddDinnerTomorrowIntent", {"Meal": "Fresh meal"}, first, "fresh.meal")
        self.assertEqual(self.board.calls[-1]["date"], "2026-11-01")
        self.assertEqual(self.board.calls[-1]["request_id"], "fresh.meal")
        self.assertEqual(speech(response), "Saved Fresh meal for dinner tomorrow. Anything else?")

    def test_explicit_dinner_without_meal_never_reuses_prior_title(self):
        first = self.call(slots={"Date": "2026-12-25"})
        first["sessionAttributes"]["pending"]["title"] = "Old meal"
        response = self.call("AddDinnerTodayIntent", previous=first, request_id="new.today")
        self.assertFalse(self.board.calls)
        self.assertNotIn("title", response["sessionAttributes"]["pending"])
        self.assertIn("What is for dinner", speech(response))

    def test_dinner_date_words_captured_as_meal_require_clarification_without_saving(self):
        cases = (
            ("AddDinnerIntent", "chicken teriyaki tomorrow"),
            ("AddDinnerIntent", "chicken teriyaki for dinner tomorrow"),
            ("AddDinnerTodayIntent", "chicken teriyaki tomorrow"),
            ("AddDinnerTomorrowIntent", "chicken teriyaki today"),
            ("AddDinnerTodayIntent", "chicken teriyaki is for dinner tomorrow"),
            ("AddDinnerTomorrowIntent", "chicken teriyaki is for dinner tomorrow"),
            ("AddDinnerTomorrowIntent", "tomorrow"),
        )
        for name, meal in cases:
            with self.subTest(intent=name, meal=meal):
                response = self.call(name, {"Meal": meal})
                self.assertFalse(self.board.calls)
                self.assertIn("followed by just the meal", speech(response))
                self.assertNotIn("title", response["sessionAttributes"]["pending"])

    def test_explicit_dinner_preserves_exact_meal_and_same_day_replacement(self):
        today = self.call("AddDinnerTodayIntent", {"Meal": "Chicken Tonight"}, request_id="today.first")
        self.call("AddDinnerTomorrowIntent", {"Meal": "Soup & Sourdough"}, request_id="tomorrow.first")
        self.call("AddDinnerTodayIntent", {"Meal": "Crème brûlée"}, request_id="today.second")
        response = self.call("AddDinnerTodayIntent", {"Meal": "Chicken Tonight"}, request_id="today.first")
        self.assertEqual(speech(today), "Saved Chicken Tonight for dinner today. Anything else?")
        self.assertIn("already replaced or cancelled", speech(response))
        self.assertEqual({item["title"] for item in self.store.list_items()}, {"Crème brûlée", "Soup & Sourdough"})

    def test_explicit_dinner_uncertain_retry_keeps_original_day_after_midnight(self):
        self.config["timezone"] = "America/Chicago"
        self.board.fail_after_save = True
        with patch.object(alexa, "_now", return_value=datetime(2026, 11, 1, 4, 59, tzinfo=timezone.utc)):
            first = self.call("AddDinnerTomorrowIntent", {"Meal": "Pasta"})
        self.assertIn("could not confirm whether", speech(first))
        with patch.object(alexa, "_now", return_value=datetime(2026, 11, 1, 6, 1, tzinfo=timezone.utc)):
            response = self.call("AMAZON.YesIntent", previous=first, request_id="retry.after.midnight")
        self.assertEqual(self.board.calls[0], self.board.calls[1])
        self.assertEqual(self.board.calls[-1]["date"], "2026-11-01")
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertIn("Already saved Pasta for dinner today", speech(response))

    def test_explicit_dinner_errors_never_save_or_expose_private_details(self):
        with patch.object(self.board, "context", side_effect=alexa.BoardError()):
            response = self.call("AddDinnerTomorrowIntent", {"Meal": "Pasta"})
        self.assertIn("could not connect", speech(response))
        self.assertFalse(self.board.calls)
        for meal in ("", "x" * 161, "private\nmeal"):
            with self.subTest(meal_length=len(meal)):
                response = self.call("AddDinnerTodayIntent", {"Meal": meal})
                self.assertFalse(self.board.calls)
                if meal:
                    self.assertNotIn(meal, speech(response))
        with patch.object(self.board, "save", side_effect=alexa.BoardError(retryable=False)):
            response = self.call("AddDinnerTodayIntent", {"Meal": "Pasta"})
        self.assertIn("could not accept", speech(response))
        self.assertNotIn("Saved", speech(response))

    def test_timed_out_save_retries_original_id_without_duplicate(self):
        self.board.fail_after_save = True
        first = self.call(slots={"Meal": "chicken"})
        self.assertIn("could not confirm whether", speech(first))
        result = self.call("AMAZON.YesIntent", previous=first, request_id="request.retry")
        self.assertEqual(len(self.board.calls), 2)
        self.assertEqual(self.board.calls[0], self.board.calls[1])
        self.assertEqual([item["created"] for item in self.board.results], [True, False])
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertIn("Already saved", speech(result))

    def test_cancel_after_uncertain_save_does_not_claim_nothing_was_saved(self):
        self.board.fail_after_save = True
        response = self.call(slots={"Meal": "chicken"})
        result = self.call("AMAZON.NoIntent", previous=response)
        self.assertIn("earlier save may have completed", speech(result))
        self.assertEqual(len(self.board.calls), 1)

    def test_replayed_replaced_dinner_does_not_claim_it_was_restored(self):
        self.call(slots={"Meal": "chicken"}, request_id="dinner.first")
        self.call(slots={"Meal": "pasta"}, request_id="dinner.second")
        result = self.call(slots={"Meal": "chicken"}, request_id="dinner.first")
        self.assertIn("already replaced or cancelled", speech(result))
        self.assertEqual([item["title"] for item in self.store.list_items()], ["pasta"])

    def test_saving_while_personal_display_disabled_says_cards_are_hidden(self):
        self.board.display_enabled = False
        response = self.call(slots={"Meal": "teriyaki chicken"})
        self.assertIn("Saved", speech(response))
        self.assertIn("hidden on the display", speech(response))

    def test_title_markup_is_plain_text_and_nothing_is_logged(self):
        title = "eye check <audio src='no'/> & review"
        output = io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", output):
            response = self.appointment(Title=title)
        self.assertEqual(response["response"]["outputSpeech"]["type"], "PlainText")
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("ssml", response["response"]["outputSpeech"])

    def test_bad_skill_id_or_conflicting_ids_never_contact_board(self):
        envelopes = [event(), event(), event()]
        envelopes[0]["context"]["System"]["application"]["applicationId"] = "amzn1.ask.skill.other"
        envelopes[1]["session"]["application"]["applicationId"] = "amzn1.ask.skill.other"
        del envelopes[2]["context"]["System"]["application"]
        del envelopes[2]["session"]["application"]
        for envelope in envelopes:
            response = alexa.lambda_handler(envelope, None)
            self.assertIn("not authorized", speech(response))
        alexa.BoardClient.assert_not_called()

    def test_help_cancel_and_yes_without_pending_never_write(self):
        for name in ("AMAZON.HelpIntent", "AMAZON.CancelIntent", "AMAZON.YesIntent"):
            self.call(name)
        self.assertFalse(self.board.calls)
        alexa.BoardClient.assert_not_called()


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


class AlexaHttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"ALEXA_SKILL_ID": SKILL_ID,
            "FLIGHTWALL_BASE_URL": "https://board.example.test",
            "FLIGHTWALL_ALEXA_TOKEN": "a-dedicated-voice-token-with-enough-entropy"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_only_dedicated_bearer_goes_to_exact_https_endpoint(self):
        with patch.object(alexa, "build_opener") as opener:
            opener.return_value.open.return_value = Response(json.dumps(CONTEXT).encode(), "https://board.example.test/api/v1/voice-context")
            self.assertEqual(alexa.BoardClient().context(), CONTEXT)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://board.example.test/api/v1/voice-context")
            self.assertEqual(request.get_header("Authorization"), "Bearer " + os.environ["FLIGHTWALL_ALEXA_TOKEN"])
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 2.5)

    def test_idempotent_created_false_response_is_accepted(self):
        with patch.object(alexa, "build_opener") as opener:
            opener.return_value.open.return_value = Response(b'{"created":false,"item":{"id":"existing"}}', "https://board.example.test/api/v1/personal-items")
            result = alexa.BoardClient().save({"kind": "dinner"})
            self.assertFalse(result["created"])
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")

    def test_redirects_are_refused_including_same_host(self):
        self.assertIsNone(alexa._NoRedirect().redirect_request(Request("https://board.example.test/api"), None, 302,
                                                             "redirect", {}, "https://other.example.test/api"))

    def test_base_url_rejects_http_credentials_and_non_origin_paths(self):
        for value in ("http://board.example.test", "https://user:secret@board.example.test",
                      "https://board.example.test/api", "https://board.example.test/?secret=value"):
            with self.subTest(value=value), patch.dict(os.environ, {"FLIGHTWALL_BASE_URL": value}):
                with self.assertRaises(alexa.BoardError):
                    alexa.BoardClient()

    def test_oversized_or_invalid_context_is_rejected(self):
        for raw in (b"x" * (alexa.MAX_RESPONSE_BYTES + 1), b"not JSON", b'{"timezone":"bad"}'):
            with self.subTest(length=len(raw)), patch.object(alexa, "build_opener") as opener:
                opener.return_value.open.return_value = Response(raw, "https://board.example.test/api/v1/voice-context")
                with self.assertRaises(alexa.BoardError):
                    alexa.BoardClient().context()

    def test_safe_board_validation_message_is_available_without_server_details(self):
        body = b'{"error":"That appointment has already ended. Check the date and local time, then try again."}'
        with patch.object(alexa, "build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://board.example.test/api", 400, "bad", {}, io.BytesIO(body))
            with self.assertRaises(alexa.BoardError) as error:
                alexa.BoardClient().save({})
            self.assertIn("already ended", error.exception.public_message)
            self.assertFalse(error.exception.retryable)

    def test_unsafe_error_and_unexpected_network_exception_do_not_leak(self):
        for body in (b'{"error":"https://example.test/private?secret=value"}', b'{"error":"Bearer my-private-key"}'):
            with patch.object(alexa, "build_opener") as opener:
                opener.return_value.open.side_effect = HTTPError("https://board.example.test/api", 400, "bad", {}, io.BytesIO(body))
                with self.assertRaises(alexa.BoardError) as error:
                    alexa.BoardClient().save({})
                self.assertIsNone(error.exception.public_message)
                self.assertNotIn("private", str(error.exception))
        with patch.object(alexa, "build_opener") as opener:
            opener.return_value.open.side_effect = OSError("secret connection detail")
            with self.assertRaises(alexa.BoardError) as error:
                alexa.BoardClient().context()
            self.assertNotIn("secret", str(error.exception))


class AlexaModelTests(unittest.TestCase):
    def test_day_specific_dinner_phrases_select_distinct_intents(self):
        path = Path(__file__).resolve().parents[1] / "integrations" / "alexa" / "interaction-model-en-US.json"
        intents = {intent["name"]: intent for intent in json.loads(path.read_text())["interactionModel"]["languageModel"]["intents"]}
        for day, name in (("today", "AddDinnerTodayIntent"), ("tomorrow", "AddDinnerTomorrowIntent")):
            with self.subTest(day=day):
                intent = intents[name]
                self.assertEqual(intent["slots"], [{"name": "Meal", "type": "AMAZON.SearchQuery"}])
                self.assertIn(f"set dinner {day} to {{Meal}}", intent["samples"])
                self.assertIn(f"dinner {day} is {{Meal}}", intent["samples"])
                self.assertIn(f"{{Meal}} is for dinner {day}", intent["samples"])
                self.assertIn(f"dinner {day}", intent["samples"])
                self.assertTrue(all(day in sample for sample in intent["samples"]))

    def test_model_does_not_combine_searchquery_with_other_slots_in_a_sample(self):
        path = Path(__file__).resolve().parents[1] / "integrations" / "alexa" / "interaction-model-en-US.json"
        model = json.loads(path.read_text())["interactionModel"]["languageModel"]
        self.assertEqual(model["invocationName"], "flight wall")
        for intent in model["intents"]:
            names = {slot["name"]: slot["type"] for slot in intent.get("slots", [])}
            query = {name for name, kind in names.items() if kind == "AMAZON.SearchQuery"}
            self.assertLessEqual(len(query), 1)
            for sample in intent.get("samples", []):
                slots = set(re.findall(r"\{([^}]+)\}", sample))
                self.assertTrue(slots <= set(names), (intent["name"], sample))
                if slots & query:
                    self.assertEqual(len(slots), 1, sample)
                    self.assertTrue(re.sub(r"\{[^}]+\}", "", sample).strip(), sample)


if __name__ == "__main__":
    unittest.main()
