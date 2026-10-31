"""Compatibility and isolation for local setup commands; no external accounts."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flightwall.config import ConfigStore, private_json
from flightwall.main import main, parser
from flightwall.presence import PresenceStore


CALENDARS = [{"id": "fc-view-id", "name": "FC View", "timezone": "America/New_York"},
             {"id": "buzz-id", "name": "Buzz Flight Brief", "timezone": "UTC"},
             {"id": "family-id", "name": "Family", "timezone": "America/Chicago"}]
SNAPSHOT = {"flights": [], "last_sync": None, "source": "FC View", "error": None}
HOUSEHOLD = {"flight_snapshot": SNAPSHOT, "kids": {"state": "unknown", "error": None}}


class SetupCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.store = ConfigStore(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = main(["--data-dir", str(self.data), *args])
        return result, out.getvalue(), err.getvalue()

    def test_legacy_calendar_listing_still_uses_legacy_account(self):
        with patch("flightwall.calendar_client.CalendarClient") as legacy, \
                patch("flightwall.household_calendar.HouseholdCalendars") as household:
            legacy.return_value.list_calendars.return_value = CALENDARS
            code, out, _ = self.run_cli("calendars")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), CALENDARS)
        self.assertEqual(legacy.call_args.args[0], str(self.data))
        household.assert_not_called()

    def test_scoped_calendar_listing_selects_only_requested_account(self):
        with patch("flightwall.household_calendar.HouseholdCalendars") as household:
            household.return_value.list_calendars.return_value = [CALENDARS[2]]
            code, out, _ = self.run_cli("calendars", "--person", "lisa")
        self.assertEqual(code, 0)
        household.return_value.list_calendars.assert_called_once_with("lisa")
        self.assertEqual(json.loads(out), [CALENDARS[2]])

    def test_legacy_reauthorization_updates_scoped_chris_credentials_privately(self):
        account = self.data / "accounts" / "chris"
        private_json(account / "google-client.json", {"client_id": "old-client"})
        private_json(account / "google-token.json", {"access_token": "old-access"})
        fresh_client = {"client_id": "new-client", "client_secret": "private-client-value"}
        fresh_token = {"access_token": "new-private-access", "refresh_token": "new-private-refresh"}
        def authorize(*_args, **_kwargs):
            private_json(self.data / "google-client.json", fresh_client)
            private_json(self.data / "google-token.json", fresh_token)
            return {"authorized": True}
        with patch("flightwall.calendar_client.CalendarClient") as legacy:
            legacy.return_value.authorize.side_effect = authorize
            legacy.return_value.list_calendars.return_value = CALENDARS
            legacy.return_value.sync.return_value = SNAPSHOT
            code, out, err = self.run_cli("authorize", "--credentials", "client.json", "--no-browser")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads((account / "google-client.json").read_text()), fresh_client)
        self.assertEqual(json.loads((account / "google-token.json").read_text()), fresh_token)
        if os.name == "posix":
            self.assertEqual(os.stat(account / "google-token.json").st_mode & 0o777, 0o600)
        self.assertEqual(self.store.get()["calendar_id"], "fc-view-id")
        self.assertFalse(self.store.get()["demo"])
        self.assertNotIn("new-private", out + err)

    def test_person_authorization_does_not_implicitly_select_a_role(self):
        with patch("flightwall.household_calendar.HouseholdCalendars") as household:
            household.return_value.authorize.return_value = {"authorized": True, "verified": True, "profile": "lisa", "calendars": [CALENDARS[2]]}
            code, out, _ = self.run_cli("authorize", "--person", "lisa", "--credentials", "lisa-client.json", "--no-browser")
        self.assertEqual(code, 0)
        household.return_value.authorize.assert_called_once_with("lisa", Path("lisa-client.json"), open_browser=False, callback_port=8766)
        self.assertEqual(json.loads(out)["profile"], "lisa")
        self.assertTrue(self.store.get()["demo"])
        self.assertEqual(self.store.get()["family_calendar_id"], "")

    def test_lisa_family_selection_keeps_flight_and_display_timezone(self):
        self.store.update({"timezone": "America/New_York", "calendar_id": "existing-flight"})
        with patch("flightwall.household_calendar.HouseholdCalendars") as household:
            household.return_value.list_calendars.return_value = CALENDARS
            household.return_value.sync.return_value = HOUSEHOLD
            code, out, _ = self.run_cli("select-calendar", "--person", "lisa", "--role", "family", "--id", "family-id")
        self.assertEqual(code, 0)
        household.return_value.list_calendars.assert_called_once_with("lisa")
        observed = self.store.get()
        self.assertEqual(observed["family_calendar_profile"], "lisa")
        self.assertEqual(observed["family_calendar_id"], "family-id")
        self.assertTrue(observed["kids_filter_enabled"])
        self.assertEqual(observed["calendar_id"], "existing-flight")
        self.assertEqual(observed["timezone"], "America/New_York")
        self.assertTrue(observed["demo"])
        self.assertEqual(json.loads(out)["role"], "family")

    def test_secondary_flight_selection_uses_chris_without_changing_timezone(self):
        self.store.update({"timezone": "America/New_York"})
        with patch("flightwall.household_calendar.HouseholdCalendars") as household:
            household.return_value.list_calendars.return_value = CALENDARS
            household.return_value.sync.return_value = HOUSEHOLD
            code, _, _ = self.run_cli("select-calendar", "--role", "secondary")
        self.assertEqual(code, 0)
        household.return_value.list_calendars.assert_called_once_with("chris")
        self.assertEqual(self.store.get()["secondary_calendar_id"], "buzz-id")
        self.assertEqual(self.store.get()["timezone"], "America/New_York")
        self.assertFalse(self.store.get()["demo"])

    def test_lisa_cannot_be_mistakenly_bound_to_chris_flight_role(self):
        with patch("flightwall.household_calendar.HouseholdCalendars") as household:
            code, _, err = self.run_cli("select-calendar", "--person", "lisa", "--role", "flight", "--id", "family-id")
        self.assertEqual(code, 1)
        self.assertIn("--role family", err)
        household.assert_not_called()
        self.assertEqual(self.store.get()["calendar_id"], "")

    def test_sync_keeps_legacy_default_until_scoped_accounts_or_roles_exist(self):
        with patch("flightwall.calendar_client.CalendarClient") as legacy, \
                patch("flightwall.household_calendar.HouseholdCalendars") as household:
            legacy.return_value.sync.return_value = SNAPSHOT
            code, _, _ = self.run_cli("sync")
            self.assertEqual(code, 0)
            legacy.return_value.sync.assert_called_once()
            household.assert_not_called()
        self.store.update({"family_calendar_profile": "lisa", "family_calendar_id": "family-id"})
        with patch("flightwall.calendar_client.CalendarClient") as legacy, \
                patch("flightwall.household_calendar.HouseholdCalendars") as household:
            household.return_value.sync.return_value = HOUSEHOLD
            code, out, _ = self.run_cli("sync")
            self.assertEqual(code, 0)
            household.return_value.sync.assert_called_once()
            legacy.assert_not_called()
            self.assertEqual(json.loads(out)["flights"], 0)

    def test_setup_info_does_not_provision_a_secret_without_show_option(self):
        code, out, _ = self.run_cli("setup-info")
        self.assertEqual(code, 0)
        info = json.loads(out)
        self.assertEqual(info["setup_url"], "http://127.0.0.1:8765/setup")
        self.assertFalse(info["token_present"])
        self.assertNotIn("control_token", info)
        path = self.data / "control-token.txt"
        self.assertFalse(path.exists())
        code, out, _ = self.run_cli("setup-info", "--show-token")
        self.assertEqual(code, 0)
        token = path.read_text().strip()
        self.assertEqual(json.loads(out)["control_token"], token)
        self.assertGreaterEqual(len(token), 32)
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertFalse((self.data / "alexa-token.txt").exists())
        _, out, _ = self.run_cli("setup-info")
        self.assertNotIn(token, out)
        self.assertEqual(path.read_text().strip(), token)

    def test_presence_pair_rotates_only_selected_person_and_hides_secret_by_default(self):
        code, out, _ = self.run_cli("presence-pair", "chris")
        self.assertEqual(code, 0)
        presence = PresenceStore(self.data, self.store.get)
        old = presence.token_path("chris").read_text().strip()
        self.assertNotIn(old, out)
        self.assertNotIn("presence_token", json.loads(out))
        self.assertFalse((self.data / "control-token.txt").exists())
        self.assertFalse(presence.token_path("lisa").exists())
        code, out, _ = self.run_cli("presence-pair", "chris", "--show-token")
        self.assertEqual(code, 0)
        fresh = json.loads(out)["presence_token"]
        self.assertNotEqual(old, fresh)
        self.assertTrue(presence.authenticate("chris", fresh))
        self.assertFalse(presence.authenticate("chris", old))

    def test_api_secrets_are_private_atomic_and_never_echoed(self):
        source = Path(self.temp.name) / "api-key.txt"
        for kind, value in (("flightaware", "my-private-flightaware-key"), ("openai", "sk-" + "a" * 40)):
            source.write_text(value)
            destination = self.data / (kind + ".key")
            destination.write_text("old-value")
            destination.chmod(0o644)
            code, out, err = self.run_cli("store-secret", kind, "--file", str(source))
            self.assertEqual(code, 0, err)
            self.assertEqual(destination.read_text().strip(), value)
            if os.name == "posix":
                self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
            self.assertNotIn(value, out + err)
        source.write_text("invalid-private-key")
        code, out, err = self.run_cli("store-secret", "openai", "--file", str(source))
        self.assertEqual(code, 1)
        self.assertNotIn("invalid-private-key", out + err)

    def test_cli_help_explains_scoped_roles_and_explicit_secret_options(self):
        choices = parser().parse_args(["select-calendar", "--person", "lisa", "--role", "family", "--id", "family-id"])
        self.assertEqual((choices.person, choices.role), ("lisa", "family"))
        self.assertFalse(parser().parse_args(["setup-info"]).show_token)
        self.assertFalse(parser().parse_args(["presence-pair", "chris"]).show_token)

    def test_diagnose_recognizes_scoped_credentials_without_disclosing_private_ids(self):
        private_json(self.data / "accounts" / "lisa" / "google-token.json", {"access_token": "private-lisa-access"})
        with patch("flightwall.household_calendar.HouseholdCalendars") as household:
            household.return_value.status.return_value = {"connected_profiles": {
                "lisa": {"credentials_present": True, "verified": False, "connected": False,
                         "email": "private-account@example.com", "calendar_id": "private-calendar-id"}}}
            code, out, err = self.run_cli("diagnose")
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertIs(info["google_authorized"], True)
        self.assertEqual(info["google_profiles"]["lisa"], {"credentials_present": True, "verified": False, "connected": False})
        self.assertNotIn("private-", out + err)
        self.assertNotIn("private-account@example.com", out + err)
        self.assertFalse((self.data / "control-token.txt").exists())


if __name__ == "__main__":
    unittest.main()
