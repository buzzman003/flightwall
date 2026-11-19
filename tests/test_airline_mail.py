"""Flight email access stays bounded to the opted-in mailbox, issuers and MIME parts."""
import base64
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest

from flightwall.airline_mail import AirlineMail, CATEGORY_QUERY, KNOWN_QUERY, STRUCTURE, TICKET_QUERY
from flightwall.delta_travel import MAX_TEXT
from test_airline_travel import CWT_INVOICE, NOW, PASSENGER, part, reservation, script


def meta(ident, domain="aa.com", subject="Flight confirmation"):
    return {"id": ident, "internalDate": str(int(NOW.timestamp() * 1000)), "labelIds": ["INBOX"],
            "payload": {"headers": [
                {"name": "From", "value": "Reservations <bookings@" + domain + ">"},
                {"name": "Subject", "value": subject},
                {"name": "Authentication-Results", "value": "mx.google.com; dmarc=pass header.from=" + domain},
            ]}}


class Mailbox:
    def __init__(self):
        self.calls = []
        self.auth = "simulated"
        self.after = lambda action: None
        self.pages = {(KNOWN_QUERY, None): {"messages": [{"id": "test1"}]}}
        self.meta = {"test1": meta("test1")}
        self.body = {"test1": part("text/html", script(reservation()))}
        self.structure = {"test1": {"mimeType": "text/html", "body": {"size": self.body["test1"]["body"]["size"]}}}
        self.ticket_attachment = Mock()

    def connected(self):
        return bool(self.auth)

    def _token(self):
        return {"refresh_token": self.auth, "client_id": "synthetic-id"}

    def api(self, path, params):
        if path == "/messages":
            action = "list"
            result = self.pages.get((params["q"], params.get("pageToken")), {})
        else:
            ident = path.split("/")[-1]
            if params["format"] == "metadata":
                action, result = "meta", self.meta[ident]
            elif params["fields"] == STRUCTURE:
                action, result = "structure", {"payload": self.structure[ident]}
            else:
                action, result = "body", {"payload": self.body[ident]}
        self.calls.append((action, path, params))
        self.after(action)
        return deepcopy(result)


def enabled(tmp_path):
    cfg = {"demo": False, "lisa_airline_mail_enabled": True, "delta_travel_lisa_names": json.dumps([PASSENGER])}
    feed = AirlineMail(tmp_path, lambda: cfg)
    feed.client = Mailbox()
    return feed, feed.client, cfg


def message(mail, ident, nodes, minutes=0, subject="Flight confirmation", domain="aa.com"):
    mail.meta[ident] = meta(ident, domain=domain, subject=subject)
    mail.meta[ident]["internalDate"] = str(int((NOW + timedelta(minutes=minutes)).timestamp() * 1000))
    mail.body[ident] = part("text/html", script(nodes))
    mail.structure[ident] = {"mimeType": "text/html", "body": {"size": mail.body[ident]["body"]["size"]}}


def test_legacy_confirmed_receipt_rebinds_before_exact_cancellation_and_prevents_paid_target(tmp_path):
    from flightwall.airline_travel import parse_airline
    from flightwall.personal_tracking import personal_tracking_targets
    feed, mail, _ = enabled(tmp_path)
    original = reservation()
    legacy = parse_airline(part("text/html", script(original)), [PASSENGER], NOW, b"x" * 32)["records"]
    feed.travel.import_records(legacy, NOW)  # 0.4.31/32 kept no booking association.
    message(mail, "cancel", original | {"reservationStatus": "ReservationCancelled"}, 2, "Flight cancelled")
    mail.pages[KNOWN_QUERY, None] = {"messages": [{"id": "cancel"}, {"id": "test1"}]}
    assert feed.sync(NOW)["status"] == "review_needed"
    assert feed.sync(NOW)["status"] == "ready"
    assert feed.travel.list_records()[0]["status"] == "cancelled"
    assert personal_tracking_targets(feed.travel.list_records()) == []
    assert "EXMPL7" not in feed.travel.airline_evidence.read_text()
    message(mail, "reissued", original, 3)
    mail.pages[KNOWN_QUERY, None]["messages"].insert(0, {"id": "reissued"})
    feed.sync(NOW)
    assert feed.travel.list_records()[0]["status"] == "confirmed"


def test_other_booking_issuer_or_passenger_cannot_cancel_verified_leg(tmp_path):
    feed, mail, _ = enabled(tmp_path)
    original = reservation()
    feed.sync(NOW)
    second = original | {"reservationNumber": "OTHER42"}
    message(mail, "second", second, 1)
    message(mail, "cancel", original | {"reservationStatus": "ReservationCancelled"}, 2, "Trip cancelled")
    mail.pages[KNOWN_QUERY, None] = {"messages": [{"id": "second"}, {"id": "cancel"}]}
    assert feed.sync(NOW)["status"] == "ready"
    assert feed.travel.list_records()[0]["status"] == "confirmed"
    cancel = second | {"reservationStatus": "ReservationCancelled"}
    message(mail, "bad", cancel, 3, "Trip cancelled", "united.com")
    message(mail, "other", cancel | {"underName": {"@type": "Person", "name": "Other Person"}}, 4, "Trip cancelled")
    mail.pages[KNOWN_QUERY, None] = {"messages": [{"id": "bad"}, {"id": "other"}]}
    assert feed.sync(NOW)["review_needed"] == 2
    assert feed.travel.list_records()[0]["status"] == "confirmed"
    message(mail, "final", cancel, 5, "Trip cancelled")
    mail.pages[KNOWN_QUERY, None] = {"messages": [{"id": "final"}]}
    feed.sync(NOW)
    assert feed.travel.list_records()[0]["status"] == "cancelled"


def test_partial_schedule_change_updates_only_explicitly_replaced_legs(tmp_path):
    feed, mail, _ = enabled(tmp_path)
    original, connection = reservation(), reservation("UA")
    message(mail, "test1", [original, connection])
    feed.sync(NOW)
    replacement = deepcopy(original)
    replacement["reservationFor"]["flightNumber"] = "22"
    message(mail, "change", [original | {"reservationStatus": "ReservationCancelled"}, replacement], 2,
            "Your flight reservation changed")
    mail.pages[KNOWN_QUERY, None] = {"messages": [{"id": "change"}]}
    assert feed.sync(NOW)["status"] == "ready"
    assert {r["number"]: r["status"] for r in feed.travel.list_records()} == {
        "AA1234": "cancelled", "AA22": "confirmed", "UA1234": "confirmed"}


def test_ambiguous_subject_and_disagreeing_departure_remain_for_owner_review(tmp_path):
    feed, mail, _ = enabled(tmp_path)
    original = reservation()
    feed.sync(NOW)
    message(mail, "ambiguous", original, 2, "Your reservation was cancelled")
    wrong = deepcopy(original)
    wrong["reservationStatus"] = "ReservationCancelled"
    wrong["reservationFor"]["departureTime"] = "2026-11-05T23:00:00-05:00"
    message(mail, "wrong", wrong, 2, "Your reservation was cancelled")
    mail.pages[KNOWN_QUERY, None] = {"messages": [{"id": "ambiguous"}, {"id": "wrong"}]}
    assert feed.sync(NOW)["review_needed"] == 2
    mail.pages[KNOWN_QUERY, None] = {}
    assert feed.sync(NOW)["status"] == "review_needed"
    assert feed.travel.list_records()[0]["status"] == "confirmed"


def test_structured_receipt_is_read_once_across_repeated_polls_and_overlapping_queries(tmp_path):
    feed, mail, _ = enabled(tmp_path)
    mail.pages[CATEGORY_QUERY, None] = mail.pages[KNOWN_QUERY, None]
    for _ in range(2):
        result = feed.sync(NOW)
        assert result["matched_legs"] == result["inspected"] == 1 and result["status"] == "ready"
    assert [c[0] for c in mail.calls].count("body") == 1
    mail.ticket_attachment.assert_not_called()
    assert [(r["number"], r["traveler"]) for r in feed.travel.list_records()] == [("AA1234", "lisa")]
    saved = feed.travel.storage.path.read_text()
    assert all(text not in saved for text in (PASSENGER, "EXMPL7", "test1"))


@pytest.mark.parametrize("change", ["failed", "wrong-server", "misaligned", "free-mail", "irrelevant", "sent"])
def test_rejected_metadata_never_fetches_body_or_attachment(tmp_path, change):
    feed, mail, _ = enabled(tmp_path)
    row = meta("test1", "gmail.com" if change == "free-mail" else "aa.com")
    headers = row["payload"]["headers"]
    if change == "failed":
        headers.insert(2, {"name": "Authentication-Results", "value": "mx.google.com; dmarc=fail header.from=aa.com"})
    if change == "wrong-server":
        headers[-1]["value"] = "unknown.example; dmarc=pass header.from=aa.com"
    if change == "misaligned":
        headers[-1]["value"] = "mx.google.com; dmarc=pass header.from=other.example"
    if change == "irrelevant":
        headers[1]["value"] = "Hotel reservation confirmation"
    if change == "sent":
        row["labelIds"] = ["SENT"]
    mail.meta["test1"] = row
    assert feed.sync(NOW)["matched_legs"] == 0
    assert all(c[0] in {"meta", "list"} for c in mail.calls)
    mail.ticket_attachment.assert_not_called()


def test_unlisted_authenticated_issuer_needs_positive_category_and_flight_subject(tmp_path):
    feed, mail, _ = enabled(tmp_path)
    mail.meta["test1"] = meta("test1", "regional.example")
    assert feed.sync(NOW)["matched_legs"] == 0
    assert all(c[0] in {"meta", "list"} for c in mail.calls)
    mail.pages[CATEGORY_QUERY, None] = mail.pages[KNOWN_QUERY, None]
    assert feed.sync(NOW)["matched_legs"] == 1


@pytest.mark.parametrize("change", ["oversize", "attached", "combined"])
def test_invalid_declared_text_size_or_attachment_never_fetches_bytes(tmp_path, change):
    feed, mail, _ = enabled(tmp_path)
    if change == "combined":
        mail.structure["test1"] = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": mime, "body": {"size": MAX_TEXT // 2 + 1}} for mime in ("text/plain", "text/html")]}
    else:
        mail.structure["test1"]["body"].update({"size": MAX_TEXT + 1} if change == "oversize" else {"attachmentId": "file"})
    assert feed.sync(NOW)["status"] == "review_needed"
    assert all(c[0] != "body" for c in mail.calls)
    mail.ticket_attachment.assert_not_called()


def pdf_ticket(mail):
    raw = b"%PDF-synthetic%%EOF"
    mail.meta["test1"] = meta("test1", "cwtsato.com", "Ticket/Invoice Issued")
    mail.pages[TICKET_QUERY, None] = mail.pages[KNOWN_QUERY, None]
    mail.structure["test1"] = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "body": {"size": 100}},
        {"mimeType": "application/pdf", "filename": "receipt.pdf", "body": {"size": len(raw), "attachmentId": "file1"}}]}
    mail.ticket_attachment.return_value = {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()}


def test_agency_pdf_requires_own_switch_and_receipt_is_prioritized_and_deduplicated(tmp_path, monkeypatch):
    feed, mail, cfg = enabled(tmp_path)
    pdf_ticket(mail)
    assert feed.sync(NOW)["matched_legs"] == 0
    assert all(c[0] in {"meta", "list"} for c in mail.calls)
    mail.ticket_attachment.assert_not_called()
    monkeypatch.setattr("flightwall.itinerary_pdf.extract_itinerary_text", lambda raw: CWT_INVOICE)
    cfg["lisa_ticket_pdfs_enabled"] = True
    mail.calls.clear()
    assert feed.sync(NOW)["matched_legs"] == 2
    assert mail.calls[0][2]["q"] == TICKET_QUERY
    assert {r["number"] for r in feed.travel.list_records()} == {"AA318", "AA259"}
    mail.ticket_attachment.assert_called_once_with("test1", "file1")
    assert all(c[0] != "body" for c in mail.calls)


def test_pdf_consent_withdrawn_during_transfer_discards_attachment_without_parsing(tmp_path, monkeypatch):
    feed, mail, cfg = enabled(tmp_path)
    pdf_ticket(mail)
    cfg["lisa_ticket_pdfs_enabled"] = True
    extract = Mock(side_effect=AssertionError("Revoked PDF must not be processed"))
    monkeypatch.setattr("flightwall.itinerary_pdf.extract_itinerary_text", extract)
    def revoke(*unused):
        cfg["lisa_ticket_pdfs_enabled"] = False
        return mail.ticket_attachment.return_value
    mail.ticket_attachment.side_effect = revoke
    assert feed.sync(NOW)["status"] == "waiting"
    extract.assert_not_called()
    assert not feed.travel.storage.path.exists()


def test_verified_agency_pdf_can_cancel_one_exact_ticket_segment(tmp_path, monkeypatch):
    feed, mail, cfg = enabled(tmp_path)
    cfg["lisa_ticket_pdfs_enabled"] = True
    pdf_ticket(mail)
    invoice = CWT_INVOICE
    monkeypatch.setattr("flightwall.itinerary_pdf.extract_itinerary_text", lambda raw: invoice)
    assert feed.sync(NOW)["matched_legs"] == 2
    invoice = invoice.replace("Status                Confirmed", "Status                Cancelled", 1)
    mail.meta["amend"] = meta("amend", "cwtsato.com", "Ticket/Invoice cancellation")
    mail.meta["amend"]["internalDate"] = str(int((NOW + timedelta(minutes=1)).timestamp() * 1000))
    mail.structure["amend"] = mail.structure["test1"]
    mail.pages[KNOWN_QUERY, None] = {"messages": [{"id": "amend"}]}
    assert feed.sync(NOW)["status"] == "ready"
    assert {r["number"]: r["status"] for r in feed.travel.list_records()} == {"AA318": "cancelled", "AA259": "confirmed"}


@pytest.mark.parametrize("change", ["old-only", "invalid-flag", "missing-aliases", "no-account"])
def test_no_consent_or_alias_or_account_never_searches(tmp_path, change):
    feed, mail, cfg = enabled(tmp_path)
    cfg["delta_travel_lisa_mail_enabled"] = True
    if change == "old-only":
        cfg.pop("lisa_airline_mail_enabled")
    if change == "invalid-flag":
        cfg["lisa_airline_mail_enabled"] = "true"
    if change == "missing-aliases":
        cfg["delta_travel_lisa_names"] = "[]"
    if change == "no-account":
        mail.auth = None
    assert feed.sync(NOW)["status"] in {"disabled", "not_connected", "review_needed"}
    assert not mail.calls


@pytest.mark.parametrize("stage", ["meta", "structure", "body"])
def test_revoking_between_requests_stops_further_reads_and_saves_nothing(tmp_path, stage):
    feed, mail, cfg = enabled(tmp_path)
    def revoke(action):
        if action == stage:
            cfg["lisa_airline_mail_enabled"] = False
    mail.after = revoke
    assert feed.sync(NOW)["status"] == "disabled"
    assert mail.calls[-1][0] == stage
    assert not feed.travel.storage.path.exists()


def test_next_page_is_separate_for_each_query_and_account_change_discards_cursor(tmp_path):
    feed, mail, _ = enabled(tmp_path)
    mail.pages[KNOWN_QUERY, None] = {"nextPageToken": "airline-page"}
    mail.pages[CATEGORY_QUERY, None] = {"nextPageToken": "reservation-page"}
    assert feed.sync(NOW)["status"] == "ready"
    mail.calls.clear()
    assert feed.sync(NOW)["status"] == "ready"
    assert [c[2]["pageToken"] for c in mail.calls] == ["airline-page", "reservation-page"]
    assert feed.sync(NOW)["status"] == "ready"
    mail.auth = "new-authorization"
    assert feed.status()["status"] == "waiting"
    mail.calls.clear()
    assert feed.sync(NOW)["status"] == "ready"
    assert all("pageToken" not in c[2] for c in mail.calls)
