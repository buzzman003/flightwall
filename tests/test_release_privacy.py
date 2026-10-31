"""Release inventory rejects misplaced private runtime files; synthetic data only."""
from pathlib import Path
import tempfile
import unittest

from flightwall.updater import UpdateError
from scripts.make_release import _source_files


class ReleasePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "frozen"
        self.source.mkdir()
        self.key = self.root / "not-created-signing.pem"

    def test_runtime_private_directories_rejected_even_nested_or_empty(self):
        for name in ("accounts", "artwork", "presence", "shopping", "phone-invites", "updates", "logs",
                     ".local-install", ".aws", ".ssh", ".codex", ".agents", "userdata", "secrets"):
            with self.subTest(name=name):
                folder = self.source / "_internal" / name
                folder.mkdir(parents=True)
                with self.assertRaises(UpdateError):
                    _source_files(self.source, self.key)
                folder.rmdir()

    def test_detached_runtime_files_cannot_be_signed_as_application_files(self):
        names = ("config.json", "state.json", "google-client.json", "google-token.json", "gmail-token.json",
                 "calendar-cache.json", "household-cache.json", "household-calendars.json", "messages-cache.json",
                 "research-cache.json", "inspiration-cache.json", "personal.json", "content.json", "layovers.json",
                 "timers.json", "flight-watches.json", "remote-oauth.json", "weather.json", "setup-consents.json", "setup-complete.json",
                 "openai-usage.json", "flightaware-usage.json", "sms.json", "flightwall-list.json", "list.json", "lisa.json",
                 "control-token.txt", "alexa-token.txt", "notification-token.txt", "customtoken.txt", "lisa.token",
                 "openai.key", "flightaware.key", "personal.lock", "timers.lock", "flight-watches.lock",
                 "household-calendars.lock", "openai-usage.lock", "flightaware-usage.lock")
        for name in names:
            with self.subTest(name=name):
                path = self.source / name
                path.write_text("synthetic private fixture", encoding="utf-8")
                with self.assertRaises(UpdateError):
                    _source_files(self.source, self.key)
                path.unlink()

    def test_case_variants_are_rejected_without_echoing_contents(self):
        path = self.source / "SMS.JSON"
        path.write_text("synthetic-secret-must-not-be-in-error", encoding="utf-8")
        with self.assertRaises(UpdateError) as caught:
            _source_files(self.source, self.key)
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_legitimate_code_public_catalogue_and_mobile_assets_remain_allowed(self):
        names = ("FlightWall", "VERSION", "_internal/flightwall/shopping.py", "_internal/flightwall/phone_invites.py",
                 "_internal/flightwall/flight_watches.py", "assets/stores/emerald-coast-research.json",
                 "assets/onboarding/lisa.html", "assets/onboarding/lisa.js", "assets/onboarding/lisa.css",
                 "_internal/library.so")
        for name in names:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public synthetic fixture", encoding="utf-8")
        files = _source_files(self.source, self.key)
        self.assertEqual({relative.as_posix() for _, relative in files}, set(names))


if __name__ == "__main__":
    unittest.main()
