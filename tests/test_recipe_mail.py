"""Selected recipes only, with no real credentials or delivery."""
from contextlib import nullcontext
import json
from unittest.mock import MagicMock

import pytest

from flightwall.config import private_json
from flightwall.recipe_mail import RecipeMail


SETTINGS = {"sender": "example-sender@gmail.com", "recipient": "example-recipient@example.net",
            "app_password": "syntheticonlyabc"}
OPTION = {"number": 3, "title": "Test noodles", "minutes": 30, "servings": 8, "sides": "Broccoli",
          "ingredients": ["8 cups dry noodles"], "steps": ["Cook until tender."]}


@pytest.mark.parametrize("fail", [False, True])
def test_recipe_dispatch_reserves_once_and_strips_bodies_even_if_delivery_uncertain(tmp_path, fail):
    smtp_factory = MagicMock()
    smtp = smtp_factory.return_value.__enter__.return_value
    smtp.send_message.return_value = {}
    if fail:
        smtp.send_message.side_effect = OSError("Synthetic connection failure")
    queue = RecipeMail(tmp_path, nullcontext, smtp_factory=smtp_factory)
    private_json(queue.credentials, SETTINGS)
    queue.queue(OPTION, "2026-11-04", "synthetic")
    queue.queue(OPTION, "2026-11-04", "synthetic")
    smtp_factory.assert_not_called()
    if fail:
        with pytest.raises(OSError):
            queue.poll()
    else:
        queue.poll()
    queue.poll()
    queue.queue(OPTION, "2026-11-04", "synthetic")
    smtp.send_message.assert_called_once()
    msg = smtp.send_message.call_args.args[0]
    assert str(msg["To"]) == SETTINGS["recipient"]
    assert "8 cups dry noodles" in msg.get_content() and "Cook until tender." in msg.get_content()
    ledger = json.loads(queue.outbox.read_text())
    assert len(ledger) == 1
    assert next(iter(ledger.values()))["status"] == ("unconfirmed" if fail else "accepted")
    assert "noodles" not in json.dumps(ledger) and SETTINGS["recipient"] not in json.dumps(ledger)
    status = queue.status()
    assert status["recent"]["unconfirmed" if fail else "accepted"] == 1
    assert SETTINGS["recipient"] not in json.dumps(status)


def test_switching_recipient_does_not_send_prior_queued_recipe(tmp_path):
    smtp_factory = MagicMock()
    queue = RecipeMail(tmp_path, nullcontext, smtp_factory=smtp_factory)
    private_json(queue.credentials, SETTINGS)
    queue.queue(OPTION, "2026-11-04", "synthetic")
    private_json(queue.credentials, SETTINGS | {"recipient": "different@example.net"})
    queue.poll()
    smtp_factory.assert_not_called()


def test_switching_recipient_while_authenticating_cancels_dispatch(tmp_path):
    smtp_factory = MagicMock()
    smtp = smtp_factory.return_value.__enter__.return_value
    queue = RecipeMail(tmp_path, nullcontext, smtp_factory=smtp_factory)
    private_json(queue.credentials, SETTINGS)
    queue.queue(OPTION, "2026-11-04", "synthetic")
    smtp.login.side_effect = lambda *_: private_json(queue.credentials, SETTINGS | {"recipient": "new@example.net"})
    queue.poll()
    smtp.send_message.assert_not_called()
