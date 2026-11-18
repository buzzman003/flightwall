"""Separate Delta import consent must not enable unrelated private features."""
import json

import pytest

from flightwall.config import ConfigStore, validate
from flightwall.onboarding import OnboardingRouter, SetupError


def test_delta_owner_names_are_private_local_settings(tmp_path):
    store = ConfigStore(tmp_path)
    assert store.get()["delta_travel_enabled"] is False
    assert json.loads(validate("delta_travel_owner_names", '["Pat Example", "PAT EXAMPLE"]')) == ["PAT EXAMPLE"]
    for key, value in (("delta_travel_enabled", True), ("delta_travel_owner_names", '["Pat Example"]'),
                       ("delta_travel_lisa_names", '["Lee Example"]')):
        assert key not in store.public()
        with pytest.raises(ValueError, match="local CLI"):
            store.update({key: value}, remote=True)
    with pytest.raises(ValueError, match="passenger-name"):
        store.update({"delta_travel_enabled": True})
    for value in ('["Pat"]', '["Pat <Example>"]', '[1]', '{"name":"Pat Example"}'):
        with pytest.raises(ValueError):
            validate("delta_travel_owner_names", value)
    with pytest.raises(ValueError, match="distinct"):
        store.update({"delta_travel_owner_names": '["Pat Example"]', "delta_travel_lisa_names": '["Pat Example"]'})


def test_delta_setup_requires_chris_and_gmail_and_leaves_previews_off(tmp_path):
    store = ConfigStore(tmp_path)
    router = OnboardingRouter(tmp_path, store.get, {"setup_save": store.update})
    settings = {"delta_travel_enabled": True, "delta_travel_owner_names": '["Pat Example"]'}
    with pytest.raises(SetupError):
        router._save_settings({"settings": settings})
    with pytest.raises(SetupError):
        router._save_settings({"settings": settings, "consenting_person": "lisa", "owner_consent": True})
    with pytest.raises(SetupError, match="read-only Gmail"):
        router._save_settings({"settings": settings, "consenting_person": "chris", "owner_consent": True})
    router._saved_authorization = lambda person, service: person == "chris" and service == "gmail"
    assert router._save_settings({"settings": settings, "consenting_person": "chris", "owner_consent": True}) == {"saved": True}
    actual = store.get()
    assert actual["delta_travel_enabled"] is True
    for key in ("messages_enabled", "chris_gmail_enabled", "lisa_gmail_enabled", "lisa_private_enabled"):
        assert actual[key] is False
    router._save_settings({"settings": {"delta_travel_enabled": False}})
    assert store.get()["delta_travel_enabled"] is False


def test_delta_worker_is_independent_and_does_not_stop_other_mail(tmp_path):
    from flightwall.backend import Runtime
    from unittest.mock import Mock
    runtime = Runtime(tmp_path, tmp_path / "install")
    runtime.config.update({"delta_travel_enabled": True, "delta_travel_owner_names": '["Pat Example"]'})
    runtime.delta_travel.sync = Mock()
    runtime.messages.sync = Mock()
    runtime._sync_messages()
    runtime.delta_travel.sync.assert_called_once_with()
    runtime.messages.sync.assert_not_called()
    runtime.config.update({"messages_enabled": True, "chris_gmail_enabled": True})
    runtime.delta_travel.sync.side_effect = RuntimeError("private input must not escape")
    runtime._sync_messages()
    runtime.messages.sync.assert_called_once_with("chris")


def test_owner_leave_plan_is_opt_in_validated_and_not_a_remote_write(tmp_path):
    store = ConfigStore(tmp_path)
    assert store.get()["vps_leave_plan_enabled"] is False
    with pytest.raises(ValueError, match="local CLI"):
        store.update({"vps_leave_plan_enabled": True}, remote=True)
    with pytest.raises(ValueError, match="early"):
        store.update({"vps_leave_target_early_minutes": 40, "vps_leave_target_late_minutes": 50})
    with pytest.raises(ValueError):
        store.update({"vps_leave_drive_minutes": -1})
    store.update({"vps_leave_plan_enabled": True})
    assert store.get()["vps_leave_plan_enabled"] is True


def test_gmail_web_flow_reuses_only_the_same_profiles_staged_client(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    from flightwall.config import private_json
    store = ConfigStore(tmp_path)
    store.update({"public_url": "https://board.example.test"})
    client = {"client_id": "synthetic-client.apps.googleusercontent.com", "client_secret": "synthetic-secret"}
    source = tmp_path / "accounts/chris/google-web-client.json"
    private_json(source, client)
    before = source.read_bytes()
    router = OnboardingRouter(tmp_path, store.get)
    body = {"person": "chris", "service": "gmail", "consenting_person": "chris", "owner_consent": True}
    result = router._oauth_start(body, "synthetic-setup-session")
    query = parse_qs(urlsplit(result["authorization_url"]).query)
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert query["client_id"] == [client["client_id"]]
    assert source.read_bytes() == before
    assert not (tmp_path / "accounts/chris/gmail-token.json").exists()
    with pytest.raises(SetupError):
        router._oauth_start(body | {"person": "lisa", "consenting_person": "lisa"}, "synthetic-setup-session")


def test_lisa_mail_opt_in_requires_her_separate_consent_and_gmail_without_previews(tmp_path):
    store = ConfigStore(tmp_path)
    router = OnboardingRouter(tmp_path, store.get, {"setup_save": store.update})
    settings = {"delta_travel_lisa_mail_enabled": True, "delta_travel_lisa_names": '["Casey Taylor"]'}
    assert store.get()["delta_travel_lisa_mail_enabled"] is False
    assert "delta_travel_lisa_mail_enabled" not in store.public()
    with pytest.raises(ValueError, match="local CLI"):
        store.update({"delta_travel_lisa_mail_enabled": True}, remote=True)
    with pytest.raises(ValueError, match="passenger-name"):
        store.update({"delta_travel_lisa_mail_enabled": True})
    for consent in ({}, {"consenting_person": "chris", "owner_consent": True}):
        with pytest.raises(SetupError):
            router._save_settings({"settings": settings} | consent)
    lisa_consent = {"consenting_person": "lisa", "owner_consent": True}
    with pytest.raises(SetupError, match="Lisa's read-only Gmail"):
        router._save_settings({"settings": settings} | lisa_consent)
    router._saved_authorization = lambda person, service: person == "lisa" and service == "gmail"
    assert router._save_settings({"settings": settings} | lisa_consent) == {"saved": True}
    current = store.get()
    assert current["delta_travel_lisa_mail_enabled"] is True
    for field in ("delta_travel_enabled", "messages_enabled", "lisa_gmail_enabled", "lisa_private_enabled",
                  "presence_enabled", "lisa_flight_tracking_enabled"):
        assert current[field] is False
    with pytest.raises(SetupError):
        router._save_settings({"settings": {"delta_travel_lisa_names": '["Another Person"]'},
                               "consenting_person": "chris", "owner_consent": True})
    router._save_settings({"settings": {"delta_travel_lisa_mail_enabled": False}})
    assert store.get()["delta_travel_lisa_mail_enabled"] is False


def test_lisa_tracking_opt_in_is_separate_setup_only_and_needs_lisa_consent(tmp_path):
    store = ConfigStore(tmp_path)
    router = OnboardingRouter(tmp_path, store.get, {"setup_save": store.update})
    assert store.get()["lisa_flight_tracking_enabled"] is False
    assert store.public()["lisa_flight_tracking_enabled"] is False
    with pytest.raises(ValueError, match="local CLI"):
        store.update({"lisa_flight_tracking_enabled": True}, remote=True)
    settings = {"settings": {"lisa_flight_tracking_enabled": True}}
    with pytest.raises(SetupError):
        router._save_settings(settings | {"consenting_person": "chris", "owner_consent": True})
    router._save_settings(settings | {"consenting_person": "lisa", "owner_consent": True})
    assert store.get()["lisa_flight_tracking_enabled"] is True
    assert store.public()["lisa_flight_tracking_enabled"] is True
    assert store.get()["flightaware_monthly_limit_usd"] == 5
    assert store.get()["delta_travel_lisa_mail_enabled"] is False


def test_lisa_mail_worker_is_independent_of_chris_errors_and_message_previews(tmp_path):
    from flightwall.backend import Runtime
    from unittest.mock import Mock
    runtime = Runtime(tmp_path, tmp_path / "install")
    runtime.config.update({"delta_travel_enabled": True, "delta_travel_owner_names": '["Pat Example"]',
                           "delta_travel_lisa_mail_enabled": True, "delta_travel_lisa_names": '["Casey Taylor"]'})
    runtime.delta_travel.sync = Mock(side_effect=RuntimeError("private Chris error"))
    runtime.delta_travel_lisa.sync = Mock()
    runtime.messages.sync = Mock()
    runtime._sync_messages()
    runtime.delta_travel.sync.assert_called_once_with()
    runtime.delta_travel_lisa.sync.assert_called_once_with()
    runtime.messages.sync.assert_not_called()
    runtime.delta_travel_lisa.status = Mock(return_value={"enabled": True, "status": "ready", "imported": 2})
    assert runtime.setup_get()["delta_travel_lisa"] == {"enabled": True, "status": "ready", "imported": 2}
