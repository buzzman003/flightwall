"""Issued-ticket fixtures for airline designators and ambiguous agency fields."""
import pytest

from flightwall.delta_travel import DeltaReview
from test_airline_travel import CWT_INVOICE, cwt


@pytest.mark.parametrize(("ticket_carrier", "designator"), [
    ("Alaska Airlines", "AS"), ("Allegiant Air", "G4"), ("Avelo Airlines", "XP"),
    ("Breeze Airways", "MX"), ("Frontier Airlines", "F9"), ("Hawaiian Airlines", "HA"),
    ("JetBlue Airways", "B6"), ("Southwest   Airlines", "WN"), ("Spirit Airlines", "NK"),
    ("Sun Country Airlines", "SY"), ("FRONTIER AIRLINES (F9)", "F9"),
    ("BA", "BA"), ("9X", "9X"), ("G4", "G4"),
])
def test_agency_uses_printed_code_or_exact_marketing_name(ticket_carrier, designator):
    legs = cwt(CWT_INVOICE.replace("AMERICAN AIRLINES", ticket_carrier))["records"]
    assert [row["number"] for row in legs] == [designator + "318", designator + "259"]
    assert [(row["origin"], row["destination"]) for row in legs] == [("VPS", "CLT"), ("CLT", "STL")]


@pytest.mark.parametrize("ticket_carrier", [
    "Pacific", "99", "ALASKA AIRLINES (AA)", "OPERATED BY ALASKA AIRLINES",
    "AA / AMERICAN EAGLE", "Some Airline (XY)",
])
def test_unverified_or_conflicting_airline_names_still_need_review(ticket_carrier):
    with pytest.raises(DeltaReview):
        cwt(CWT_INVOICE.replace("AMERICAN AIRLINES", ticket_carrier))


def test_recognized_airline_does_not_resolve_an_ambiguous_station():
    with pytest.raises(DeltaReview):
        cwt(CWT_INVOICE.replace("AMERICAN AIRLINES", "Breeze Airways")
            .replace("FT WALTON BEACH,FL", "NEW YORK, NY"))
