"""Fictional Delta email fixtures; no household identities or real reservations."""
from datetime import datetime, timedelta, timezone
import base64
import copy
import json

import pytest

from flightwall.delta_travel import DeltaReview, DeltaTravel, ERROR, REVIEW, _inline_fields, owner_names, parse_listing
from flightwall.messages import MessageError
from flightwall.personal_travel import visible_travel


NOW = datetime(2026, 11, 5, 17, tzinfo=timezone.utc)
NAMES = ["ALEX MORGAN", "ALEX R MORGAN"]
SALT = b"s" * 32
ROWS = [["DL", "0456", "MEM", "ATL", "07Nov 05:05PM", "07Nov 08:05PM"],
        ["DL", "0457", "ATL", "JAX", "07Nov 09:30PM", "07Nov 10:45PM"]]


def text_body(rows=None, passenger="ALEX R MORGAN", locator="ZXCV12", employee="DIFFERENT EMPLOYEE"):
    people = [passenger] if isinstance(passenger, str) else passenger
    lines = ["Thank you for booking Non Revenue Listing.", "Employee Name - " + employee,
             "(PPR No. 123456789)", "See below for your flight and passenger information.",
             "Flight Information (Record Locator : " + locator + ")", "Carrier", "Flight", "From", "To",
             "Departure Time", "Arrival Time"]
    lines.extend(" ".join(row) for row in (rows if rows is not None else ROWS))
    passenger_lines = [part for index, name in enumerate(people, 1) for part in (str(index), name)]
    return "\n\n".join([*lines, "Passenger Information", "#", "Name", *passenger_lines,
                            "Additional Comments", "Please review the travel dress code."])


def html_body(rows=None, passenger="ALEX R MORGAN", locator="ZXCV12", employee="DIFFERENT EMPLOYEE"):
    people = [passenger] if isinstance(passenger, str) else passenger
    header = "<tr>" + "".join("<td>" + cell + "</td>" for cell in
                              ("Carrier", "Flight", "From", "To", "Departure Time", "Arrival Time"))
    # Delta's verified template has implicit row closure: each next tr closes
    # its predecessor. Test that shape instead of repairing it in the fixture.
    flights = "".join("<tr>" + "".join("<td>" + cell + "</td>" for cell in row)
                      for row in (rows if rows is not None else ROWS))
    passengers = "".join("<tr><td>" + str(index) + "</td><td><div>&nbsp;" + name + "</div></td></tr>"
                         for index, name in enumerate(people, 1))
    return ("<html><body><p>Thank you for booking Non Revenue Listing.</p>"
            "<p>Employee Name - " + employee + " (PPR No. 123456789)</p>"
            "<p>Flight Information (Record Locator : " + locator + ")</p><table>" + header + flights +
            "</table><p>Passenger Information</p><table><tr><td>#</td><td>Name</td></tr>"
            + passengers + "</table>"
            "<p>Additional Comments</p><p>Please review the travel dress code.</p></body></html>")


def dash_body(rows=None, passenger="ALEX R MORGAN", locator="ZXCV12", employee="DIFFERENT EMPLOYEE"):
    people = [passenger] if isinstance(passenger, str) else passenger
    lines = ["Thank you for booking Non Revenue Listing.", "Employee Name - " + employee + "(PPR No. 123456789)",
             "Flight Information(Record Locator " + locator + ") - "]
    for row in (ROWS if rows is None else rows):
        lines.append(row[0] + " - Flight " + row[1] + " : " + row[2] + " - " + row[3] + "\r\n"
                     "Departure Time : " + row[4] + " / Arrival Time : " + row[5])
    lines += ["Passenger Information", *[str(index) + " - " + name for index, name in enumerate(people, 1)],
              "Additional Comments", "Please review the travel dress code."]
    return "\r\n\r\n".join(lines)


def part(mime, text):
    encoded = text.encode()
    return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(encoded).decode().rstrip("="), "size": len(encoded)}}


def payload(rows=None, **changes):
    return {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [part("text/plain", text_body(rows, **changes)),
                                                            part("text/html", html_body(rows, **changes))]},
        {"mimeType": "application/pdf", "filename": "never-read.pdf", "body": {"size": 1000000, "attachmentId": "not-fetched"}},
    ]}


def metadata(identifier="fictional1", received=NOW, sender="Delta <no-reply@delta.com>", subject="Non Revenue Listing", **changes):
    return {"id": identifier, "internalDate": str(int(received.timestamp() * 1000)), "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]}} | changes


class Client:
    def __init__(self, messages=None):
        self.messages = messages or [(metadata(), payload())]
        self.calls, self.authorized = [], True

    def connected(self):
        return self.authorized

    def _token(self):
        return {"client_id": "fictional-client", "refresh_token": "fictional-refresh"}

    def api(self, path, parameters):
        self.calls.append((path, dict(parameters)))
        if path == "/messages":
            return {"messages": [{"id": row[0]["id"]} for row in self.messages]}
        row = next(row for row in self.messages if path == "/messages/" + row[0]["id"])
        return row[0] if parameters["format"] == "metadata" else {"payload": row[1]}


def enabled(tmp_path, messages=None):
    config = {"demo": False, "delta_travel_enabled": True, "delta_travel_owner_names": json.dumps(NAMES)}
    feed = DeltaTravel(tmp_path, lambda: config)
    feed.client = Client(messages)
    return feed, config


@pytest.mark.parametrize("mime", ["plain", "html", "both"])
def test_verified_template_supports_implied_html_closures_leading_zeroes_and_station_clocks(mime):
    value = payload() if mime == "both" else part("text/" + mime, text_body() if mime == "plain" else html_body())
    result = parse_listing(value, NAMES, NOW, SALT)
    assert len(result["booking_key"]) == 64
    first, second = result["records"]
    assert first == {"number": "DL456", "origin": "MEM", "destination": "ATL", "status": "listed",
                     "start": "2026-11-07T23:05:00+00:00", "end": "2026-11-08T01:05:00+00:00",
                     "source": "delta_email", "updated_at": NOW.isoformat(), "traveler": "chris"}
    assert second["number"] == "DL457"
    assert second["end"] == "2026-11-08T03:45:00+00:00"
    assert parse_listing(value, NAMES, NOW, b"t" * 32)["booking_key"] != result["booking_key"]


def test_owner_must_be_an_actual_passenger_employee_name_alone_is_not_enough():
    with pytest.raises(DeltaReview):
        parse_listing(payload(passenger="SOMEONE ELSE", employee="ALEX R MORGAN"), NAMES, NOW, SALT)


def test_actual_dash_plain_format_matches_the_html_alternative():
    value = payload()
    value["parts"][0]["parts"][0] = part("text/plain", dash_body())
    parsed = parse_listing(value, NAMES, NOW, SALT)
    assert parsed == parse_listing(part("text/html", html_body()), NAMES, NOW, SALT)


@pytest.mark.parametrize("passengers,travelers", [
    (["CASEY TAYLOR"], {"lisa"}),
    (["ALEX R MORGAN", "CASEY TAYLOR"], {"chris", "lisa"}),
])
def test_requested_partner_and_shared_listings_match_actual_passengers(passengers, travelers):
    value = payload(passenger=passengers)
    value["parts"][0]["parts"][0] = part("text/plain", dash_body(passenger=passengers))
    result = parse_listing(value, NAMES, NOW, SALT, lisa_names=["CASEY TAYLOR"])
    assert {row["traveler"] for row in result["records"]} == travelers
    assert len(result["records"]) == 2 * len(travelers)
    assert all(row["status"] == "listed" for row in result["records"])


def test_overlapping_partner_aliases_are_rejected():
    with pytest.raises(DeltaReview):
        parse_listing(payload(), NAMES, NOW, SALT, lisa_names=["ALEX MORGAN"])


@pytest.mark.parametrize("change", ["clock", "passenger", "locator"])
def test_inconsistent_mime_alternatives_are_rejected(change):
    value = payload()
    text = html_body(rows=[ROWS[0], [*ROWS[1][:4], "07Nov 09:45PM", ROWS[1][-1]]]) if change == "clock" else html_body(**{change: "SAMPLE"})
    value["parts"][0]["parts"][1] = part("text/html", text)
    with pytest.raises(DeltaReview):
        parse_listing(value, NAMES, NOW, SALT)


@pytest.mark.parametrize("row", [
    ["AA", "456", "MEM", "ATL", "07Nov 05:05PM", "07Nov 08:05PM"],
    ["DL", "0000", "MEM", "ATL", "07Nov 05:05PM", "07Nov 08:05PM"],
    ["DL", "0456", "MEM", "ZZZ", "07Nov 05:05PM", "07Nov 08:05PM"],
    ["DL", "0456", "MEM", "ATL", "07Nov 13:05PM", "07Nov 08:05PM"],
    ["DL", "0456", "MEM", "ATL", "07Nov 05:05PM", "09Nov 08:05PM"],
])
def test_one_unsupported_or_invalid_leg_rejects_the_whole_listing(row):
    with pytest.raises(DeltaReview):
        parse_listing(payload(rows=[ROWS[0], row]), NAMES, NOW, SALT)


@pytest.mark.parametrize("received,clock", [
    (datetime(2026, 10, 20, tzinfo=timezone.utc), "01Nov 01:15AM"),
    (datetime(2026, 3, 1, tzinfo=timezone.utc), "08Mar 02:15AM"),
])
def test_ambiguous_and_nonexistent_station_local_dst_clocks_are_not_guessed(received, clock):
    row = ["DL", "0456", "MEM", "ATL", clock, clock.replace("01:15", "04:15").replace("02:15", "05:15")]
    with pytest.raises(DeltaReview):
        parse_listing(payload(rows=[row]), NAMES, received, SALT)


def test_year_rollover_is_anchored_to_message_receipt_not_the_current_clock():
    received = datetime(2026, 12, 30, tzinfo=timezone.utc)
    rows = [["DL", "0456", "MEM", "ATL", "02Jan 05:05PM", "02Jan 08:05PM"]]
    result = parse_listing(payload(rows=rows), NAMES, received, SALT)
    assert result["records"][0]["start"].startswith("2027-01-02")
    with pytest.raises(DeltaReview):
        parse_listing(payload(rows=rows), NAMES, NOW, SALT[:-1])


@pytest.mark.parametrize("names", ["not-json", "[]", '[42]', '["ALEX 123"]', None])
def test_owner_aliases_need_explicit_valid_names(names):
    with pytest.raises(DeltaReview):
        owner_names(names)


def test_opt_in_and_own_gmail_consent_are_independent_of_mail_previews(tmp_path):
    feed, config = enabled(tmp_path)
    real = DeltaTravel(tmp_path, lambda: config)
    assert real.client.account_dir == tmp_path / "accounts" / "chris"
    config["delta_travel_enabled"] = False
    assert feed.sync(NOW)["status"] == "disabled"
    assert feed.client.calls == [] and not feed.path.exists()
    config["delta_travel_enabled"] = True
    feed.client.authorized = False
    assert feed.sync(NOW)["status"] == "not_connected"
    assert feed.client.calls == [] and not feed.travel.storage.path.exists()
    feed.client.authorized = True
    # No messages_enabled or chris_gmail_enabled setting is required.
    assert feed.sync(NOW)["status"] == "ready"
    assert len(feed.travel.list_records()) == 2
    assert not (tmp_path / "accounts" / "lisa").exists()


def test_success_keeps_only_narrow_records_private_hmac_and_aggregate_status(tmp_path):
    feed, config = enabled(tmp_path)
    result = feed.sync(NOW)
    assert result == {"enabled": True, "status": "ready", "last_sync": NOW.isoformat(), "error": None,
                      "inspected": 1, "imported": 2, "review_needed": 0}
    assert feed.status() == result
    assert visible_travel(feed.travel.list_records(), config, NOW)
    assert visible_travel(feed.travel.list_records(), config | {"guest_mode": True}, NOW) == []
    contents = "\n".join(file.read_text() for file in tmp_path.rglob("*") if file.is_file() and file.suffix != ".lock")
    for sensitive in ("ALEX", "MORGAN", "123456789", "ZXCV12", "dress code", "fictional1", "Employee"):
        assert sensitive not in contents
    assert all("attachments" not in path for path, _ in feed.client.calls)
    assert "body" not in feed.client.calls[1][1]["fields"]
    assert feed.client.calls[2][1]["fields"].startswith("payload(")
    assert "body" not in feed.client.calls[2][1]["fields"]
    assert feed.client.calls[3][1]["fields"] == "payload(mimeType,filename,parts(mimeType,filename,parts(mimeType,filename,body(data,size))))"
    assert 'subject:"Non Revenue Listing"' in feed.client.calls[0][1]["q"]
    before, touched = feed.travel.storage.path.read_bytes(), feed.travel.storage.path.stat().st_mtime_ns
    feed.sync(NOW)
    assert feed.travel.storage.path.read_bytes() == before
    assert feed.travel.storage.path.stat().st_mtime_ns == touched


def test_sync_can_import_partner_only_from_chris_mailbox_without_lisa_oauth(tmp_path):
    value = payload(passenger="CASEY TAYLOR")
    value["parts"][0]["parts"][0] = part("text/plain", dash_body(passenger="CASEY TAYLOR"))
    feed, config = enabled(tmp_path, [(metadata(), value)])
    config["delta_travel_lisa_names"] = '["CASEY TAYLOR"]'
    assert feed.sync(NOW)["status"] == "ready"
    assert {row["traveler"] for row in feed.travel.list_records()} == {"lisa"}
    assert not (tmp_path / "accounts" / "lisa").exists()


@pytest.mark.parametrize("sender,subject,expected", [
    ("no-reply@delta.com.attacker.test", "Non Revenue Listing", "ready"),
    ("no-reply@delta.com", "Fwd: Non Revenue Listing", "review_needed"),
    ("no-reply@delta.com", "Reservation cancellation", "review_needed"),
    ("no-reply@delta.com", "Unrelated airline news", "ready"),
])
def test_headers_are_checked_before_any_inline_body_fetch(tmp_path, sender, subject, expected):
    feed, _ = enabled(tmp_path, [(metadata(sender=sender, subject=subject), payload())])
    assert feed.sync(NOW)["status"] == expected
    assert not any(parameters.get("format") == "full" for _, parameters in feed.client.calls)
    assert feed.travel.list_records() == []


def test_supported_latest_listing_revises_the_same_booking_without_stale_replays(tmp_path):
    feed, _ = enabled(tmp_path)
    feed.sync(NOW)
    newer = NOW + timedelta(hours=1)
    replacement = [["DL", "0458", "MEM", "ATL", "08Nov 05:05PM", "08Nov 08:05PM"]]
    feed.client.messages = [(metadata(identifier="fictional2", received=newer), payload(rows=replacement))]
    assert feed.sync(newer)["status"] == "ready"
    assert {row["number"]: row["status"] for row in feed.travel.list_records()} == {"DL456": "cancelled", "DL457": "cancelled", "DL458": "listed"}
    before = feed.travel.storage.path.read_bytes()
    feed.client.messages = [(metadata(), payload())]
    feed.sync(newer)
    assert feed.travel.storage.path.read_bytes() == before


def test_unsupported_cancellation_never_guesses_or_deletes_saved_flights(tmp_path):
    feed, _ = enabled(tmp_path)
    feed.sync(NOW)
    before = feed.travel.storage.path.read_bytes()
    bad = payload()
    bad["parts"][0]["parts"][0] = part("text/plain", text_body() + "\nListing cancelled")
    feed.client.messages = [(metadata(received=NOW + timedelta(minutes=1)), bad)]
    result = feed.sync(NOW + timedelta(minutes=2))
    assert result["status"] == "review_needed" and result["error"] == REVIEW
    assert feed.travel.storage.path.read_bytes() == before


def test_bounded_pagination_never_applies_a_truncated_mail_scan(tmp_path):
    feed, _ = enabled(tmp_path)
    original = feed.client.api
    def api(path, parameters):
        if path == "/messages":
            return {"messages": [], "nextPageToken": "more-results"}
        return original(path, parameters)
    feed.client.api = api
    result = feed.sync(NOW)
    assert result["status"] == "review_needed" and result["imported"] == 0
    assert not feed.travel.storage.path.exists()


def test_lost_consent_during_fetch_discards_pending_body_derived_records(tmp_path):
    feed, config = enabled(tmp_path)
    original = feed.client.api
    def api(path, parameters):
        value = original(path, parameters)
        if parameters.get("format") == "full":
            config["delta_travel_enabled"] = False
        return value
    feed.client.api = api
    assert feed.sync(NOW)["status"] == "disabled"
    assert not feed.travel.storage.path.exists()


@pytest.mark.parametrize("change", ["owner", "account"])
def test_owner_or_account_switch_during_fetch_prevents_import(tmp_path, change):
    feed, config = enabled(tmp_path)
    original = feed.client.api
    def api(path, parameters):
        value = original(path, parameters)
        if parameters.get("format") == "full":
            if change == "owner":
                config["delta_travel_owner_names"] = '["ANOTHER OWNER"]'
            else:
                feed.client._token = lambda: {"client_id": "fictional-client", "refresh_token": "different-account"}
        return value
    feed.client.api = api
    feed.sync(NOW)
    assert not feed.travel.storage.path.exists()


def test_network_errors_are_fixed_and_never_store_exception_payloads(tmp_path):
    feed, _ = enabled(tmp_path)
    def api(path, parameters):
        raise MessageError("secret message body and locator ZXCV12")
    feed.client.api = api
    result = feed.sync(NOW)
    assert result["status"] == "error" and result["error"] == ERROR
    assert "ZXCV12" not in feed.path.read_text()


@pytest.mark.parametrize("change", ["size", "duplicate", "filename"])
def test_payload_bounds_duplicate_alternatives_and_attached_text_are_not_imported(change):
    value = payload()
    if change == "size":
        value["parts"][0]["parts"][0]["body"]["size"] = 9000000
    elif change == "duplicate":
        value["parts"][0]["parts"].append(copy.deepcopy(value["parts"][0]["parts"][0]))
    else:
        for body in value["parts"][0]["parts"]:
            body["filename"] = "attached-itinerary.txt"
    with pytest.raises(DeltaReview):
        parse_listing(value, NAMES, NOW, SALT)


def test_unknown_mime_layout_cannot_request_attachment_sibling_body_data(tmp_path):
    value = payload()
    value["parts"] = [value["parts"][0]["parts"][0], value["parts"][1]]
    with pytest.raises(DeltaReview):
        _inline_fields(value)
    feed, _ = enabled(tmp_path, [(metadata(), value)])
    assert feed.sync(NOW)["status"] == "review_needed"
    assert not any("body" in parameters.get("fields", "") for _, parameters in feed.client.calls)
