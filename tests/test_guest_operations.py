from datetime import date
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flightwall.guest_operations import GuestOperationsError, fetch_operations, project_operations


TODAY = date(2026, 10, 31)
LISTING = "00000000-0000-4000-8000-000000000001"


def stay(identifier, arrival, departure, **extra):
    return {"id": identifier, "arrival_date": arrival, "departure_date": departure,
            "reservation_status": {"current": {"category": "accepted"}}, **extra}


def page(rows, current=1, last=1):
    return io.BytesIO(json.dumps({"data": rows, "meta": {"current_page": current, "last_page": last},
                                  "links": {"next": "https://untrusted.invalid/"}}).encode())


class GuestOperationsTests(unittest.TestCase):
    def test_keeps_local_dates_and_deduplicates_without_guest_fields(self):
        guest = stay("stay-1", "2026-10-31T23:30:00-05:00", "2026-11-01T11:00:00-06:00",
                     guests={"name": "not retained"}, notes="never displayed")
        prior = stay("stay-2", "2026-10-20", "2026-10-31")
        final = stay("stay-3", "2026-11-03", "2026-11-04")
        owner = stay("owner", "2026-11-01", "2026-11-02", stay_type="owner_stay")
        canceled = stay("canceled", "2026-11-01", "2026-11-02", status="cancelled")
        self.assertEqual(project_operations(TODAY, [guest, guest, prior, final, owner, canceled]), [
            {"date": "2026-10-31", "arrivals": 1, "departures": 1},
            {"date": "2026-11-01", "arrivals": 0, "departures": 1},
            {"date": "2026-11-02", "arrivals": 0, "departures": 0},
            {"date": "2026-11-03", "arrivals": 1, "departures": 0},
        ])

    def test_fixed_endpoint_filters_and_explicit_pagination(self):
        guests = stay("stay-1", "2026-10-31", "2026-11-03")
        with patch("flightwall.guest_operations.build_opener") as build:
            opener = build.return_value
            opener.open.side_effect = [page([{"id": LISTING}]), page([guests], last=2),
                                       page([], current=2, last=2), page([guests])]
            days = fetch_operations("fixture-token", TODAY)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual([urlsplit(r.full_url).path for r in requests],
                         ["/v2/properties", "/v2/reservations", "/v2/reservations", "/v2/reservations"])
        self.assertTrue(all(urlsplit(r.full_url).netloc == "public.api.hospitable.com" for r in requests))
        self.assertTrue(all(r.get_header("Authorization") == "Bearer fixture-token" for r in requests))
        queries = [parse_qs(urlsplit(r.full_url).query) for r in requests[1:]]
        self.assertEqual([q["date_query"] for q in queries], [["checkin"], ["checkin"], ["checkout"]])
        self.assertEqual([q["page"] for q in queries], [["1"], ["2"], ["1"]])
        for query in queries:
            self.assertNotIn("include", query)
            self.assertEqual(query["properties[]"], [LISTING])
            self.assertEqual(query["status[]"], ["accepted"])
            self.assertEqual(query["start_date"], ["2026-10-30"])
            self.assertEqual(query["end_date"], ["2026-11-04"])
        self.assertEqual(days[0]["arrivals"], 1)
        self.assertEqual(days[-1]["departures"], 1)

    def test_fails_closed_and_hides_provider_errors(self):
        with patch("flightwall.guest_operations.build_opener") as build:
            opener = build.return_value
            with self.assertRaises(GuestOperationsError):
                fetch_operations("fixture-token\nsecret", TODAY)
            opener.open.assert_not_called()
            opener.open.side_effect = OSError("Never show this response detail")
            with self.assertRaisesRegex(GuestOperationsError, "could not be refreshed"):
                fetch_operations("fixture-token", TODAY)
            opener.open.side_effect = [page([], last=5)]
            with self.assertRaises(GuestOperationsError):
                fetch_operations("fixture-token", TODAY)
        with self.assertRaises(GuestOperationsError):
            project_operations(TODAY, [stay("first", "unknown", "2026-11-02")])

    def test_conflicting_duplicate_does_not_display_a_false_summary(self):
        a = stay("stay-1", "2026-10-31", "2026-11-01")
        b = stay("stay-1", "2026-10-31", "2026-11-02")
        with self.assertRaises(GuestOperationsError):
            project_operations(TODAY, [a, b])
