"""Synthetic Desktop Gmail consent; no real Google account or credentials."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import stat
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from flightwall.gmail_oauth import AUTH_URL, authorize_gmail, check_gmail_profile
from flightwall.messages import GmailClient, GMAIL_SCOPE, MessageError, TOKEN_URL


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "private"
    account = data / "accounts" / "chris"
    account.mkdir(parents=True)
    credentials = tmp_path / "desktop.json"
    credentials.write_text(json.dumps({"installed": {
        "client_id": "synthetic-client.apps.googleusercontent.com", "client_secret": "synthetic-secret",
        "token_uri": "https://untrusted.example.invalid/token",
        "auth_uri": "https://untrusted.example.invalid/auth"}}))
    old = {account / "gmail-client.json": b'{"old":"gmail-client"}\n',
           account / "gmail-token.json": b'{"old":"gmail-token"}\n',
           account / "google-client.json": b'{"calendar":"client"}\n',
           account / "google-token.json": b'{"calendar":"token"}\n',
           data / "config.json": b'{"messages_enabled":false,"delta_travel_enabled":false}\n'}
    for path, content in old.items():
        path.write_bytes(content)
    return data, account, credentials, old


def response(**changes):
    return {"access_token": "synthetic-gmail-access", "refresh_token": "synthetic-gmail-refresh",
            "scope": GMAIL_SCOPE, "token_type": "Bearer", "expires_in": 3600} | changes


def driver(actions=None):
    query, replies, threads = {}, [], []

    def on_url(url):
        assert url.startswith(AUTH_URL + "?")
        query.update(parse_qs(urlsplit(url).query))
        redirect = urlsplit(query["redirect_uri"][0])

        def browser():
            for action in actions or [{"code": "synthetic-one-use-code"}]:
                values = {"state": query["state"][0]} | action
                host = values.pop("host", f"127.0.0.1:{redirect.port}")
                path = values.pop("path", "/oauth2/callback")
                client = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=3)
                client.request("GET", path + "?" + urlencode(values), headers={"Host": host})
                result = client.getresponse()
                replies.append((result.status, result.read(), dict(result.getheaders())))
                client.close()

        thread = threading.Thread(target=browser)
        thread.start()
        threads.append(thread)

    return on_url, query, replies, threads


def join(threads):
    for thread in threads:
        thread.join(timeout=4)
        assert not thread.is_alive()


@pytest.mark.parametrize("identity, expected", [
    ({"email": "OWNER@example.test", "verified_email": True}, True),
    ({"email": "other@example.test", "verified_email": True}, False),
    ({"email": "owner@example.test", "verified_email": False}, False),
])
def test_tasks_desktop_consent_keeps_existing_calendar_and_mail_credentials(setup, identity, expected):
    from flightwall.agenda_google_tasks import TasksClient
    from flightwall.gmail_oauth import authorize_tasks
    data, account, credentials, original = setup
    calendar_token = account / "google-token.json"
    original[calendar_token] = b'{"refresh_token":"synthetic-original-calendar-grant"}'
    calendar_token.write_bytes(original[calendar_token])
    prior_token = account / "tasks-token.json"
    prior_token.write_text('{"prior":"tasks"}')
    on_url, query, _, threads = driver()
    with (patch.object(TasksClient, "_request", return_value=response(scope=TasksClient.scope + " email")),
          patch("flightwall.agenda_google_tasks._GoogleAccountInfo._request", return_value=identity) as verify,
          patch("flightwall.agenda_google_tasks.CalendarClient._api", return_value={"id": "owner@example.test", "primary": True}) as primary):
        if expected:
            result = authorize_tasks(data, credentials, False, callback_port=0, timeout=3, on_url=on_url)
            assert result["person"] == "chris" and result["scopes"] == list(TasksClient.scopes)
            assert TasksClient(account).connected()
        else:
            with pytest.raises(MessageError, match="same Google account"):
                authorize_tasks(data, credentials, False, callback_port=0, timeout=3, on_url=on_url)
            assert prior_token.read_text() == '{"prior":"tasks"}'
    verify.assert_called_once_with("GET", "https://www.googleapis.com/oauth2/v2/userinfo?fields=email,verified_email",
                                   token="synthetic-gmail-access")
    primary.assert_called_once_with("/users/me/calendarList/primary", {"fields": "id,primary"})
    join(threads)
    assert query["scope"] == [" ".join(TasksClient.scopes)]
    assert all(path.read_bytes() == content for path, content in original.items())
    if expected:
        assert "@example.test" not in (account / "tasks-client.json").read_text() + prior_token.read_text()
        calendar_token.write_text('{"refresh_token":"synthetic-another-calendar"}')
        assert not TasksClient(account).connected()


def test_loopback_checks_host_state_and_pkce_and_saves_only_separate_gmail_files(setup, capsys):
    data, account, credentials, old = setup
    on_url, query, replies, threads = driver([
        {"host": "untrusted.example.invalid", "code": "forged"},
        {"state": "wrong", "code": "forged"},
        {"path": "/wrong", "code": "forged"},
        {"code": "synthetic-one-use-code"},
    ])
    captured = {}

    def exchange(method, url, **kwargs):
        assert (method, url) == ("POST", TOKEN_URL)
        captured.update(kwargs["form"])
        return response()

    with patch.object(GmailClient, "_request", side_effect=exchange):
        result = authorize_gmail(data, credentials, False, callback_port=0, timeout=4, on_url=on_url)
    join(threads)
    assert [item[0] for item in replies] == [404, 403, 404, 200]
    assert all(item[2]["Cache-Control"] == "no-store" for item in replies)
    assert result["authorized"] and result["person"] == "chris"
    assert query["scope"] == [GMAIL_SCOPE]
    assert query["include_granted_scopes"] == ["false"]
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    expected = base64.urlsafe_b64encode(hashlib.sha256(captured["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected]
    assert captured["code_verifier"] not in json.dumps(query)
    assert captured["code"] == "synthetic-one-use-code"
    assert GmailClient(account).connected()
    assert set(json.loads((account / "gmail-token.json").read_text())["scopes"]) == {GMAIL_SCOPE}
    assert "untrusted.example.invalid" not in (account / "gmail-client.json").read_text()
    assert "synthetic-one-use-code" not in (account / "gmail-token.json").read_text()
    assert not (data / "accounts" / "lisa").exists()
    for path, content in old.items():
        if not path.name.startswith("gmail-"):
            assert path.read_bytes() == content
    assert capsys.readouterr() == ("", "")
    if os.name == "posix":
        assert stat.S_IMODE(account.stat().st_mode) == 0o700
        for path in (account / "gmail-client.json", account / "gmail-token.json"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("changes", [
    {"scope": GMAIL_SCOPE + " https://www.googleapis.com/auth/calendar.events.readonly"},
    {"scope": ""}, {"access_token": ""}, {"refresh_token": ""}, {"expires_in": True},
    {"expires_in": 0}, {"expires_in": 86401}, {"token_type": "Basic"}, {"refresh_token": "bad\nsecret"},
])
def test_invalid_or_expanded_authorization_preserves_all_previous_files(setup, changes):
    data, _account, credentials, old = setup
    on_url, _query, _replies, threads = driver()
    with patch.object(GmailClient, "_request", return_value=response(**changes)):
        with pytest.raises(MessageError, match="read-only"):
            authorize_gmail(data, credentials, False, callback_port=0, timeout=3, on_url=on_url)
    join(threads)
    assert all(path.read_bytes() == content for path, content in old.items())


def test_declining_consent_never_exchanges_or_changes_existing_files(setup):
    data, _account, credentials, old = setup
    on_url, _query, replies, threads = driver([{"error": "access_denied"}])
    with patch.object(GmailClient, "_request") as exchange:
        with pytest.raises(MessageError, match="declined"):
            authorize_gmail(data, credentials, False, callback_port=0, timeout=3, on_url=on_url)
    join(threads)
    assert [item[0] for item in replies] == [400]
    exchange.assert_not_called()
    assert all(path.read_bytes() == content for path, content in old.items())


def test_timeout_and_web_credentials_leave_connections_unchanged(setup):
    data, _account, credentials, old = setup
    with pytest.raises(MessageError, match="timed out"):
        authorize_gmail(data, credentials, False, callback_port=0, timeout=.01, on_url=lambda _url: None)
    credentials.write_text(json.dumps({"web": {"client_id": "wrong-client"}}))
    with pytest.raises(MessageError, match="Desktop"):
        authorize_gmail(data, credentials, False, on_url=lambda _url: None)
    assert all(path.read_bytes() == content for path, content in old.items())


def test_pair_write_failure_restores_exact_previous_bytes(setup):
    data, _account, credentials, old = setup
    on_url, _query, _replies, threads = driver()
    replace = os.replace

    def fail_second(source, target):
        if Path(target).name == "gmail-token.json" and Path(source).name == "gmail-token.json":
            raise OSError("synthetic failure")
        return replace(source, target)

    with patch.object(GmailClient, "_request", return_value=response()), patch("flightwall.gmail_oauth.os.replace", side_effect=fail_second):
        with pytest.raises(MessageError, match="could not be saved"):
            authorize_gmail(data, credentials, False, callback_port=0, timeout=3, on_url=on_url)
    join(threads)
    assert all(path.read_bytes() == content for path, content in old.items())


def test_lisa_desktop_consent_isolated_from_chris_calendar_and_private_features(setup):
    data, _account, credentials, old = setup
    lisa = data / "accounts/lisa"
    lisa.mkdir()
    calendar = {lisa / "google-client.json": b'{"calendar":"lisa-client"}',
                lisa / "google-token.json": b'{"calendar":"lisa-token"}'}
    for path, value in calendar.items():
        path.write_bytes(value)
    on_url, query, _replies, threads = driver()
    with patch.object(GmailClient, "_request", return_value=response()):
        result = authorize_gmail(data, credentials, False, person="lisa", callback_port=0, timeout=3, on_url=on_url)
    join(threads)
    assert result["person"] == "lisa" and result["authorized"]
    assert query["scope"] == [GMAIL_SCOPE]
    assert GmailClient(lisa).connected()
    assert all(path.read_bytes() == value for path, value in (old | calendar).items())
    assert set(path.name for path in lisa.iterdir()) == {
        "google-client.json", "google-token.json", "gmail-client.json", "gmail-token.json", "gmail-auth.lock"}


@pytest.mark.parametrize("person", ["other", "../chris", "lisa/../../chris", None, []])
def test_invalid_gmail_profile_is_rejected_before_reading_client_or_opening_callback(tmp_path, person):
    with patch("flightwall.gmail_oauth._desktop_client") as desktop, patch("flightwall.gmail_oauth.HTTPServer") as server:
        with pytest.raises(MessageError, match="Chris or Lisa"):
            authorize_gmail(tmp_path / "not-created", tmp_path / "credentials.json", person=person)
    desktop.assert_not_called()
    server.assert_not_called()
    assert not (tmp_path / "not-created").exists()


@pytest.mark.parametrize("actual,matched", [("lisa@example.test", True), ("LISA@EXAMPLE.TEST", True), ("chris@example.test", False)])
def test_gmail_profile_identity_check_returns_only_match_and_uses_fixed_read_only_endpoint(tmp_path, actual, matched, capsys):
    with patch("flightwall.gmail_oauth.GmailClient") as client:
        client.return_value._token.return_value = {"scopes": [GMAIL_SCOPE]}
        client.return_value._access_token.return_value = "private-access"
        client.return_value._request.return_value = {"emailAddress": actual}
        result = check_gmail_profile(tmp_path, "lisa@example.test", person="lisa")
    client.assert_called_once_with(tmp_path / "accounts/lisa")
    client.return_value._request.assert_called_once_with(
        "GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile?fields=emailAddress", token="private-access")
    assert result == {"person": "lisa", "matches": matched, "read_only": True}
    assert "@" not in json.dumps(result) and "private" not in json.dumps(result)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("value", [{}, {"emailAddress": []}, {"emailAddress": ""}, {"emailAddress": "é@example.test"}])
def test_invalid_gmail_identity_response_fails_closed_without_address_output(tmp_path, value):
    with patch("flightwall.gmail_oauth.GmailClient") as client:
        client.return_value._token.return_value = {"scopes": [GMAIL_SCOPE]}
        client.return_value._request.return_value = value
        with pytest.raises(MessageError, match="could not be verified") as error:
            check_gmail_profile(tmp_path, "lisa@example.test", person="lisa")
    assert "@" not in str(error.value)


def test_gmail_profile_error_and_invalid_expectation_never_leak_private_values(tmp_path):
    with patch("flightwall.gmail_oauth.GmailClient") as client:
        for invalid in ("missing-domain", "lisa@example.test\n", None):
            with pytest.raises(MessageError):
                check_gmail_profile(tmp_path, invalid, person="lisa")
        client.assert_not_called()
        client.return_value._token.return_value = {"scopes": [GMAIL_SCOPE]}
        client.return_value._request.side_effect = RuntimeError("private-access lisa@example.test")
        with pytest.raises(MessageError) as error:
            check_gmail_profile(tmp_path, "lisa@example.test", person="lisa")
    assert "private-access" not in str(error.value) and "@" not in str(error.value)


@pytest.mark.parametrize("person", ["other", "../chris", None, []])
def test_gmail_identity_check_rejects_invalid_profile_before_access(tmp_path, person):
    with patch("flightwall.gmail_oauth.GmailClient") as client:
        with pytest.raises(MessageError, match="Chris or Lisa"):
            check_gmail_profile(tmp_path, "lisa@example.test", person=person)
    client.assert_not_called()


@pytest.mark.parametrize("scopes", [[GMAIL_SCOPE, "https://www.googleapis.com/auth/gmail.modify"],
                                     [GMAIL_SCOPE, "https://www.googleapis.com/auth/calendar.readonly"],
                                     [], None, [GMAIL_SCOPE, {}]])
def test_gmail_identity_check_rejects_broader_or_invalid_permissions_before_profile_request(tmp_path, scopes, capsys):
    with patch("flightwall.gmail_oauth.GmailClient") as client:
        client.return_value._token.return_value = {"scopes": scopes}
        with pytest.raises(MessageError, match="could not be verified") as error:
            check_gmail_profile(tmp_path, "lisa@example.test", person="lisa")
    client.return_value._request.assert_not_called()
    client.return_value._access_token.assert_not_called()
    assert "@" not in str(error.value) and "https:" not in str(error.value)
    assert capsys.readouterr() == ("", "")
