"""Paid route diagnostics are bounded aggregate reasons, never aircraft history."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds, NEARBY_ROUTE_DIAGNOSTIC_CODES


NOW = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
CONFIG = DEFAULTS | {"demo": False, "flightaware_enabled": True, "nearby_enabled": True,
                     "nearby_enrichment_enabled": True, "flightaware_monthly_limit_usd": 5}


def aircraft():
    return {"hex": "abc123", "callsign": "DAL123", "registration": "N123AB", "distance_miles": 1,
            "latitude": 32.0, "longitude": -85.0, "observed_at": NOW.isoformat()}


def provider(**changes):
    return {"ident": "DAL123", "fa_flight_id": "DAL123-private-id", "registration": "N123AB",
            "origin": {"code_iata": "VPS"}, "destination": {"code_iata": "ATL"},
            "actual_off": (NOW - timedelta(hours=1)).isoformat(), "actual_in": None, "actual_on": None,
            "crew": "PRIVATE-CREW", "api_key": "PRIVATE-KEY", "route": "PRIVATE-FLIGHT-PLAN", **changes}


def response(items=None, **changes):
    return {"flights": [provider()] if items is None else items, "links": {"next": None}, "num_pages": 1, **changes}


def setup(tmp_path):
    (tmp_path / "flightaware.key").write_text("synthetic-private-key")
    feed = FlightFeeds(tmp_path)
    feed.nearby = [aircraft()]
    return feed


@pytest.mark.parametrize("payload,reason", [
    (response(), "matched"), (response([]), "empty_response"),
    (response([provider(ident="AAL123")]), "no_verified_match"),
    (response([provider(), provider(fa_flight_id="SECOND")]), "ambiguous"),
    (response(links={"next": "https://example.invalid/PRIVATE"}), "incomplete_response"),
    ({"flights": "PRIVATE-MALFORMED"}, "invalid_response"),
])
def test_only_permitted_provider_attempts_add_fixed_constant_outcomes(tmp_path, payload, reason):
    feed = setup(tmp_path)
    with (patch("flightwall.feeds.utcnow", return_value=NOW),
          patch("flightwall.feeds.time.monotonic", return_value=1000),
          patch("flightwall.feeds.request_json", return_value=payload) as network):
        feed._poll_nearby_routes(CONFIG, NOW, visible=True)
        assert network.call_count == 1
        counts = feed.nearby_route_diagnostics()
        assert counts["attempts"] == counts[reason] == 1
        assert sum(value for key, value in counts.items() if key != "attempts") == 1
        assert set(counts) == set(NEARBY_ROUTE_DIAGNOSTIC_CODES)
        assert all(type(value) is int and 0 <= value <= 1_000_000 for value in counts.values())
        text = json.dumps(counts)
        for private in ("PRIVATE", "abc123", "DAL123", "N123AB", "32.0", "-85.0"):
            assert private not in text
        # Reading or mutating a returned copy never triggers a new request.
        counts["attempts"] = 123
        assert feed.nearby_route_diagnostics()["attempts"] == 1
        assert network.call_count == 1


def test_budget_stop_is_not_a_provider_attempt_and_preserves_reserved_budget(tmp_path):
    feed = setup(tmp_path)
    config = CONFIG | {"flightaware_monthly_limit_usd": 0}
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch("flightwall.feeds.request_json") as network:
        feed._poll_nearby_routes(config, NOW, visible=True)
        network.assert_not_called()
    counts = feed.nearby_route_diagnostics()
    assert counts["budget_stop"] == 1 and counts["attempts"] == 0
    ledger = json.loads((tmp_path / "flightaware-usage.json").read_text())
    assert ledger["reserved_milliusd"] == 0 and ledger["nearby_reserved_milliusd"] == 0


def test_nearby_query_uses_provider_utc_example_and_one_result_set(tmp_path):
    feed = setup(tmp_path)
    fractional = NOW.replace(microsecond=123456)
    local = fractional.astimezone(timezone(timedelta(hours=-6)))
    with (patch("flightwall.feeds.utcnow", return_value=fractional),
          patch("flightwall.feeds.request_json", return_value=response()) as network):
        feed._poll_nearby_routes(CONFIG, local, visible=True)
    network.assert_called_once()
    url = urlsplit(network.call_args.args[0])
    assert url.path == "/aeroapi/flights/DAL123"
    assert parse_qs(url.query) == {"start": ["2026-11-16T18:00:00Z"], "end": ["2026-11-17T18:00:00Z"],
                                   "ident_type": ["designator"], "max_pages": ["1"]}
    assert feed.nearby_route_diagnostics()["matched"] == 1


@pytest.mark.parametrize(("error", "reason", "wait"), [
    (HTTPError("https://example.invalid/PRIVATE", 400, "PRIVATE", {}, None), "http_400", 900),
    (HTTPError("https://example.invalid/PRIVATE", 401, "PRIVATE", {}, None), "http_auth", 900),
    (HTTPError("https://example.invalid/PRIVATE", 403, "PRIVATE", {}, None), "http_auth", 900),
    (HTTPError("https://example.invalid/PRIVATE", 429, "PRIVATE", {"Retry-After": "3600"}, None),
     "http_rate_limit", 3600),
    (HTTPError("https://example.invalid/PRIVATE", 503, "PRIVATE", {}, None), "http_upstream", 900),
    (HTTPError("https://example.invalid/PRIVATE", 404, "PRIVATE", {}, None), "http_other", 300),
    (OSError("PRIVATE"), "transport_error", 900),
])
def test_provider_failure_reasons_and_cooldown_have_no_extra_spend(tmp_path, error, reason, wait):
    feed = setup(tmp_path)
    another = response([provider(ident="DAL124")])
    with (patch("flightwall.feeds.utcnow", return_value=NOW) as clock,
          patch("flightwall.feeds.time.monotonic", return_value=1000) as tick,
          patch("flightwall.feeds.request_json", side_effect=[error, another]) as network):
        feed._poll_nearby_routes(CONFIG, NOW, visible=True)
        usage = tmp_path / "flightaware-usage.json"
        before = usage.read_bytes()
        assert feed.nearby_route_diagnostics()[reason] == 1
        assert json.loads(before)["nearby_reserved_milliusd"] == 5

        for elapsed in (wait - 1, wait):
            tick.return_value = 1000 + elapsed
            clock.return_value = NOW + timedelta(seconds=elapsed)
            feed.nearby = [dict(aircraft(), hex="abc124", callsign="DAL124",
                                observed_at=clock.return_value.isoformat())]
            feed._poll_nearby_routes(CONFIG, clock.return_value, visible=True)
            if elapsed == wait - 1:
                network.assert_called_once()
                assert usage.read_bytes() == before
        assert network.call_count == 2
        assert json.loads(usage.read_text())["nearby_reserved_milliusd"] == 10
    counts = feed.nearby_route_diagnostics()
    assert counts["attempts"] == 2 and counts["matched"] == counts[reason] == 1
    assert "PRIVATE" not in json.dumps(counts)


def test_permitted_transport_error_counts_attempt_without_retaining_error_or_resetting_spend(tmp_path):
    feed = setup(tmp_path)
    with (patch("flightwall.feeds.utcnow", return_value=NOW),
          patch("flightwall.feeds.request_json", side_effect=OSError("PRIVATE-PROVIDER-URL-TOKEN"))):
        feed._poll_nearby_routes(CONFIG, NOW, visible=True)
    counts = feed.nearby_route_diagnostics()
    assert counts["attempts"] == counts["transport_error"] == 1
    assert "PRIVATE" not in json.dumps(counts)
    path = tmp_path / "flightaware-usage.json"
    before = path.read_bytes()
    ledger = json.loads(before)
    assert ledger["reserved_milliusd"] == ledger["nearby_reserved_milliusd"] == 5
    assert all(value == 0 for value in FlightFeeds(tmp_path).nearby_route_diagnostics().values())
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*diagnostic*"))


def test_pre_network_configuration_error_has_no_attempt_and_hidden_card_does_no_diagnostics(tmp_path):
    feed = setup(tmp_path)
    with patch.object(feed, "_flightaware", side_effect=ValueError("PRIVATE-KEY")) as network:
        feed._poll_nearby_routes(CONFIG, NOW, visible=False)
        assert all(value == 0 for value in feed.nearby_route_diagnostics().values())
        network.assert_not_called()
        feed._poll_nearby_routes(CONFIG, NOW, visible=True)
        assert network.call_count == 1
    counts = feed.nearby_route_diagnostics()
    assert counts["attempts"] == 0 and counts["configuration_error"] == 1
    assert "PRIVATE" not in json.dumps(counts)


def test_counters_saturate_and_unknown_reason_is_ignored(tmp_path):
    feed = setup(tmp_path)
    feed._nearby_route_diagnostic_counts["attempts"] = 1_000_000
    feed._record_nearby_route_diagnostic("attempts")
    feed._record_nearby_route_diagnostic("PRIVATE-NEW-FIELD")
    result = feed.nearby_route_diagnostics()
    assert result["attempts"] == 1_000_000
    assert set(result) == set(NEARBY_ROUTE_DIAGNOSTIC_CODES)
