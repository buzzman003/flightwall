"""Synthetic spoken selection crosses the real dedicated-voice HTTP route."""
from datetime import datetime, timezone
import json
import socket
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from flightwall.backend import Runtime
from flightwall.config import ConfigStore, private_json
from integrations.alexa import lambda_function as alexa


NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
GENERATION = "d4" * 12
SKILL = "amzn1.ask.skill.synthetic-option-http"


def test_selection_maps_actual_daily_option_and_voice_context_never_exposes_recipes(tmp_path):
    data = tmp_path / "data"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ConfigStore(data).update({"timezone": "America/Chicago", "alexa_enabled": True, "control_port": port,
                              "demo": False, "dinner_options_enabled": True,
                              "dinner_options_monthly_limit_usd": 3})
    rows = [{"number": index, "title": f"Fictional choice {index}", "sides": "Fictional peas",
             "minutes": 30 if index < 4 else 55, "servings": 8,
             "ingredients": ["8 fictional servings"], "steps": ["Fictional cooking method"],
             "image_data": None} for index in range(1, 5)]
    private_json(data / "dinner-options.json", {"schema": 1, "date": "2026-11-01", "generation": GENERATION,
                                               "attempted_at": NOW.isoformat(), "generated_at": NOW.isoformat(),
                                               "status": "ready", "options": rows})
    private_json(data / "recipe-mail.json", {"sender": "synthetic-sender@gmail.com",
                                           "recipient": "synthetic-recipient@example.net",
                                           "app_password": "syntheticonlyabc"})
    smtp_factory = MagicMock()
    smtp = smtp_factory.return_value.__enter__.return_value
    smtp.send_message.return_value = {}
    real_opener = build_opener(ProxyHandler({}))
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
            if request.data:
                writes.append((path, json.loads(request.data)))
            local = Request(f"http://127.0.0.1:{port}" + path, data=request.data,
                            headers=dict(request.header_items()), method=request.get_method())
            return Reply(real_opener.open(local, timeout=timeout), request.full_url)

    with patch("flightwall.dinner_options.request_json") as paid_provider, \
            patch.object(Runtime, "_run_recipe_mail", return_value=None), \
            patch("flightwall.dinner_options._now", side_effect=lambda value=None: NOW if value is None else value.astimezone(timezone.utc)), \
            patch("flightwall.personal._now", return_value=NOW), patch.object(alexa, "_now", return_value=NOW):
        runtime = Runtime(data, tmp_path / "install").start()
        runtime.recipe_mail.smtp_factory = smtp_factory
        token = runtime.remote.alexa_token_path.read_text().strip()
        environment = {"ALEXA_SKILL_ID": SKILL, "FLIGHTWALL_BASE_URL": "https://board.example",
                       "FLIGHTWALL_ALEXA_TOKEN": token}
        try:
            with patch.dict("os.environ", environment), patch.object(alexa, "build_opener", return_value=Transport()):
                context = alexa.BoardClient().context()
                assert context["dinner_options_date"] == "2026-11-01"
                assert context["dinner_options_generation"] == GENERATION
                assert not {"options", "recipes", "presence", "kids", "title", "image_data"}.intersection(context)
                envelope = {"context": {"System": {"application": {"applicationId": SKILL}}},
                            "request": {"type": "IntentRequest", "locale": "en-US", "requestId": "fictional-choice-request",
                                        "intent": {"name": "SelectDinnerOptionIntent", "slots": {"OptionNumber": {"value": "3"}}}}}
                response = alexa.lambda_handler(envelope, None)
                assert "Saved Fictional choice 3" in response["response"]["outputSpeech"]["text"]
                assert writes == [("/api/v1/dinner-options/select", {"option": 3, "date": "2026-11-01",
                                  "generation": GENERATION, "request_id": "fictional-choice-request"})]
                replay = alexa.lambda_handler(envelope, None)
                assert "Already saved" in replay["response"]["outputSpeech"]["text"]
                items = runtime.personal.list_items(NOW)
                assert len(items) == 1 and items[0]["title"] == rows[2]["title"]
                assert items[0]["sides"] == rows[2]["sides"]
                assert runtime.recipe_mail.status()["recent"]["queued"] == 1
                smtp_factory.assert_not_called()
                runtime.recipe_mail.poll()
                runtime.recipe_mail.poll()
                smtp.send_message.assert_called_once()
                message = smtp.send_message.call_args.args[0]
                assert str(message["To"]) == "synthetic-recipient@example.net"
                assert rows[2]["title"] in message["Subject"]
                assert rows[2]["ingredients"][0] in message.get_content()
                assert rows[2]["steps"][0] in message.get_content()
                assert runtime.recipe_mail.status()["recent"]["accepted"] == 1

                # Arbitrary caller labels and wrong-generation selections must
                # not replace the actual board-bound meal through this route.
                payload = writes[0][1]
                for changed in (payload | {"title": "Fictional forged label"},
                                payload | {"generation": "e5" * 12}):
                    request = Request(f"http://127.0.0.1:{port}/api/v1/dinner-options/select",
                                      data=json.dumps(changed).encode(),
                                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                    try:
                        real_opener.open(request, timeout=3)
                        raise AssertionError("Malformed selection was accepted")
                    except HTTPError as error:
                        assert error.code == 400
                        error.close()
                assert runtime.personal.list_items(NOW) == items
                paid_provider.assert_not_called()
        finally:
            runtime.stop()
