"""Profile selection is fixed before any private file or provider is touched."""
import json
from unittest.mock import patch

import pytest

from flightwall.main import main


@pytest.mark.parametrize("person_args,person", [([], "chris"), (["--person", "chris"], "chris"), (["--person", "lisa"], "lisa")])
def test_gmail_authorize_cli_selects_fixed_profile_and_preserves_default(tmp_path, person_args, person, capsys):
    with patch("flightwall.gmail_oauth.authorize_gmail", return_value={"authorized": True, "person": person}) as authorize:
        result = main(["--data-dir", str(tmp_path), "gmail-authorize", "--credentials", "desktop.json", "--no-browser", *person_args])
    assert result == 0
    assert authorize.call_args.kwargs["person"] == person
    assert authorize.call_args.kwargs["open_browser"] is False
    assert json.loads(capsys.readouterr().out)["person"] == person


@pytest.mark.parametrize("person", ["other", "../chris", "lisa/../../chris"])
@pytest.mark.parametrize("command", ["gmail-authorize", "gmail-profile-check"])
def test_cli_invalid_profile_exits_before_private_store_or_provider(tmp_path, person, command):
    required = ["--credentials", "desktop.json"] if command == "gmail-authorize" else ["--expected-email", "lisa@example.test"]
    with patch("flightwall.main.ConfigStore") as store, patch("flightwall.gmail_oauth.GmailClient") as client:
        with pytest.raises(SystemExit) as error:
            main(["--data-dir", str(tmp_path), command, "--person", person, *required])
    assert error.value.code == 2
    store.assert_not_called()
    client.assert_not_called()


@pytest.mark.parametrize("matched,exit_code", [(True, 0), (False, 1)])
def test_profile_check_cli_emits_only_safe_result_and_mismatch_fails_closed(tmp_path, matched, exit_code, capsys):
    result = {"person": "lisa", "matches": matched, "read_only": True}
    with patch("flightwall.gmail_oauth.check_gmail_profile", return_value=result) as profile:
        code = main(["--data-dir", str(tmp_path), "gmail-profile-check", "--person", "lisa", "--expected-email", "lisa@example.test"])
    assert code == exit_code
    profile.assert_called_once_with(str(tmp_path), "lisa@example.test", person="lisa")
    output = capsys.readouterr()
    assert json.loads(output.out) == result
    assert "@" not in output.out + output.err
