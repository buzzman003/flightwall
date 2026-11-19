"""Synthetic airline confirmations. No real booking identifiers or travelers."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
import json

import pytest

from flightwall.airline_travel import parse_airline, parse_cwt_invoice
from flightwall.delta_travel import DeltaReview


NOW = datetime(2026, 11, 1, 19, tzinfo=timezone.utc)
SALT = b"x" * 32
PASSENGER = "Robin Jordan"


def reservation(carrier="AA"):
    return {
        "@context": "https://schema.org", "@type": "FlightReservation",
        "reservationNumber": "EXMPL7",
        "reservationStatus": "http://schema.org/ReservationConfirmed",
        "underName": {"@type": "Person", "name": PASSENGER},
        "reservationFor": {
            "@type": "Flight", "flightNumber": "1234",
            "airline": {"@type": "Airline", "iataCode": carrier},
            "departureAirport": {"@type": "Airport", "iataCode": "ATL"},
            "departureTime": "2026-11-05T22:56:00-05:00",
            "arrivalAirport": {"@type": "Airport", "iataCode": "STL"},
            "arrivalTime": "2026-11-05T23:58:00-06:00",
        },
    }


def part(mime, text):
    raw = text.encode("utf-8")
    return {"mimeType": mime, "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()}}


def script(value):
    return '<script type="application/ld+json">' + json.dumps(value) + "</script>"


def microdata(value, prop=None):
    prop_attr = f' itemprop="{prop}"' if prop else ""
    if isinstance(value, dict):
        children = "".join(microdata(v, k) for k, v in value.items() if not k.startswith("@"))
        return f'<div itemscope itemtype="http://schema.org/{value["@type"]}"{prop_attr}>{children}</div>'
    return f'<meta{prop_attr} content="{escape(value)}"/>'


def parse(html):
    return parse_airline(part("text/html", html), [PASSENGER], NOW, SALT)


@pytest.mark.parametrize("markup", [script, microdata])
@pytest.mark.parametrize("carrier", ["AA", "UA", "G4", "9X"])
def test_confirmed_legs_keep_only_exact_owner_and_narrow_public_flight_fields(markup, carrier):
    node = reservation(carrier)
    node["ticketNumber"] = "PRIVATE-TICKET"
    node["reservationFor"]["departureGate"] = "PRIVATE-GATE"
    node["underName"]["email"] = "private@example.test"
    parsed = parse(markup(node))
    assert len(parsed["booking_key"]) == 64
    assert parsed["records"] == [{
        "number": carrier + "1234", "origin": "ATL", "destination": "STL",
        "start": "2026-11-06T03:56:00+00:00", "end": "2026-11-06T05:58:00+00:00",
        "status": "confirmed", "source": "american_email" if carrier == "AA" else "airline_email",
        "updated_at": NOW.isoformat(), "traveler": "lisa",
    }]
    for private in (PASSENGER, "EXMPL7", "PRIVATE", "private@example"):
        assert private not in json.dumps(parsed)


def test_shared_forwarded_itinerary_does_not_import_other_passenger_segments():
    own, other = reservation(), reservation("UA")
    other["underName"]["name"] = "Other Person"
    other["reservationNumber"] = "OTHER9"
    own["underName"] = {"@type": "Person", "givenName": "Robin", "familyName": "Jordan"}
    del own["@context"]
    assert [r["number"] for r in parse(script({"@context": "https://schema.org",
                                             "@graph": [other, own]}))["records"]] == ["AA1234"]


def test_airline_local_times_and_utc_supported_across_midnight_and_different_zones():
    node = reservation()
    node["reservationFor"].update(flightNumber="AA 0024", departureTime="2026-11-05T22:56",
                                  arrivalTime="2026-11-06T05:58:00Z")
    row = parse(script(node))["records"][0]
    assert row["number"] == "AA24" and row["start"] == "2026-11-06T03:56:00+00:00"
    assert row["end"] == "2026-11-06T05:58:00+00:00"


@pytest.mark.parametrize("mutate", [
    lambda x: x["underName"].update(name=PASSENGER + " Other"),
    lambda x: x.update(reservationStatus="ReservationPending"),
    lambda x: x.update(reservationStatus="ReservationCancelled"),
    lambda x: x.update(reservationNumber=""),
    lambda x: x["reservationFor"].update(flightNumber="UA 1234"),
    lambda x: x["reservationFor"]["airline"].update(iataCode="99"),
    lambda x: x["reservationFor"]["arrivalAirport"].update(iataCode="ZZZ"),
    lambda x: x["reservationFor"].update(departureTime="2026-11-01T01:15:00"),
    lambda x: x["reservationFor"].update(arrivalTime="2026-11-07T22:56:00Z"),
    lambda x: x["reservationFor"].update(arrivalTime="2026-11-05T22:56:00Z"),
    lambda x: x.update(underName={"@type": "Person", "givenName": "Robin"}),
    lambda x: x["underName"].update(givenName="Other", familyName="Traveler"),
    lambda x: x.update(reservationFor={"@id": "https://example.test/flight"}),
    lambda x: x.update({"@context": "https://example.test/wrong"}),
    lambda x: x.update({"@type": "EventReservation"}),
])
def test_ambiguous_or_unverified_data_is_rejected(mutate):
    node = reservation()
    mutate(node)
    with pytest.raises(DeltaReview):
        parse(script(node))


def test_unstructured_mime_alternative_can_accompany_verified_machine_readable_html():
    payload = {"mimeType": "multipart/alternative", "parts": [
        part("text/plain", "Your trip confirmation. Please see the ticket."),
        part("text/html", script(reservation()) + microdata(reservation())),
    ]}
    assert len(parse_airline(payload, [PASSENGER], NOW, SALT)["records"]) == 1
    with pytest.raises(DeltaReview):
        parse_airline(part("text/plain", "AA1234 to STL on Tuesday"), [PASSENGER], NOW, SALT)


def test_multiple_schemas_must_agree_and_contradictory_duplicate_legs_need_review():
    original = reservation()
    conflict = deepcopy(original)
    conflict["reservationFor"]["arrivalTime"] = "2026-11-06T00:58:00-06:00"
    with pytest.raises(DeltaReview):
        parse(script(original) + microdata(conflict))
    with pytest.raises(DeltaReview):
        parse(script([original, conflict]))


def test_nested_scopes_do_not_leak_an_unrelated_person_name_or_airport():
    node = reservation()
    del node["underName"]["name"]
    node["underName"]["seller"] = {"@type": "Organization", "name": PASSENGER}
    with pytest.raises(DeltaReview):
        parse(microdata(node))


CWT_INVOICE = """Agency    Locator: XBOOK9       Date: Nov 01, 2026

Traveler                    ROBIN JORDAN

Thursday, November 05, 2026                  Confirmation: EXMPL1
     Flight AMERICAN AIRLINES 318
     DEPARTURE                          ARRIVAL
     FT WALTON BEACH,FL                 CHARLOTTE, NC
     4:30 PM, Nov 5, 2026               7:14 PM, Nov 5, 2026

     Status                Confirmed

Thursday, November 05, 2026                  Confirmation: EXMPL1
     Flight AMERICAN AIRLINES 259
     DEPARTURE                          ARRIVAL
     CHARLOTTE, NC                      ST LOUIS/LAMB,MO
     8:52 PM, Nov 5, 2026               9:45 PM, Nov 5, 2026

     Status                Confirmed

     Invoice / Ticket / Date                    Payment
     1234567890111                              Private account
"""


def cwt(text):
    return parse_cwt_invoice(text, [PASSENGER], NOW, SALT)


def test_owner_matched_agency_ticket_extracts_only_confirmed_airline_legs():
    result = cwt(CWT_INVOICE.replace("\n", "\r\n"))
    assert [(r["number"], r["origin"], r["destination"], r["start"], r["end"]) for r in result["records"]] == [
        ("AA318", "VPS", "CLT", "2026-11-05T22:30:00+00:00", "2026-11-06T00:14:00+00:00"),
        ("AA259", "CLT", "STL", "2026-11-06T01:52:00+00:00", "2026-11-06T03:45:00+00:00"),
    ]
    serialized = json.dumps(result).upper()
    for private in ("ROBIN", "JORDAN", "XBOOK9", "EXMPL1", "1234567890111", "PRIVATE ACCOUNT"):
        assert private not in serialized


@pytest.mark.parametrize("replace,with_text", [
    ("ROBIN JORDAN", "ROBIN JORDANSON"), ("Traveler", "Billed to"),
    ("Traveler                    ROBIN JORDAN", "Traveler                    ROBIN JORDAN\nTraveler     Another Person"),
    ("ST LOUIS/LAMB,MO", "ST LOUIS,MO"), ("Status                Confirmed", "Status      Waitlisted"),
    ("AMERICAN AIRLINES", "UNKNOWN AIRLINE"), ("Invoice / Ticket / Date", "Unissued Itinerary"),
    ("Locator: XBOOK9", "Locator: XBOOK"), ("9:45 PM, Nov 5, 2026", "9:45 PM"),
    ("DEPARTURE                          ARRIVAL", "ARRIVAL      DEPARTURE"),
    ("9:45 PM, Nov 5, 2026", "9:45 PM, Nov 3, 2026"),
])
def test_unmatched_traveler_missing_ticket_ambiguous_stations_and_bad_times_reject(replace, with_text):
    with pytest.raises(DeltaReview):
        cwt(CWT_INVOICE.replace(replace, with_text))


def test_agency_ticket_accepts_explicit_airline_codes_and_parenthesized_station_codes():
    receipt = CWT_INVOICE.replace("AMERICAN AIRLINES", "G4").replace("CHARLOTTE, NC", "CHARLOTTE (CLT)")
    assert all(r["source"] == "airline_email" and r["number"].startswith("G4") for r in cwt(receipt)["records"])
