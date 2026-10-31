"""Selected Gmail metadata and scoped notifications must remain private and bounded."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from flightwall.config import private_json
from flightwall.messages import GMAIL_SCOPE, GmailClient, MessageError, MessageFeeds, _Unauthorized


UTC = timezone.utc
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)


def email(identifier="email1", thread="thread1", sender="Airbnb <notice@airbnb.com>", minutes=10, **changes):
    return {"id": identifier, "threadId": thread, "snippet": "Please confirm the reservation.",
            "internalDate": str(int((NOW - timedelta(minutes=minutes)).timestamp() * 1000)),
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": "New booking"}],
                        "body": {"data": "PRIVATE_FULL_BODY_MUST_NOT_CACHE"}, "parts": [{"filename": "private-attachment.pdf"}]},
            **changes}


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = {"messages_enabled": True, "chris_gmail_enabled": True, "lisa_gmail_enabled": False,
                       "chris_lawyer_senders": "[]", "chris_lawyer_domains": "[]",
                       "lisa_mail_senders": "[]", "lisa_mail_domains": "[]", "message_preview_chars": 180,
                       "chris_notifications_enabled": False, "lisa_notifications_enabled": False,
                       "chris_notification_apps": "[]", "lisa_notification_apps": "[]"}
        self.feeds = MessageFeeds(self.root, lambda: deepcopy(self.config))
        self.clock = patch("flightwall.messages._now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.account("chris")

    def account(self, profile):
        folder = self.feeds.account_dir(profile)
        private_json(folder / "gmail-client.json", {"client_id": "example-client", "client_secret": "private-client-secret"})
        private_json(folder / "gmail-token.json", {"client_id": "example-client", "access_token": "private-access-token",
                     "refresh_token": "private-refresh-token", "expires_at": time.time() + 3600, "scopes": [GMAIL_SCOPE]})
        return folder

    def sync(self, messages=None, sent_minutes=None):
        messages = messages or [email()]
        self.calls = []
        def api(path, parameters):
            self.calls.append((path, deepcopy(parameters)))
            if path == "/messages":
                return {"messages": [{"id": value["id"]} for value in messages]}
            if path.startswith("/messages/"):
                return next(value for value in messages if value["id"] == path.split("/")[-1])
            if path.startswith("/threads/"):
                values = [{"id": "inbound", "internalDate": messages[0]["internalDate"], "labelIds": ["INBOX", "UNREAD"]}]
                if sent_minutes is not None:
                    values.append({"id": "sent", "internalDate": str(int((NOW - timedelta(minutes=sent_minutes)).timestamp() * 1000)), "labelIds": ["SENT"]})
                return {"messages": values}
            raise AssertionError("Unexpected request")
        with patch.object(GmailClient, "api", side_effect=api):
            return self.feeds.sync("chris")

    def enable_notifications(self, profile="chris"):
        self.config[profile + "_notifications_enabled"] = True
        self.config[profile + "_notification_apps"] = '["Airbnb", "VRBO"]' if profile == "chris" else '["Messages", "WhatsApp"]'
        return self.feeds.rotate_notification_token(profile)

    @staticmethod
    def notification(**changes):
        return {"app": "Airbnb", "sender": "Alex", "preview": "See you at six.",
                "timestamp": NOW.isoformat(), "request_id": "opaque-phone-event-1", **changes}

    def test_mail_filters_are_in_query_and_rechecked_against_real_sender(self):
        result = self.sync([email(), email("evil1", "t2", "Airbnb <notice@airbnb.com.evil.test>"),
                            email("evil2", "t3", "airbnb.com <outsider@evil.test>"),
                            email("evil3", "t4", "a@airbnb.com, outsider@evil.test")])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["source"], "Gmail / Airbnb")
        self.assertEqual(result["items"][0]["category"], "rental")
        self.assertTrue(result["items"][0]["filtered"])
        query = self.calls[0][1]["q"]
        self.assertIn("in:inbox is:unread newer_than:1d", query)
        self.assertIn("from:(airbnb.com)", query)
        self.assertIn("from:(vrbo.com)", query)
        self.assertNotIn("legal", query)

    def test_only_explicit_lawyer_addresses_or_domains_are_selected(self):
        self.config["chris_lawyer_senders"] = '["counsel@firm.example"]'
        self.config["chris_lawyer_domains"] = '["other-firm.example"]'
        result = self.sync([email("a", "a", "counsel@firm.example"), email("b", "b", "associate@firm.example"),
                            email("c", "c", "lawyer@cases.other-firm.example"), email("d", "d", "lawyer@unknown.example")])
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(item["source"] == "Gmail / Lawyer" for item in result["items"]))
        self.assertTrue(all(item["category"] == "lawyer" for item in result["items"]))

    def test_public_mail_domain_cannot_expand_chris_lawyer_filter_to_unrelated_mail(self):
        self.config["chris_lawyer_domains"] = '["gmail.com"]'
        with patch.object(GmailClient, "api") as api:
            with self.assertRaisesRegex(MessageError, "exact lawyer email address"):
                self.feeds.sync("chris")
            api.assert_not_called()
        self.config["chris_lawyer_domains"] = "[]"
        self.config["chris_lawyer_senders"] = '["specific-lawyer@gmail.com"]'
        result = self.sync([email(sender="specific-lawyer@gmail.com"), email("other", "other", "friend@gmail.com")])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["category"], "lawyer")

    def test_vrbo_homeaway_and_airbnb_subdomains_are_allowed(self):
        result = self.sync([email("a", "a", "notice@mail.airbnb.com"), email("b", "b", "notice@vrbo.com"), email("c", "c", "notice@homeaway.com")])
        self.assertEqual(len(result["items"]), 3)

    def test_sent_or_read_or_non_inbox_messages_are_not_cached(self):
        result = self.sync([email("a", "a", labelIds=["INBOX"]), email("b", "b", labelIds=["UNREAD"]), email("c", "c", labelIds=["INBOX", "UNREAD", "SENT"])])
        self.assertEqual(result["items"], [])

    def test_previews_are_plain_short_and_body_attachments_never_requested_or_saved(self):
        result = self.sync([email(snippet='<b>Hello</b> &amp; welcome <script>hidden()</script> ' + "x" * 400)])
        item = result["items"][0]
        self.assertTrue(item["body"].startswith("Hello & welcome"))
        self.assertLessEqual(len(item["body"]), 180)
        self.assertNotIn("<", item["body"])
        self.assertNotIn("hidden", item["body"])
        raw = (self.feeds.account_dir("chris") / "messages-cache.json").read_text()
        self.assertNotIn("PRIVATE_FULL_BODY", raw)
        self.assertNotIn("private-attachment", raw)
        self.assertNotIn("private-access-token", raw)
        self.assertNotIn("notice@airbnb.com", raw)
        for path, params in self.calls[1:]:
            self.assertIn(params["format"], {"metadata", "full"})
            self.assertNotIn("body", params["fields"])
            if params["format"] == "full":
                self.assertEqual(params["fields"], "snippet")

    def test_reply_is_labeled_as_uncertain_heuristic(self):
        item = self.sync()["items"][0]
        self.assertTrue(item["needs_reply"])
        self.assertEqual(item["reply_label"], "May need reply")
        item = self.sync(sent_minutes=2)["items"][0]
        self.assertFalse(item["needs_reply"])
        self.assertEqual(item["reply_label"], "Reply not inferred")

    def test_latest_allowed_message_per_thread_wins(self):
        result = self.sync([email("older", "same", minutes=60), email("newer", "same", minutes=5)])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["observed_at"], (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"))

    def test_email_cache_expires_and_failure_is_marked_stale_without_erasing_cache(self):
        original = self.sync()["items"]
        with patch.object(GmailClient, "api", side_effect=MessageError("secret-provider-error")):
            result = self.feeds.sync("chris")
        self.assertEqual(result["items"], original)
        self.assertTrue(result["stale"])
        self.assertNotIn("secret-provider-error", result["error"])
        with patch("flightwall.messages._now", return_value=NOW + timedelta(hours=25)):
            self.assertEqual(self.feeds.status("chris")["items"], [])
        self.assertEqual(json.loads((self.feeds.account_dir("chris") / "messages-cache.json").read_text())["records"], [])

    def test_removed_lawyer_filter_cannot_leave_cached_private_summaries_visible(self):
        self.config["chris_lawyer_senders"] = '["lawyer@firm.example"]'
        self.assertEqual(len(self.sync([email(sender="lawyer@firm.example")])["items"]), 1)
        self.config["chris_lawyer_senders"] = "[]"
        self.assertEqual(self.feeds.status("chris")["items"], [])

    def test_lisa_is_calendar_only_until_her_separate_explicit_consent_and_allowlist(self):
        with patch.object(GmailClient, "api") as api:
            self.assertEqual(self.feeds.sync("lisa")["items"], [])
            api.assert_not_called()
        self.account("lisa")
        self.config["lisa_gmail_enabled"] = True
        with patch.object(GmailClient, "api") as api:
            self.assertEqual(self.feeds.sync("lisa")["items"], [])
            api.assert_not_called()  # No sender filter, no broad-inbox fallback.
        self.assertNotIn("chris", json.dumps(self.feeds.status("lisa")))

    def test_global_or_per_profile_opt_out_hides_cache_and_stops_requests(self):
        self.sync()
        for key in ("messages_enabled", "chris_gmail_enabled"):
            saved = self.config[key]
            self.config[key] = False
            with patch.object(GmailClient, "api") as api:
                self.assertEqual(self.feeds.sync("chris")["items"], [])
                api.assert_not_called()
            self.config[key] = saved

    def test_calendar_authorization_is_never_treated_as_gmail_consent(self):
        (self.feeds.account_dir("chris") / "gmail-token.json").unlink()
        private_json(self.feeds.account_dir("chris") / "google-token.json", {"access_token": "calendar-only"})
        self.assertFalse(self.feeds.connected("chris"))
        self.assertFalse(self.feeds.connected("lisa"))

    def test_notification_token_is_explicit_private_scoped_and_rotatable(self):
        self.assertFalse(self.feeds.notification_token_path("chris").exists())
        token = self.enable_notifications()
        self.assertTrue(self.feeds.authenticate_notification("chris", token))
        self.assertFalse(self.feeds.authenticate_notification("lisa", token))
        self.assertFalse(self.feeds.authenticate_notification("chris", "wrong" * 20))
        if os.name == "posix":
            self.assertEqual(self.feeds.notification_token_path("chris").stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.feeds.account_dir("chris").stat().st_mode & 0o777, 0o700)
        replacement = self.feeds.rotate_notification_token("chris")
        self.assertNotEqual(replacement, token)
        self.assertFalse(self.feeds.authenticate_notification("chris", token))
        self.config["chris_notifications_enabled"] = False
        self.assertFalse(self.feeds.authenticate_notification("chris", replacement))

    def test_selected_notification_is_private_idempotent_and_short(self):
        self.enable_notifications()
        payload = self.notification(preview="<b>Hi</b> " + "x" * 200)
        first = self.feeds.receive_notification("chris", payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(self.feeds.receive_notification("chris", payload)["duplicate"])
        items = self.feeds.status("chris")["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["owner"], "chris")
        self.assertTrue(items[0]["body"].startswith("Hi "))
        self.assertEqual(len(items[0]["body"]), 180)
        self.assertFalse(items[0]["needs_reply"])
        self.assertEqual(self.feeds.status("lisa")["items"], [])
        raw = (self.feeds.account_dir("chris") / "messages-cache.json").read_text()
        self.assertNotIn("opaque-phone-event-1", raw)
        if os.name == "posix":
            self.assertEqual((self.feeds.account_dir("chris") / "messages-cache.json").stat().st_mode & 0o777, 0o600)

    def test_unselected_apps_old_timestamps_full_bodies_and_conflicting_retries_rejected(self):
        self.enable_notifications()
        self.feeds.receive_notification("chris", self.notification())
        invalid = [self.notification(app="Bank"), self.notification(body="unwanted full body"),
                   self.notification(timestamp=(NOW - timedelta(hours=25)).isoformat()),
                   self.notification(timestamp="2026-10-22T16:00:00"),
                   self.notification(preview="x" * 5000), self.notification(sender="Different person")]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:80]):
                with self.assertRaises(MessageError):
                    self.feeds.receive_notification("chris", payload)
        self.assertEqual(len(self.feeds.status("chris")["items"]), 1)

    def test_cache_is_capped_at_twenty_and_app_revocation_hides_cached_notifications(self):
        self.enable_notifications()
        for i in range(25):
            self.feeds.receive_notification("chris", self.notification(request_id="event-" + str(i), sender="Person " + str(i)))
        self.assertEqual(len(self.feeds.status("chris")["items"]), 20)
        self.config["chris_notification_apps"] = '["VRBO"]'
        self.assertEqual(self.feeds.status("chris")["items"], [])

    def test_chris_generic_phone_messages_are_rejected_but_lisa_can_opt_in_to_her_own(self):
        chris_token = self.enable_notifications("chris")
        self.config["chris_notification_apps"] = '["Messages"]'
        self.assertFalse(self.feeds.authenticate_notification("chris", chris_token))
        with self.assertRaisesRegex(MessageError, "only Airbnb, VRBO or HomeAway"):
            self.feeds.receive_notification("chris", self.notification(app="Messages"))
        lisa_token = self.enable_notifications("lisa")
        self.assertTrue(self.feeds.authenticate_notification("lisa", lisa_token))
        self.feeds.receive_notification("lisa", self.notification(app="Messages"))
        item = self.feeds.status("lisa")["items"][0]
        self.assertEqual(item["category"], "personal")
        self.assertEqual(item["owner"], "lisa")
        self.assertFalse(self.feeds.connected("lisa"))  # Her phone opt-in is not Gmail consent.

    def test_notification_ingestion_does_not_call_google_or_change_calendar_tokens(self):
        self.enable_notifications()
        private_json(self.root / "google-token.json", {"private": "calendar-unchanged"})
        before = (self.root / "google-token.json").read_bytes()
        with patch.object(GmailClient, "api") as api:
            self.feeds.receive_notification("chris", self.notification())
            api.assert_not_called()
        self.assertEqual((self.root / "google-token.json").read_bytes(), before)

    def test_broad_or_injected_sender_rules_fail_closed(self):
        for value in ('["*"]', '["com"]', '["firm.example) OR in:anywhere"]'):
            self.config["chris_lawyer_domains"] = value
            with patch.object(GmailClient, "api") as api:
                with self.assertRaises(MessageError):
                    self.feeds.sync("chris")
                api.assert_not_called()

    def test_gmail_refresh_preserves_offline_token_and_never_calls_send_api(self):
        folder = self.feeds.account_dir("chris")
        token = json.loads((folder / "gmail-token.json").read_text())
        token["expires_at"] = 0
        private_json(folder / "gmail-token.json", token)
        client = GmailClient(folder)
        with patch.object(client, "_request", return_value={"access_token": "new-access", "expires_in": 3600, "scope": GMAIL_SCOPE}) as request:
            self.assertEqual(client._access_token(), "new-access")
        self.assertEqual(request.call_args.args, ("POST", "https://oauth2.googleapis.com/token"))
        saved = json.loads((folder / "gmail-token.json").read_text())
        self.assertEqual(saved["refresh_token"], "private-refresh-token")
        self.assertNotIn("client_secret", saved)
        with patch.object(client, "_request") as request:
            with self.assertRaises(MessageError):
                client.api("/messages/send", {})
            request.assert_not_called()

    def test_401_forces_one_refresh_and_tokens_cannot_follow_redirects(self):
        client = GmailClient(self.feeds.account_dir("chris"))
        with patch.object(client, "_access_token", side_effect=["old", "new"]) as access, \
                patch.object(client, "_request", side_effect=[_Unauthorized(), {"messages": []}]) as request:
            self.assertEqual(client.api("/messages", {}), {"messages": []})
        self.assertEqual(access.call_args.kwargs, {"refresh": True})
        self.assertEqual(request.call_count, 2)
        with self.assertRaises(MessageError):
            client._request("GET", "https://attacker.example/private", token="secret")
        redirect = next(handler for handler in client.opener.handlers if hasattr(handler, "redirect_request"))
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://attacker.example"))


if __name__ == "__main__":
    unittest.main()
