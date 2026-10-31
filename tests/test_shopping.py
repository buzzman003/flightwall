"""Synthetic store fixes and mocked transport only; no household data or SMS."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from flightwall.shopping import (AlexaShoppingListUnavailable, ManualShoppingList,
                                 ShoppingAlerts, ShoppingError, ShoppingListStore, ShoppingUnavailable,
                                 TwilioSmsSender, _NoRedirect, shopping_message, validate_stores)

UTC = timezone.utc


class Sender:
    def __init__(self, status="accepted", ready=True):
        self.messages = []
        self.outcome = status
        self.configured = ready

    def ready(self):
        return self.configured

    def send(self, message, *, request_id):
        self.messages.append((message, request_id))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return {"status": self.outcome}


class ShoppingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"shopping_enabled": True, "shopping_lisa_opt_in": True,
                       "presence_enabled": True, "shopping_list_source": "manual",
                       "shopping_stores": [
                           {"id": "synthetic-one", "verified": True, "latitude": 0,
                            "longitude": 0, "radius_m": 50},
                           {"id": "synthetic-two", "verified": True, "latitude": 0,
                            "longitude": 1, "radius_m": 50}]}
        self.list = ManualShoppingList(self.root)
        self.list.set_items(["Pasta", "Apples"])
        self.sender = Sender()
        self.alerts = self.engine()
        self.start = datetime(2026, 10, 31, 17, tzinfo=UTC)

    def engine(self, **kwargs):
        return ShoppingAlerts(self.root, lambda: self.config, sms_sender=self.sender, **kwargs)

    def report(self, seconds=0, lon=0, acc=5, now=None, **changes):
        now = now or self.start + timedelta(seconds=seconds)
        payload = {"_type": "location", "lat": 0, "lon": lon, "acc": acc, "tst": now.timestamp()}
        payload.update(changes)
        return self.alerts.consume_location("lisa", payload, now)

    def test_disabled_defaults_do_not_read_location_or_create_state(self):
        fresh = self.root / "fresh"
        alerts = ShoppingAlerts(fresh, lambda: {})
        self.assertEqual(alerts.consume_location("lisa", None), {"status": "disabled"})
        self.assertFalse(alerts.status()["enabled"])
        self.assertFalse(fresh.exists())

    def test_all_three_opt_in_conditions_are_required(self):
        for key in ("shopping_enabled", "shopping_lisa_opt_in", "presence_enabled"):
            with self.subTest(key=key):
                self.config[key] = False
                self.assertEqual(self.report()["status"], "disabled")
                self.config[key] = True
        self.assertFalse(self.sender.messages)

    def test_only_lisa_authenticated_location_path_is_accepted(self):
        self.assertEqual(self.alerts.consume_location("chris", {}, self.start)["status"], "wrong_person")
        for value in ({"_type": "transition", "event": "enter"}, {"state": "home"}, None):
            self.assertEqual(self.alerts.consume_location("lisa", value, self.start)["status"],
                             "unsupported_observation")
        self.assertFalse(self.sender.messages)

    def test_freshness_replay_and_accuracy(self):
        for delta in (-300, 121):
            self.assertEqual(self.report(tst=(self.start + timedelta(seconds=delta)).timestamp())["status"],
                             "stale_or_future_observation")
        self.assertEqual(self.report(acc=70)["status"], "uncertain_location")
        self.assertFalse(self.sender.messages)
        self.assertEqual(self.report(1)["status"], "accepted")
        self.assertEqual(self.report(1)["status"], "duplicate_or_old_observation")
        self.assertEqual(len(self.sender.messages), 1)

    def test_invalid_numbers_never_send(self):
        for value in ({"lat": float("nan")}, {"lon": float("inf")}, {"acc": -1},
                      {"acc": None}, {"tst": True}, {"lat": 91}):
            with self.subTest(value=value), self.assertRaises(ShoppingError):
                self.report(**value)
        self.assertFalse(self.sender.messages)

    def test_unverified_or_unbounded_geofences_fail_closed(self):
        self.config["shopping_stores"][0]["verified"] = False
        with self.assertRaises(ShoppingError):
            self.report()
        self.config["shopping_stores"] = []
        self.assertEqual(self.report()["status"], "stores_unconfigured")
        self.assertFalse(self.sender.messages)

    def test_geofence_json_string_matches_existing_settings_storage(self):
        self.config["shopping_stores"] = json.dumps(self.config["shopping_stores"])
        self.assertEqual(self.report()["status"], "accepted")

    def test_invalid_geofence_json_string_fails_closed(self):
        for value in ("{bad", "{}", "x" * 70000):
            self.config["shopping_stores"] = value
            with self.subTest(value=value[:20]), self.assertRaises(ShoppingError):
                self.report()
        self.assertFalse(self.sender.messages)

    def test_daily_cap_is_shared_by_all_stores_and_restarts(self):
        self.assertEqual(self.report()["status"], "accepted")
        self.assertEqual(self.report(1, lon=0.5)["status"], "outside")
        self.alerts = self.engine()
        self.assertEqual(self.report(2, lon=1)["status"], "daily_limit")
        self.assertEqual(self.report(3, lon=1)["status"], "daily_limit")
        self.assertEqual(len(self.sender.messages), 1)

    def test_day_is_chicago_not_utc(self):
        before = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
        self.assertEqual(self.report(now=before)["status"], "accepted")
        self.report(now=before + timedelta(minutes=1), lon=0.5)
        self.assertEqual(self.report(now=before + timedelta(minutes=2))["status"], "daily_limit")
        self.report(now=datetime(2026, 11, 1, 4, 59, tzinfo=UTC), lon=0.5)
        self.assertEqual(self.report(now=datetime(2026, 11, 1, 5, 1, tzinfo=UTC))["status"], "accepted")
        self.assertEqual(len(self.sender.messages), 2)

    def test_dst_fall_back_does_not_reset_daily_cap(self):
        first = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
        self.assertEqual(self.report(now=first)["status"], "accepted")
        self.report(now=first + timedelta(minutes=30), lon=0.5)
        self.assertEqual(self.report(now=first + timedelta(hours=1))["status"], "daily_limit")
        self.assertEqual(len(self.sender.messages), 1)

    def test_staying_in_store_after_midnight_is_not_new_arrival(self):
        before = datetime(2026, 10, 31, 4, 59, tzinfo=UTC)
        self.assertEqual(self.report(now=before)["status"], "accepted")
        after = before + timedelta(minutes=2)
        self.assertEqual(self.report(now=after)["status"], "no_new_arrival")
        self.report(now=after + timedelta(seconds=1), lon=0.5)
        self.assertEqual(self.report(now=after + timedelta(seconds=2))["status"], "accepted")

    def test_native_alexa_list_remains_unavailable_without_silent_source_change(self):
        self.config["shopping_list_source"] = "alexa"
        self.assertEqual(self.report()["status"], "list_source_unavailable")
        self.assertFalse(self.sender.messages)
        with self.assertRaises(ShoppingUnavailable):
            AlexaShoppingListUnavailable().get_items()

    def test_explicit_flightwall_source_uses_owners_private_list(self):
        self.config["shopping_list_source"] = "flightwall"
        ShoppingListStore(self.root).add({"item": "Bananas", "source": "alexa"})
        self.assertEqual(self.report()["status"], "accepted")
        self.assertIn("Bananas", self.sender.messages[0][0])
        self.assertNotIn("Pasta", self.sender.messages[0][0])

    def test_source_becomes_available_during_same_visit_without_second_arrival(self):
        self.list.set_items([])
        self.assertEqual(self.report()["status"], "list_source_unavailable")
        self.list.set_items(["Pasta"])
        self.assertEqual(self.report(1)["status"], "accepted")

    def test_provider_credentials_required_without_consuming_daily_dispatch(self):
        self.sender.configured = False
        self.assertEqual(self.report()["status"], "sms_unconfigured")
        self.assertIsNone(self.alerts.status()["last_dispatch_date"])
        self.sender.configured = True
        self.assertEqual(self.report(1)["status"], "accepted")

    def test_provider_error_or_timeout_never_automatically_retries(self):
        self.sender.outcome = TimeoutError("private provider detail")
        self.assertEqual(self.report()["status"], "unconfirmed")
        self.alerts = self.engine()
        self.assertEqual(self.report(1)["status"], "daily_limit")
        self.assertEqual(len(self.sender.messages), 1)
        self.assertNotIn("private provider detail", self.alerts.path.read_text())

    def test_crash_after_reservation_does_not_allow_another_send(self):
        self.sender.outcome = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.report()
        self.alerts = self.engine()
        self.assertEqual(self.alerts.status()["last_dispatch_status"], "reserved")
        self.assertEqual(self.report(1)["status"], "daily_limit")
        self.assertEqual(len(self.sender.messages), 1)

    def test_concurrent_instances_dispatch_only_once(self):
        engines = [self.engine() for _ in range(4)]
        payload = {"_type": "location", "lat": 0, "lon": 0, "acc": 5, "tst": self.start.timestamp()}
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda engine: engine.consume_location("lisa", payload, self.start), engines))
        self.assertEqual(sum(item["status"] == "accepted" for item in results), 1)
        self.assertEqual(len(self.sender.messages), 1)

    def test_corrupt_state_stops_instead_of_resetting_daily_limit(self):
        self.alerts.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ShoppingError):
            self.report()
        self.assertIn("error", self.alerts.status())
        self.assertFalse(self.sender.messages)

    def test_state_and_status_exclude_coordinates_store_and_message(self):
        self.report()
        state = json.loads(self.alerts.path.read_text())
        self.assertEqual(set(state), {"version", "last_observed_at", "inside", "pending_arrival_date",
                                      "dispatch_date", "dispatch_status"})
        status = self.alerts.status()
        self.assertNotIn("inside", status)
        for value in ("Pasta", "synthetic-one", "latitude", "longitude", "to_number"):
            self.assertNotIn(value, json.dumps(state) + json.dumps(status))

    def test_public_catalogue_is_research_and_cannot_enable_a_geofence(self):
        path = Path(__file__).resolve().parents[1] / "assets" / "stores" / "emerald-coast-research.json"
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(catalogue["all_geofences_disabled"])
        for store in catalogue["stores"]:
            self.assertIs(store["verified"], False)
            self.assertIsNone(store["latitude"])
            self.assertIsNone(store["longitude"])
        self.config["shopping_stores"] = catalogue["stores"]
        with self.assertRaises(ShoppingError):
            self.report()

    def test_revoked_consent_during_list_read_prevents_send(self):
        owner = self

        class RevokingSource:
            source_id = "manual"

            def get_items(self):
                owner.config["shopping_lisa_opt_in"] = False
                return ["Pasta"]

        self.alerts = self.engine(list_source=RevokingSource())
        self.assertEqual(self.report()["status"], "disabled")
        self.assertFalse(self.sender.messages)

    def test_overlong_lists_are_rejected_not_silently_truncated(self):
        with self.assertRaises(ShoppingUnavailable):
            shopping_message(["x" * 120] * 20)
        for item in ("a\nb", "a\u2028b", "", "x" * 121):
            with self.assertRaises(ShoppingError):
                shopping_message([item])


class FakeResponse:
    status = 201

    def __init__(self, body=None):
        self.body = body if body is not None else json.dumps({"sid": "SM" + "1" * 32}).encode()

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class TwilioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "shopping"
        self.directory.mkdir()
        self.credentials = {"account_sid": "AC" + "a" * 32, "api_key_sid": "SK" + "b" * 32,
                            "api_key_secret": "synthetic-not-real-secret", "messaging_service_sid": "MG" + "c" * 32,
                            "to_number": "+12025550123"}
        self.path = self.directory / "sms.json"
        self.path.write_text(json.dumps(self.credentials))
        self.opener = FakeOpener()
        self.sender = TwilioSmsSender(self.root, opener=self.opener)

    def test_fixed_https_host_bounded_post_and_accepted_is_not_delivered(self):
        result = self.sender.send("Synthetic test", request_id="a" * 64)
        self.assertEqual(result, {"status": "accepted"})
        request, timeout = self.opener.calls[0]
        self.assertEqual(request.full_url, "https://api.twilio.com/2010-04-01/Accounts/AC" + "a" * 32 + "/Messages.json")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 5)
        body = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(set(body), {"To", "MessagingServiceSid", "Body"})
        self.assertEqual(body["Body"], ["Synthetic test"])
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))

    def test_credential_endpoint_injection_rejected_before_network(self):
        self.credentials["account_sid"] = "https://malicious.invalid/"
        self.path.write_text(json.dumps(self.credentials))
        self.assertFalse(self.sender.ready())
        with self.assertRaises(ShoppingUnavailable):
            self.sender.send("Synthetic", request_id="a" * 64)
        self.assertFalse(self.opener.calls)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.invalid"))

    def test_default_transport_disables_environment_proxies(self):
        with patch("flightwall.shopping.urllib.request.build_opener") as build:
            TwilioSmsSender(self.root)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], _NoRedirect)

    def test_unknown_errors_response_shape_and_overlarge_bodies_are_unconfirmed(self):
        for response in (TimeoutError("contains private token"), FakeResponse(b"bad json"),
                         FakeResponse(b"x" * 70000), FakeResponse(b'{"sid":"not-valid"}')):
            opener = FakeOpener(response)
            sender = TwilioSmsSender(self.root, opener=opener)
            self.assertEqual(sender.send("Synthetic", request_id="a" * 64), {"status": "unconfirmed"})
            self.assertEqual(len(opener.calls), 1)


class ShoppingListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ShoppingListStore(self.root)
        self.now = datetime(2026, 10, 31, 17, tzinfo=UTC)

    def test_add_remove_snapshot_and_clear(self):
        added = self.store.add({"item": "  Whole   milk ", "source": "alexa", "request_id": "add-1"}, self.now)
        self.assertTrue(added["changed"])
        self.assertEqual(added["item"]["name"], "Whole milk")
        self.assertEqual(self.store.get_items(), ["Whole milk"])
        self.assertEqual(set(self.store.snapshot()), {"items", "updated_at"})
        removed = self.store.remove({"id": added["item"]["id"], "request_id": "remove-1"}, self.now)
        self.assertTrue(removed["changed"])
        self.store.add({"item": "Pasta"}, self.now)
        self.assertTrue(self.store.clear({"confirmation": True}, self.now)["changed"])
        self.assertEqual(self.store.get_items(), [])

    def test_same_name_is_not_duplicated_and_remove_is_case_insensitive(self):
        self.store.add({"item": "Pasta"}, self.now)
        result = self.store.add({"item": "PASTA"}, self.now)
        self.assertFalse(result["changed"])
        self.assertEqual(self.store.get_items(), ["Pasta"])
        self.assertTrue(self.store.remove({"item": "pasta"}, self.now)["changed"])
        self.assertFalse(self.store.remove({"item": "pasta"}, self.now)["changed"])

    def test_request_retry_survives_restart_without_resurrecting_removed_item(self):
        request = {"item": "Pasta", "source": "alexa", "request_id": "amazon-private-request-id"}
        self.store.add(request, self.now)
        self.store.remove({"item": "Pasta"}, self.now)
        self.store = ShoppingListStore(self.root)
        repeated = self.store.add(request, self.now)
        self.assertTrue(repeated["duplicate"])
        self.assertFalse(repeated["changed"])
        self.assertEqual(self.store.get_items(), [])
        self.assertNotIn("amazon-private-request-id", self.store.path.read_text())

    def test_request_reuse_with_new_action_or_content_is_rejected(self):
        self.store.add({"item": "Pasta", "request_id": "same"}, self.now)
        for action, payload in ((self.store.add, {"item": "Milk", "request_id": "same"}),
                                (self.store.remove, {"item": "Pasta", "request_id": "same"}),
                                (self.store.clear, {"confirmation": True, "request_id": "same"})):
            with self.assertRaises(ShoppingError):
                action(payload, self.now)
        self.assertEqual(self.store.get_items(), ["Pasta"])

    def test_duplicate_clear_cannot_clear_items_added_after_first_clear(self):
        self.store.add({"item": "Pasta"}, self.now)
        request = {"confirmation": True, "request_id": "clear-once"}
        self.store.clear(request, self.now)
        self.store.add({"item": "Milk"}, self.now)
        self.assertTrue(self.store.clear(request, self.now)["duplicate"])
        self.assertEqual(self.store.get_items(), ["Milk"])

    def test_clear_requires_explicit_boolean_confirmation(self):
        self.store.add({"item": "Pasta"}, self.now)
        for value in ({}, {"confirmation": "yes"}, {"confirmation": 1}):
            with self.assertRaises(ShoppingError):
                self.store.clear(value, self.now)
        self.assertEqual(self.store.get_items(), ["Pasta"])

    def test_limit_validation_and_no_arbitrary_fields(self):
        for value in ({"item": ""}, {"item": "x" * 121}, {"item": "a\nb"}, {"item": "pasta", "url": "https://bad.invalid"},
                      {"item": "pasta", "source": "import"}, {"item": "pasta", "request_id": ""}):
            with self.assertRaises(ShoppingError):
                self.store.add(value, self.now)
        for value in ({}, {"item": "pasta", "id": "a" * 24}, {"id": "bad"}):
            with self.assertRaises(ShoppingError):
                self.store.remove(value, self.now)
        for index in range(100):
            self.store.add({"item": "Item " + str(index)}, self.now)
        with self.assertRaises(ShoppingUnavailable):
            self.store.add({"item": "one too many"}, self.now)

    def test_snapshot_is_detached_from_private_state(self):
        self.store.add({"item": "Pasta"}, self.now)
        snapshot = self.store.snapshot()
        snapshot["items"][0]["name"] = "Changed"
        self.assertEqual(self.store.get_items(), ["Pasta"])

    def test_cross_instance_concurrency_preserves_distinct_updates(self):
        stores = [ShoppingListStore(self.root) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda pair: pair[1].add({"item": "Item " + str(pair[0])}, self.now), enumerate(stores)))
        self.assertEqual(len(self.store.get_items()), 4)

    def test_ledger_expiry_does_not_remove_current_items(self):
        self.store.add({"item": "Pasta", "request_id": "old"}, self.now)
        self.store.add({"item": "Milk", "request_id": "new"}, self.now + timedelta(days=31))
        self.assertEqual(self.store.get_items(), ["Pasta", "Milk"])
        self.assertEqual(len(json.loads(self.store.path.read_text())["requests"]), 1)

    def test_corrupt_private_list_is_not_replaced(self):
        self.store.directory.mkdir()
        self.store.path.write_text('{"items":[]}', encoding="utf-8")
        with self.assertRaises(ShoppingError):
            self.store.add({"item": "Pasta"}, self.now)
        self.assertEqual(self.store.path.read_text(), '{"items":[]}')

    def test_store_validator_rejects_extra_fields_and_disabled_candidates(self):
        self.assertEqual(validate_stores("[]"), "[]")
        for value in ([{"id": "candidate", "verified": False}],
                      [{"id": "synthetic", "verified": True, "latitude": 0, "longitude": 0, "url": "bad"}]):
            with self.assertRaises(ShoppingError):
                validate_stores(value)


if __name__ == "__main__":
    unittest.main()
