"""Calendar integration tests use synthetic events and a local OAuth callback."""

import base64
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from flightwall.calendar_client import (
    AUTH_URL, CALENDAR_URL, SCOPES, TOKEN_URL,
    CalendarClient, CalendarError, parse_events, _HTTPFailure, _private_json,
)


def event(title, start="2026-11-06T18:19:00Z", end="2026-11-06T21:49:00Z", **extra):
    return {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


class ParserTests(unittest.TestCase):
    def test_fc_view_multi_leg_report_and_privacy(self):
        events = [
            event("Report: 1219L", "2026-11-06T17:19:00Z", "2026-11-07T05:49:00Z",
                  id="private-report-id", description="1946 ATL-SJU\n1946 SJU-ATL\nCrew Private Name\nHotel 555-1234"),
            event("1946(DH): ATL-SJU(1319-1749)", id="private-flight-id"),
            event("1946: SJU-ATL(1859-2151)", "2026-11-06T22:59:00Z", "2026-11-07T02:51:00Z"),
            event("Report: 0600L", "2026-11-08T11:00:00Z", "2026-11-08T23:00:00Z"),
            event("1946: ATL-MEM(0900-0930)", "2026-11-08T14:00:00Z", "2026-11-08T15:30:00Z"),
        ]
        result = parse_events(events)
        self.assertEqual(len(result), 3)
        self.assertTrue(result[0]["deadhead"])
        self.assertFalse(result[1]["deadhead"])
        # The title's airport-local departure 1319 must not become UTC1319.
        self.assertEqual(datetime.fromisoformat(result[0]["start"]).hour, 18)
        self.assertEqual(result[0]["report"], "2026-11-06T17:19:00+00:00")
        self.assertEqual(result[1]["report"], result[0]["report"])
        self.assertEqual(result[2]["report"], "2026-11-08T11:00:00+00:00")
        text = json.dumps(result)
        for private in ("Private Name", "Hotel", "private-flight-id", "private-report-id", "description"):
            self.assertNotIn(private, text)

    def test_operating_number_prefix_and_deadhead_variants(self):
        cases = [("DL 1234 ATL → MEM", "DL1234", False),
                 ("DH DL1234 ATL-MEM", "DL1234", True),
                 ("DL1234 ATL-MEM (DH)", "DL1234", True),
                 ("1946(DH): ATL-SJU(1319-1749)", "1946", True),
                 ("Flight B6 123: JFK–LAX", "B6123", False)]
        for title, number, deadhead in cases:
            with self.subTest(title=title):
                flight = parse_events([event(title)])[0]
                self.assertEqual((flight["number"], flight["deadhead"]), (number, deadhead))

    def test_dst_fall_back_explicit_offsets_and_overnight(self):
        result = parse_events([
            event("123: ATL-MEM", "2026-11-01T01:30:00-04:00", "2026-11-01T01:20:00-05:00"),
            event("456: ATL-LAX", "2026-11-01T23:30:00-05:00", "2026-11-02T01:30:00-08:00"),
        ])
        self.assertEqual(len(result), 2)
        elapsed = datetime.fromisoformat(result[0]["end"]) - datetime.fromisoformat(result[0]["start"])
        self.assertEqual(elapsed.total_seconds(), 50 * 60)
        self.assertTrue(result[1]["end"].startswith("2026-11-02"))

    def test_ambiguous_nonexistent_and_missing_timezone_are_not_guessed(self):
        events = []
        for local_start, local_end, zone in [
            ("2026-11-01T01:30:00", "2026-11-01T03:00:00", "America/New_York"),
            ("2026-03-08T02:30:00", "2026-03-08T04:00:00", "America/New_York"),
            ("2026-05-01T01:30:00", "2026-05-01T03:00:00", ""),
        ]:
            events.append({"summary": "123: ATL-MEM", "start": {"dateTime": local_start, "timeZone": zone},
                           "end": {"dateTime": local_end, "timeZone": zone}})
        self.assertEqual(parse_events(events), [])
        # Unambiguous local times with an explicit IANA zone are supported.
        events[0]["start"]["dateTime"] = "2026-11-01T03:30:00"
        events[0]["end"]["dateTime"] = "2026-11-01T05:00:00"
        self.assertEqual(len(parse_events(events)), 1)

    def test_cancellations_repeated_events_and_distinct_dates(self):
        events = [
            event("123: ATL-MEM", id="deleted", updated="2026-10-01T00:00:00Z"),
            {"id": "deleted", "status": "cancelled", "updated": "2026-10-02T00:00:00Z"},
            event("456: ATL-LAX", id="a", updated="2026-10-01T00:00:00Z"),
            event("456: ATL-LAX", id="a", updated="2026-10-02T00:00:00Z", description="duplicate"),
            event("456: ATL-LAX", id="b"),  # Same leg duplicated across sources.
            event("456: ATL-LAX", "2026-11-07T18:19:00Z", "2026-11-07T21:49:00Z"),
            event("789: ATL-MEM", status="cancelled"),
        ]
        result = parse_events(events)
        self.assertEqual(len(result), 2)
        self.assertEqual([item["number"] for item in result], ["456", "456"])
        self.assertNotEqual(result[0]["id"], result[1]["id"])

    def test_report_does_not_attach_across_duties(self):
        result = parse_events([
            event("Report: 0600L", "2026-11-06T11:00:00Z", "2026-11-06T15:00:00Z"),
            event("123: ATL-MEM"),
        ])
        self.assertNotIn("report", result[0])

    def test_all_day_and_ambiguous_multi_leg_summaries_are_ignored(self):
        events = [
            {"summary": "123: ATL-MEM", "start": {"date": "2026-11-06"}, "end": {"date": "2026-11-07"}},
            event("123: ATL-MEM / 456: MEM-ATL"),
            event("Vacation", description="123: ATL-MEM"),
            event("123: ATL-ATL"),
            event("123: ATL-MEM", end="2026-11-06T18:19:00Z"),
        ]
        self.assertEqual(parse_events(events), [])


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {"calendar_name": "FC View", "calendar_id": "", "demo": False}
        self.client = CalendarClient(self.root, self.config)
        _private_json(self.client.client_path, {"client_id": "test.apps.googleusercontent.com", "client_secret": "test-secret"})
        _private_json(self.client.token_path, {"client_id": "test.apps.googleusercontent.com", "access_token": "test-access",
                                             "refresh_token": "test-refresh", "expires_at": time.time() + 3600,
                                             "scopes": list(SCOPES)})

    def tearDown(self):
        self.directory.cleanup()

    def _mock_success(self, method, url, **kwargs):
        if "/calendarList?" in url:
            return {"items": [{"id": "calendar-opaque-id", "summary": "FC View", "accessRole": "reader"}]}
        return {"items": [event("123: ATL-MEM")]}

    def test_paginated_lists_and_events_private_cache(self):
        calls = []

        def response(method, url, **kwargs):
            calls.append((method, url, kwargs))
            query = parse_qs(urlsplit(url).query)
            if "/calendarList?" in url:
                if not query.get("pageToken"):
                    return {"items": [{"id": "other", "summary": "Personal"}], "nextPageToken": "cal-page-2"}
                self.assertEqual(query["pageToken"], ["cal-page-2"])
                return {"items": [{"id": "opaque@example.invalid", "summary": "FC View"}]}
            self.assertIn("opaque%40example.invalid/events", url)
            if not query.get("pageToken"):
                return {"items": [event("123: ATL-MEM", description="Private Hotel")], "nextPageToken": "event-page-2"}
            return {"items": [event("456: MEM-ATL", "2026-11-06T22:00:00Z", "2026-11-06T23:30:00Z")]}

        with patch.object(self.client, "_request_json", side_effect=response):
            snapshot = self.client.sync()
        self.assertIsNone(snapshot["error"])
        self.assertEqual(len(snapshot["flights"]), 2)
        self.assertFalse(snapshot["demo"])
        self.assertEqual(len(calls), 4)
        self.assertNotIn("Private Hotel", self.client.cache_path.read_text())
        for _method, url, _kwargs in calls:
            self.assertNotIn("description", parse_qs(urlsplit(url).query)["fields"][0])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.client.token_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.client.client_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.client.cache_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(CalendarClient(self.root, self.config).snapshot()["flights"], snapshot["flights"])

    def test_network_failure_preserves_successful_cache_and_timestamp(self):
        with patch.object(self.client, "_request_json", side_effect=self._mock_success):
            successful = self.client.sync()
        original = self.client.cache_path.read_bytes()
        with patch.object(self.client, "_request_json", side_effect=CalendarError("Google Calendar network unavailable; using the cached schedule.")):
            failed = self.client.sync()
        self.assertEqual(successful["flights"], failed["flights"])
        self.assertEqual(successful["last_sync"], failed["last_sync"])
        self.assertIn("network unavailable", failed["error"])
        self.assertEqual(self.client.cache_path.read_bytes(), original)

    def test_switching_calendar_cannot_display_previous_calendar_cache(self):
        with patch.object(self.client, "_request_json", side_effect=self._mock_success):
            self.client.sync()
        self.config["calendar_id"] = "other-calendar"
        self.assertEqual(self.client.snapshot()["flights"], [])
        self.assertIsNone(self.client.snapshot()["last_sync"])

    def test_ambiguous_calendar_name_requires_explicit_id(self):
        with patch.object(self.client, "list_calendars", return_value=[{"id": "one", "name": "FC View"}, {"id": "two", "name": "FC View"}]):
            result = self.client.sync()
        self.assertIn("Several calendars", result["error"])
        self.assertEqual(result["flights"], [])

    def test_refresh_retains_refresh_token_and_uses_read_only_scopes(self):
        token = json.loads(self.client.token_path.read_text())
        token["expires_at"] = 0
        _private_json(self.client.token_path, token)
        calls = []

        def response(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return {"access_token": "new-access", "expires_in": 3600, "token_type": "Bearer"}

        with patch.object(self.client, "_request_json", side_effect=response):
            self.assertEqual(self.client._access_token(), "new-access")
        self.assertEqual(calls[0][0:2], ("POST", TOKEN_URL))
        self.assertEqual(calls[0][2]["form"]["grant_type"], "refresh_token")
        updated = json.loads(self.client.token_path.read_text())
        self.assertEqual(updated["refresh_token"], "test-refresh")
        self.assertEqual(updated["scopes"], list(SCOPES))
        self.assertTrue(all(scope.endswith(".readonly") for scope in SCOPES))

    def test_401_refreshes_and_retries_once(self):
        with patch.object(self.client, "_access_token", side_effect=["old", "new"]) as access:
            with patch.object(self.client, "_request_json", side_effect=[_HTTPFailure(401), {"items": []}]) as request:
                self.assertEqual(self.client._api("/users/me/calendarList", {}), {"items": []})
        self.assertEqual(access.call_args_list[1].kwargs, {"force_refresh": True})
        self.assertEqual(request.call_count, 2)

    def test_partial_scope_grant_is_rejected_without_saving_token(self):
        existing = self.client.token_path.read_bytes()
        with self.assertRaisesRegex(CalendarError, "Both Google Calendar read permissions"):
            self.client._store_token({"access_token": "new", "refresh_token": "refresh", "scope": SCOPES[0]}, self.client._client())
        self.assertEqual(existing, self.client.token_path.read_bytes())

    def test_http_and_network_errors_never_echo_server_payload_or_url(self):
        private = "PRIVATE-REFRESH-TOKEN-and-user@example.invalid"
        for error in [URLError(private), HTTPError(TOKEN_URL, 400, private, {}, io.BytesIO(json.dumps({"error": "invalid_grant", "error_description": private}).encode()))]:
            with self.subTest(error=type(error).__name__):
                with patch.object(self.client._opener, "open", side_effect=error):
                    with self.assertRaises(CalendarError) as caught:
                        self.client._request_json("POST", TOKEN_URL, form={"refresh_token": private})
                self.assertNotIn(private, str(caught.exception))
                self.assertNotIn(TOKEN_URL, str(caught.exception))

    def test_repeated_page_token_fails_instead_of_looping(self):
        with patch.object(self.client, "_api", return_value={"items": [], "nextPageToken": "again"}) as request:
            with self.assertRaisesRegex(CalendarError, "pagination failed"):
                self.client._pages("/users/me/calendarList", {})
        self.assertEqual(request.call_count, 2)


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.client = CalendarClient(self.root / "private-data", {"calendar_name": "FC View"})
        self.credentials = self.root / "downloaded-client.json"
        self.credentials.write_text(json.dumps({"installed": {"client_id": "test.apps.googleusercontent.com", "client_secret": "test-client-secret",
                                                              "token_uri": "https://untrusted.example.invalid/token"}}))

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def _callback(port, path, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        response = connection.getresponse()
        result = (response.status, response.read())
        connection.close()
        return result

    def test_real_loopback_callback_checks_state_and_pkce_and_does_not_log_code(self):
        browser_thread = []
        auth_query = {}
        callbacks = []

        def on_url(url):
            self.assertTrue(url.startswith(AUTH_URL + "?"))
            auth_query.update(parse_qs(urlsplit(url).query))
            redirect = urlsplit(auth_query["redirect_uri"][0])
            self.assertEqual(redirect.hostname, "127.0.0.1")

            def browser():
                callbacks.append(self._callback(redirect.port, "/oauth2/callback?state=wrong&code=malicious"))
                callbacks.append(self._callback(redirect.port, "/oauth2/callback?state=" + auth_query["state"][0] + "&code=one-use-code"))

            thread = threading.Thread(target=browser)
            thread.start()
            browser_thread.append(thread)

        captured = {}

        def exchange(method, url, **kwargs):
            self.assertEqual((method, url), ("POST", TOKEN_URL))
            captured.update(kwargs["form"])
            return {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_in": 3600,
                    "scope": " ".join(SCOPES), "token_type": "Bearer"}

        with patch.object(self.client, "_request_json", side_effect=exchange):
            result = self.client.authorize(self.credentials, open_browser=False, callback_port=0, timeout=3, on_url=on_url)
        for thread in browser_thread:
            thread.join(timeout=3)
        self.assertTrue(result["authorized"])
        self.assertEqual([status for status, _body in callbacks], [403, 200])
        self.assertEqual(auth_query["code_challenge_method"], ["S256"])
        self.assertEqual(set(auth_query["scope"][0].split()), set(SCOPES))
        self.assertEqual(auth_query["access_type"], ["offline"])
        self.assertEqual(captured["code"], "one-use-code")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(captured["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, auth_query["code_challenge"][0])
        self.assertNotIn(captured["code_verifier"], json.dumps(auth_query))
        self.assertNotIn("one-use-code", self.client.token_path.read_text())
        self.assertNotIn("untrusted.example.invalid", self.client.client_path.read_text())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.client.token_path.stat().st_mode), 0o600)
        # The installation guide uploads credentials at this same destination.
        # A later authorization must still accept the persisted Desktop JSON.
        self.assertEqual(json.loads(self.client.client_path.read_text())["installed"]["client_id"], self.client._client()["client_id"])
        with self.assertRaisesRegex(CalendarError, "timed out"):
            self.client.authorize(self.client.client_path, False, callback_port=0, timeout=0.01, on_url=lambda _url: None)

    def test_oauth_timeout_does_not_create_credentials(self):
        with self.assertRaisesRegex(CalendarError, "timed out"):
            self.client.authorize(self.credentials, False, callback_port=0, timeout=0.03, on_url=lambda _url: None)
        self.assertFalse(self.client.token_path.exists())
        self.assertFalse(self.client.client_path.exists())

    def test_web_client_is_rejected_with_actionable_message(self):
        self.credentials.write_text(json.dumps({"web": {"client_id": "wrong-type"}}))
        with self.assertRaisesRegex(CalendarError, "Desktop app"):
            self.client.authorize(self.credentials, False, on_url=lambda _url: None)


if __name__ == "__main__":
    unittest.main()
