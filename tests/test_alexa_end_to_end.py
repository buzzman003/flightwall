"""Exercise the Lambda conversation through the actual household HTTP backend.

The test transport maps the configured HTTPS origin to loopback. It verifies
application behavior and credential scope; it does not verify public TLS/AWS.
"""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from flightwall.backend import Runtime
from flightwall.config import ConfigStore

MODULE_PATH = Path(__file__).resolve().parents[1] / "integrations" / "alexa" / "lambda_function.py"
spec = importlib.util.spec_from_file_location("flightwall_alexa_e2e", MODULE_PATH)
alexa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alexa)
NOW = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)
SKILL = "amzn1.ask.skill.flightwall-test"


def event(intent, request_id, slots=None, attributes=None):
    return {"context": {"System": {"application": {"applicationId": SKILL}}},
            "session": {"application": {"applicationId": SKILL}, "attributes": attributes or {}},
            "request": {"type": "IntentRequest", "locale": "en-US", "requestId": request_id,
                        "intent": {"name": intent, "slots": {key: {"value": value} for key, value in (slots or {}).items()}}}}


def dinner_without_optional_details(envelope):
    response = alexa.lambda_handler(envelope, None)
    draft = response.get("sessionAttributes", {}).get("pending", {})
    if draft.get("kind") == "dinner" and draft.get("phase") in {"sides", "ready"}:
        response = alexa.lambda_handler(event("SaveDinnerIntent", "optional-save-turn",
                                              attributes=response["sessionAttributes"]), None)
    return response


class AlexaBoardEndToEndTests(unittest.TestCase):
    def test_timers_shopping_and_public_watch_over_actual_scoped_http(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            ConfigStore(data).update({"timezone": "America/Chicago", "alexa_enabled": True, "control_port": port})
            runtime = Runtime(data, Path(folder) / "install").start()
            real_opener = build_opener(ProxyHandler({}))
            paths = []

            class Reply:
                def __init__(self, response, original):
                    self.response, self.original, self.status = response, original, response.status
                def read(self, size):
                    return self.response.read(size)
                def geturl(self):
                    return self.original
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    self.response.close()

            class Transport:
                def open(self, request, timeout):
                    path = urlsplit(request.full_url).path
                    paths.append((request.get_method(), path))
                    local = Request(f"http://127.0.0.1:{port}" + path, data=request.data,
                                    headers=dict(request.header_items()), method=request.get_method())
                    return Reply(real_opener.open(local, timeout=timeout), request.full_url)

            environment = {"ALEXA_SKILL_ID": SKILL, "FLIGHTWALL_BASE_URL": "https://board.example",
                           "FLIGHTWALL_ALEXA_TOKEN": runtime.remote.alexa_token_path.read_text().strip()}
            try:
                with patch.dict("os.environ", environment), patch.object(alexa, "build_opener", return_value=Transport()), \
                        patch.object(alexa, "_now", return_value=NOW), patch("flightwall.timers._now", return_value=NOW), \
                        patch("flightwall.flight_watches._now", return_value=NOW), patch("flightwall.feeds.request_json") as provider:
                    timer_event = event("StartTimerIntent", "timer-start", {"TimerLabel": "synthetic pasta", "TimerDuration": "PT5M"})
                    response = alexa.lambda_handler(timer_event, None)
                    self.assertIn("Started", response["response"]["outputSpeech"]["text"])
                    self.assertEqual(len(runtime.timers.list_timers(NOW)), 1)
                    alexa.lambda_handler(timer_event, None)
                    self.assertEqual(len(runtime.timers.list_timers(NOW)), 1)
                    listed = alexa.lambda_handler(event("ListTimersIntent", "timer-list"), None)
                    self.assertIn("synthetic pasta", listed["response"]["outputSpeech"]["text"])
                    alexa.lambda_handler(event("CancelTimerIntent", "timer-cancel", {"TimerNumber": "1"}), None)
                    self.assertEqual(runtime.timers.list_timers(NOW), [])

                    for index in range(5):
                        started = alexa.lambda_handler(event("StartTimerIntent", "limit-start-" + str(index),
                            {"TimerLabel": "Tea", "TimerDuration": "PT5M"}), None)
                        self.assertIn("Started", started["response"]["outputSpeech"]["text"])
                    limited = alexa.lambda_handler(event("StartTimerIntent", "limit-rejected",
                        {"TimerLabel": "Pasta", "TimerDuration": "PT5M"}), None)
                    self.assertIn("Five timers are already running", limited["response"]["outputSpeech"]["text"])
                    self.assertTrue(limited["response"]["shouldEndSession"])
                    self.assertEqual(len(runtime.timers.list_timers(NOW)), 5)
                    runtime.config.update({"timers_enabled": False})
                    disabled = alexa.lambda_handler(event("StartTimerIntent", "disabled-rejected",
                        {"TimerLabel": "Pasta", "TimerDuration": "PT5M"}), None)
                    self.assertIn("TV timers are disabled", disabled["response"]["outputSpeech"]["text"])
                    self.assertTrue(disabled["response"]["shouldEndSession"])
                    self.assertEqual(len(runtime.timers.list_timers(NOW)), 5)

                    add = event("AddShoppingItemIntent", "shopping-add", {"Item": "synthetic olives"})
                    response = alexa.lambda_handler(add, None)
                    self.assertIn("Added", response["response"]["outputSpeech"]["text"])
                    alexa.lambda_handler(add, None)
                    self.assertEqual(len(runtime.get_shopping_list()["items"]), 1)
                    show = alexa.lambda_handler(event("ShowShoppingListIntent", "shopping-show"), None)
                    self.assertIn("synthetic olives", show["response"]["outputSpeech"]["text"])
                    self.assertTrue(runtime.get_shopping_list()["visible"])
                    prompt = alexa.lambda_handler(event("ClearShoppingListIntent", "shopping-clear"), None)
                    self.assertEqual(len(runtime.get_shopping_list()["items"]), 1)
                    alexa.lambda_handler(event("AMAZON.YesIntent", "clear-confirm", attributes=prompt["sessionAttributes"]), None)
                    self.assertEqual(runtime.get_shopping_list()["items"], [])

                    begin = event("TrackFlightIntent", "watch-one", {"Airline": "Allegiant", "FlightNumber": "123", "Date": "2026-10-22"})
                    prompt = alexa.lambda_handler(begin, None)
                    self.assertEqual(runtime.flight_watches.list_watches(NOW), [])
                    confirmation = event("AMAZON.YesIntent", "watch-confirm", attributes=prompt["sessionAttributes"])
                    saved = alexa.lambda_handler(confirmation, None)
                    self.assertIn("not configured", saved["response"]["outputSpeech"]["text"])
                    alexa.lambda_handler(confirmation, None)
                    watches = runtime.flight_watches.list_watches(NOW)
                    self.assertEqual(len(watches), 1)
                    self.assertEqual(watches[0]["ident"], "G4123")
                    provider.assert_not_called()
                    self.assertEqual(runtime.personal.list_items(NOW), [])
                    self.assertFalse((data / "google-token.json").exists())
                    self.assertTrue(all(path in {"/api/v1/voice-context", "/api/v1/timers", "/api/v1/timers/cancel",
                                                "/api/v1/flight-watches", "/api/v1/shopping-list", "/api/v1/shopping-list/add",
                                                "/api/v1/shopping-list/show", "/api/v1/shopping-list/clear"} for _, path in paths))
            finally:
                runtime.stop()

    def test_dinner_replacement_and_confirmed_appointment_over_actual_http(self):
        with tempfile.TemporaryDirectory() as folder:
            data, install = Path(folder) / "data", Path(folder) / "install"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            ConfigStore(data).update({"timezone": "America/New_York", "alexa_enabled": True, "control_port": port})
            runtime = Runtime(data, install).start()
            token = runtime.remote.alexa_token_path.read_text().strip()
            real_opener = build_opener(ProxyHandler({}))
            requests = []

            class Reply:
                def __init__(self, response, original):
                    self.response, self.original = response, original
                    self.status = response.status
                def read(self, size):
                    return self.response.read(size)
                def geturl(self):
                    return self.original
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    self.response.close()

            class Transport:
                def open(self, request, timeout):
                    path = urlsplit(request.full_url).path
                    requests.append((request.get_method(), path))
                    local = Request(f"http://127.0.0.1:{port}" + path, data=request.data,
                                    headers=dict(request.header_items()), method=request.get_method())
                    return Reply(real_opener.open(local, timeout=timeout), request.full_url)

            environment = {"ALEXA_SKILL_ID": SKILL, "FLIGHTWALL_BASE_URL": "https://board.example", "FLIGHTWALL_ALEXA_TOKEN": token}
            try:
                with patch.dict("os.environ", environment), patch.object(alexa, "build_opener", return_value=Transport()), \
                        patch.object(alexa, "_now", return_value=NOW), patch("flightwall.personal._now", return_value=NOW):
                    meal = event("AddDinnerTodayIntent", "dinner-one", {"Meal": "Teriyaki chicken"})
                    response = dinner_without_optional_details(meal)
                    self.assertIn("Teriyaki chicken", response["response"]["outputSpeech"]["text"])
                    visible = runtime.personal.visible(NOW)
                    self.assertEqual(len(visible), 1)
                    self.assertEqual(visible[0]["source"], "alexa")
                    dinner_without_optional_details(meal)
                    self.assertEqual(len(runtime.personal.visible(NOW)), 1)
                    dinner_without_optional_details(event("AddDinnerIntent", "dinner-two", {"Meal": "Pizza"}))
                    self.assertEqual([x["title"] for x in runtime.personal.visible(NOW)], ["Pizza"])
                    replay = dinner_without_optional_details(meal)
                    self.assertIn("cancel", replay["response"]["outputSpeech"]["text"].lower())
                    self.assertEqual([x["title"] for x in runtime.personal.visible(NOW)], ["Pizza"])

                    tomorrow = event("AddDinnerTomorrowIntent", "dinner-tomorrow", {"Meal": "Soup & rice"})
                    response = dinner_without_optional_details(tomorrow)
                    self.assertIn("Saved Soup & rice for dinner tomorrow.", response["response"]["outputSpeech"]["text"])
                    self.assertTrue(response["response"]["outputSpeech"]["text"].endswith("Anything else?"))
                    dinner_without_optional_details(tomorrow)
                    tomorrow_item = next(item for item in runtime.personal.list_items(NOW) if item["title"] == "Soup & rice")
                    self.assertEqual(tomorrow_item["start"], "2026-10-23T18:30:00.000000Z")
                    self.assertEqual(tomorrow_item["end"], "2026-10-23T23:00:00.000000Z")
                    self.assertEqual([x["title"] for x in runtime.personal.visible(NOW)], ["Pizza", "Soup & rice"])

                    begin = event("AddAppointmentIntent", "appointment-one", {"Title": "Emerson doctor appointment", "Date": "2026-11-03", "Time": "11:25"})
                    prompt = alexa.lambda_handler(begin, None)
                    self.assertFalse(prompt["response"]["shouldEndSession"])
                    self.assertIn("Should I save", prompt["response"]["outputSpeech"]["text"])
                    self.assertEqual(len(runtime.personal.list_items(NOW)), 2)
                    confirmation = event("AMAZON.YesIntent", "appointment-yes", attributes=prompt["sessionAttributes"])
                    saved = alexa.lambda_handler(confirmation, None)
                    self.assertFalse(saved["response"]["shouldEndSession"])
                    items = runtime.personal.list_items(NOW)
                    appointment = next(item for item in items if item["kind"] == "appointment")
                    self.assertEqual(appointment["display_end"], appointment["end"])
                    self.assertEqual(appointment["end"], "2026-11-03T17:25:00.000000Z")
                    self.assertEqual(len(runtime.personal.visible(datetime(2026, 11, 3, 17, 24, tzinfo=timezone.utc))), 1)
                    self.assertEqual(runtime.personal.visible(datetime(2026, 11, 3, 17, 25, tzinfo=timezone.utc)), [])
                    self.assertTrue(all(path in {"/api/v1/voice-context", "/api/v1/personal-items"} for _, path in requests))
                    self.assertFalse((data / "google-token.json").exists())
            finally:
                runtime.stop()

    def test_dinner_details_make_one_scoped_post_and_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            ConfigStore(data).update({"timezone": "America/Chicago", "alexa_enabled": True, "control_port": port})
            runtime = Runtime(data, Path(folder) / "install").start()
            token = runtime.remote.alexa_token_path.read_text().strip()
            opener = build_opener(ProxyHandler({}))
            writes = []

            class Reply:
                def __init__(self, response, original):
                    self.response, self.original, self.status = response, original, response.status
                def read(self, size): return self.response.read(size)
                def geturl(self): return self.original
                def __enter__(self): return self
                def __exit__(self, *_): self.response.close()

            class Transport:
                def open(self, request, timeout):
                    path = urlsplit(request.full_url).path
                    if path == "/api/v1/personal-items":
                        writes.append(json.loads(request.data))
                    local = Request(f"http://127.0.0.1:{port}" + path, data=request.data,
                                    headers=dict(request.header_items()), method=request.get_method())
                    return Reply(opener.open(local, timeout=timeout), request.full_url)

            environment = {"ALEXA_SKILL_ID": SKILL, "FLIGHTWALL_BASE_URL": "https://board.example", "FLIGHTWALL_ALEXA_TOKEN": token}
            try:
                with patch.dict("os.environ", environment), patch.object(alexa, "build_opener", return_value=Transport()), \
                        patch.object(alexa, "_now", return_value=NOW), patch("flightwall.personal._now", return_value=NOW):
                    meal = alexa.lambda_handler(event("AddDinnerTomorrowIntent", "detailed-dinner", {"Meal": "Tacos"}), None)
                    self.assertEqual(writes, [])
                    side = alexa.lambda_handler(event("SetDinnerSidesIntent", "side-turn", {"Sides": "rice and beans"}, meal["sessionAttributes"]), None)
                    self.assertEqual(writes, [])
                    ready = event("SetDinnerReadyWindowIntent", "ready-turn", {"ReadyStart": "18:45", "ReadyEnd": "19:15"}, side["sessionAttributes"])
                    saved = alexa.lambda_handler(ready, None)
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(writes[0]["request_id"], "detailed-dinner")
                    self.assertEqual(writes[0]["ready_window_end"], "19:15")
                    self.assertIn("6:45 P.M. and 7:15 P.M.", saved["response"]["outputSpeech"]["text"])
                    self.assertNotIn("token", json.dumps(saved).lower())
                    repeated = alexa.lambda_handler(ready, None)
                    self.assertEqual(writes[0], writes[1])
                    self.assertIn("Already saved", repeated["response"]["outputSpeech"]["text"])
                    items = runtime.personal.list_items(NOW)
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["sides"], "rice and beans")
                    self.assertEqual(items[0]["ready_at"], "2026-10-23T23:45:00.000000Z")
                    self.assertEqual(items[0]["ready_until"], "2026-10-24T00:15:00.000000Z")
                    self.assertEqual(items[0]["display_end"], "2026-10-24T00:00:00.000000Z")
            finally:
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
