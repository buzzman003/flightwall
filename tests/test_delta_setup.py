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
