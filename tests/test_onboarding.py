import io
import json
import os
from email.message import Message
from http.cookies import SimpleCookie
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from flightwall.config import ConfigStore
from flightwall.onboarding import OnboardingRouter, SCOPES


ORIGIN = "https://board.example.test"
ADMIN = "example-admin-token-used-only-in-tests"


class Handler:
    def __init__(self, body=None, headers=None):
        self.headers = Message()
        for name, value in {"Host": "board.example.test", "Origin": ORIGIN,
                            "Content-Type": "application/json", **(headers or {})}.items():
            if value is not None:
                self.headers[name] = value
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers["Content-Length"] = str(len(raw))
        self.rfile, self.wfile = io.BytesIO(raw), io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.reply_headers = {}
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.reply_headers[name] = value

    def end_headers(self):
        pass

    def value(self):
        return json.loads(self.wfile.getvalue())


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.config = ConfigStore(self.data)
        self.config.update({"public_url": ORIGIN})
        (self.data / "control-token.txt").write_text(ADMIN)
        self.choices = {"chris": [{"id": "flight.example", "name": "FC View"}, {"id": "shared.example", "name": "Shared schedule"}],
                        "lisa": [{"id": "lisa.example", "name": "Lisa's chosen calendar"}]}
        self.callbacks = {"setup_get": lambda: {"presence": {"chris": {"state": "unknown"}}, "access_token": "should-never-appear", "health_query": "private-research-example"},
                          "setup_save": self.config.update, "setup_calendars": lambda person: self.choices[person]}
        self.router = OnboardingRouter(self.data, self.config.get, self.callbacks)
        self.cookie, self.csrf = "", ""

    def tearDown(self):
        self.temporary.cleanup()

    def call(self, path, body=None, *, method="POST", headers=None, authenticated=True):
        actual = {}
        if authenticated:
            actual.update(Cookie=self.cookie, **{"X-Setup-CSRF": self.csrf})
        actual.update(headers or {})
        handler = Handler(body, actual)
        self.assertTrue(self.router.handle(handler, method, path))
        return handler

    def login(self):
        handler = self.call("/setup/login", {"token": ADMIN}, authenticated=False)
        self.assertEqual(handler.status, 200)
        self.cookie = handler.reply_headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = self.call("/setup/status", method="GET").value()["csrf"]
        return handler

    def consent(self, person="lisa"):
        return {"consenting_person": person, "owner_consent": True}

    def test_dinner_generation_has_its_own_consent_and_cannot_enable_lisa_features(self):
        self.login()
        patch = {"dinner_options_enabled": True, "dinner_options_monthly_limit_usd": 3,
                 "dinner_options_servings": 8, "dinner_options_max_minutes": 60,
                 "dinner_options_preferences": "Fictional pasta; extra chili at the table"}
        denied = self.call('/setup/settings', {"settings": patch})
        self.assertEqual(denied.status, 400)
        self.assertFalse(self.config.get()['dinner_options_enabled'])
        saved = self.call('/setup/settings', {"settings": patch, **self.consent('chris')})
        self.assertEqual(saved.status, 200)
        actual = self.config.get()
        self.assertTrue(actual['dinner_options_enabled'])
        for name in ('openai_enabled', 'inspiration_enabled', 'lisa_private_enabled', 'lisa_gmail_enabled'):
            self.assertFalse(actual[name])
        rejected = self.call('/setup/settings', {"settings": {"dinner_options_monthly_limit_usd": 4}})
        self.assertEqual(rejected.status, 400)

    def test_phone_recipe_read_requires_an_authenticated_setup_session(self):
        self.callbacks['get_dinner_options'] = lambda: {"status": "ready", "date": "2026-11-01", "options": [
            {"number": 1, "title": "Fictional dinner", "ingredients": ['Rice'], "steps": ['Cook rice']}]}
        denied = self.call('/setup/dinner-options', method='GET', authenticated=False)
        self.assertEqual(denied.status, 401)
        self.login()
        result = self.call('/setup/dinner-options', method='GET')
        self.assertEqual(result.status, 200)
        self.assertEqual(result.value()['options'][0]['ingredients'], ['Rice'])

    def client_body(self, person="lisa", service="calendar"):
        return {"person": person, "service": service, "credentials": {"web": {
            "client_id": "123456789012-example.apps.googleusercontent.com", "client_secret": "example-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [ORIGIN + "/setup/oauth/callback"]}}, **self.consent(person)}

    def start_google(self, person="lisa", service="calendar"):
        self.assertEqual(self.call("/setup/google/client", self.client_body(person, service)).status, 200)
        result = self.call("/setup/google/start", {"person": person, "service": service, **self.consent(person)})
        self.assertEqual(result.status, 200)
        return parse_qs(urlsplit(result.value()["authorization_url"]).query)

    def token_response(self, service="calendar"):
        return {"access_token": "example-access", "refresh_token": "example-refresh", "token_type": "Bearer",
                "expires_in": 3600, "scope": " ".join(SCOPES[service])}

    def test_generic_setup_page_and_uppercase_alias_expose_no_household_data(self):
        for path in ("/setup", "/SETUP"):
            handler = self.call(path, method="GET", authenticated=False)
            self.assertEqual(handler.status, 200)
            self.assertIn(b"Make it", handler.wfile.getvalue())
            self.assertNotIn(ADMIN.encode(), handler.wfile.getvalue())
            self.assertNotIn(b"private-research-example", handler.wfile.getvalue())

    def test_login_cookie_is_secure_httponly_strict_without_token_echo(self):
        handler = self.login()
        cookie = handler.reply_headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("; Secure", cookie)
        self.assertNotIn(ADMIN, handler.wfile.getvalue().decode())
        self.assertIn("no-store", handler.reply_headers["Cache-Control"])

    def test_localhost_exception_only_for_loopback_not_unencrypted_lan(self):
        handler = self.call("/setup/login", {"token": ADMIN}, authenticated=False,
                            headers={"Host": "localhost:8765", "Origin": "http://localhost:8765"})
        self.assertEqual(handler.status, 200)
        self.assertNotIn("; Secure", handler.reply_headers["Set-Cookie"])
        handler = self.call("/setup/login", {"token": ADMIN}, authenticated=False,
                            headers={"Host": "192.0.2.10:8765", "Origin": "http://192.0.2.10:8765"})
        self.assertEqual(handler.status, 400)

    def test_csrf_and_origin_required_for_all_authenticated_posts(self):
        self.login()
        for headers in ({"X-Setup-CSRF": "wrong"}, {"Origin": "https://other.example.test"}, {"Origin": None}):
            with self.subTest(headers=headers):
                handler = self.call("/setup/settings", {"settings": {"rotation_seconds": 30}}, headers=headers)
                self.assertEqual(handler.status, 403)
        self.assertNotEqual(self.config.get()["rotation_seconds"], 30)

    def test_transfer_encoding_and_duplicate_length_rejected_and_connection_closed(self):
        self.login()
        handler = self.call("/setup/settings", {"settings": {"rotation_seconds": 30}}, headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(handler.status, 400)
        self.assertTrue(handler.close_connection)
        handler = Handler({"settings": {"rotation_seconds": 30}}, {"Cookie": self.cookie, "X-Setup-CSRF": self.csrf})
        handler.headers["Content-Length"] = "3"
        self.router.handle(handler, "POST", "/setup/settings")
        self.assertEqual(handler.status, 400)
        self.assertTrue(handler.close_connection)

    def test_status_never_returns_saved_keys_tokens_or_private_research_query(self):
        self.login()
        self.config.update({"health_query": "private-health-query"})
        self.assertEqual(self.call("/setup/openai/key", {"value": "sk-example-private-api-key-123456789"}).status, 200)
        handler = self.call("/setup/status", method="GET")
        raw = handler.wfile.getvalue().decode()
        for secret in (ADMIN, "sk-example-private-api-key-123456789", "should-never-appear", "private-health-query", "private-research-example"):
            self.assertNotIn(secret, raw)
        self.assertTrue(handler.value()["openai_key_saved"])
        self.assertTrue(handler.value()["health_query_saved"])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((self.data / "openai.key").stat().st_mode), 0o600)

    def test_google_web_pkce_flow_bound_to_person_and_exact_readonly_scopes(self):
        self.login()
        values = self.start_google()
        self.assertEqual(set(values["scope"][0].split()), set(SCOPES["calendar"]))
        self.assertEqual(values["code_challenge_method"], ["S256"])
        self.assertIn("select_account", values["prompt"][0])
        with patch("flightwall.onboarding._json_request", return_value=self.token_response()) as exchange:
            result = self.call("/setup/oauth/callback?" + urlencode({"state": values["state"][0], "code": "example-code"}),
                               method="GET", authenticated=False)
        self.assertEqual(result.status, 200)
        self.assertEqual(exchange.call_args.args[0], "https://oauth2.googleapis.com/token")
        self.assertEqual(exchange.call_args.kwargs["form"]["redirect_uri"], ORIGIN + "/setup/oauth/callback")
        self.assertGreaterEqual(len(exchange.call_args.kwargs["form"]["code_verifier"]), 43)
        path = self.data / "accounts" / "lisa" / "google-token.json"
        self.assertEqual(set(json.loads(path.read_text())["scopes"]), set(SCOPES["calendar"]))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse((self.data / "accounts" / "lisa" / "gmail-token.json").exists())
        self.assertNotIn("example-access", result.wfile.getvalue().decode())

    def test_google_callback_state_single_use_and_does_not_need_strict_cookie(self):
        self.login()
        state = self.start_google()["state"][0]
        url = "/setup/oauth/callback?" + urlencode({"state": state, "code": "example-code"})
        with patch("flightwall.onboarding._json_request", return_value=self.token_response()) as exchange:
            self.assertEqual(self.call(url, method="GET", authenticated=False).status, 200)
            self.assertEqual(self.call(url, method="GET", authenticated=False).status, 400)
            exchange.assert_called_once()

    def test_google_rejects_wrong_provider_endpoints_and_broader_scopes(self):
        self.login()
        client = self.client_body()
        client["credentials"]["web"]["token_uri"] = "https://other.example.test/token"
        self.assertEqual(self.call("/setup/google/client", client).status, 400)
        self.assertFalse((self.data / "accounts" / "lisa" / "google-client.json").exists())
        state = self.start_google()["state"][0]
        response = self.token_response()
        response["scope"] += " " + SCOPES["gmail"][0]
        with patch("flightwall.onboarding._json_request", return_value=response):
            result = self.call("/setup/oauth/callback?" + urlencode({"state": state, "code": "example-code"}), method="GET", authenticated=False)
        self.assertEqual(result.status, 400)
        self.assertFalse((self.data / "accounts" / "lisa" / "google-token.json").exists())

    def test_desktop_authorization_still_works_if_new_web_signin_is_cancelled(self):
        self.login()
        directory = self.data / "accounts" / "chris"
        directory.mkdir(parents=True)
        old_client = {"installed": {"client_id": "previous-desktop-client", "client_secret": "previous-secret"}}
        old_token = {"client_id": "previous-desktop-client", "access_token": "previous-access",
                     "refresh_token": "previous-refresh", "scopes": list(SCOPES["calendar"])}
        (directory / "google-client.json").write_text(json.dumps(old_client))
        (directory / "google-token.json").write_text(json.dumps(old_token))
        self.assertTrue(self.router._saved_authorization("chris", "calendar"))
        values = self.start_google(person="chris")
        self.assertEqual(json.loads((directory / "google-client.json").read_text()), old_client)
        result = self.call("/setup/oauth/callback?" + urlencode({"state": values["state"][0], "error": "access_denied"}), method="GET", authenticated=False)
        self.assertEqual(result.status, 400)
        self.assertTrue(self.router._saved_authorization("chris", "calendar"))
        self.assertEqual(json.loads((directory / "google-token.json").read_text()), old_token)

    def test_lisa_gmail_and_private_features_require_her_explicit_consent(self):
        self.login()
        body = self.client_body(service="gmail")
        body.update(owner_consent=False)
        self.assertEqual(self.call("/setup/google/client", body).status, 400)
        for settings in ({"lisa_private_enabled": True}, {"health_enabled": True}, {"health_query": "chosen-topic"}, {"openai_enabled": True}):
            self.assertEqual(self.call("/setup/settings", {"settings": settings}).status, 400)
        self.assertFalse(self.config.get()["lisa_private_enabled"])

    def test_calendar_roles_are_selected_only_from_verified_actual_list(self):
        self.login()
        self.assertEqual(self.call("/setup/settings", {"settings": {"calendar_id": "invented.example"}}).status, 400)
        result = self.call("/setup/settings", {"settings": {"calendar_id": "flight.example", "calendar_name": "not trusted"}})
        self.assertEqual(result.status, 200)
        self.assertEqual(self.config.get()["calendar_name"], "FC View")
        self.assertFalse(self.config.get()["demo"])
        self.assertEqual(self.call("/setup/settings", {"settings": {"calendar_id": ""}}).status, 400)
        self.assertEqual(self.call("/setup/settings", {"settings": {"kids_filter_enabled": True}}).status, 400)
        self.assertEqual(self.call("/setup/settings", {"settings": {"family_calendar_profile": "lisa", "family_calendar_id": "lisa.example", "kids_filter_enabled": True}}).status, 200)

    def test_openai_test_sends_only_generic_input_never_household_data(self):
        self.login()
        self.call("/setup/openai/key", {"value": "sk-example-private-api-key-123456789"})
        self.config.update({"health_query": "private-research-query"})
        with patch("flightwall.onboarding._json_request", return_value={"id": "example-response"}) as request:
            self.assertEqual(self.call("/setup/openai/test", {}).status, 200)
        self.assertEqual(request.call_args.kwargs["payload"]["input"], "Reply with OK.")
        self.assertFalse(request.call_args.kwargs["payload"]["store"])
        self.assertNotIn("private-research", json.dumps(request.call_args.kwargs["payload"]))

    def test_presence_pairing_requires_own_consent_and_never_reveals_existing_token(self):
        self.login()
        self.assertEqual(self.call("/setup/phone/pair", {"person": "lisa", **self.consent("chris")}).status, 400)
        result = self.call("/setup/phone/pair", {"person": "lisa", **self.consent()})
        self.assertEqual(result.status, 200)
        token = result.value()["token"]
        self.assertIn(ORIGIN + "/api/v1/presence/lisa", result.value()["endpoint"])
        self.assertNotIn(token, self.call("/setup/status", method="GET").wfile.getvalue().decode())

    def test_finish_marker_is_private_and_logout_revokes_cookie(self):
        self.login()
        self.assertEqual(self.call("/setup/finish", {}).status, 200)
        marker = self.data / "setup-complete.json"
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertEqual(self.call("/setup/logout", {}).status, 200)
        self.assertEqual(self.call("/setup/status", method="GET").status, 401)

    def test_rotating_admin_token_revokes_setup_session_and_pending_google_flow(self):
        self.login()
        state = self.start_google()["state"][0]
        (self.data / "control-token.txt").write_text("replacement-admin-secret")
        self.assertEqual(self.call("/setup/status", method="GET").status, 401)
        with patch("flightwall.onboarding._json_request") as exchange:
            result = self.call("/setup/oauth/callback?" + urlencode({"state": state, "code": "example-code"}), method="GET", authenticated=False)
        self.assertEqual(result.status, 400)
        exchange.assert_not_called()

    def test_rejected_unconsumed_post_body_closes_connection(self):
        self.login()
        handler = self.call("/setup/settings", {"settings": {"rotation_seconds": 30}}, headers={"X-Setup-CSRF": "wrong"})
        self.assertEqual(handler.status, 403)
        self.assertEqual(handler.reply_headers["Connection"], "close")
        self.assertTrue(handler.close_connection)


if __name__ == "__main__":
    unittest.main()
