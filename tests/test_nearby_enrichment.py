"""Optional nearby metadata is conservative, sparse and never buys telemetry."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds
from flightwall.flight_alerts import fresh_nearby_route, match_nearby_route, nearby_commercial_ident

NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)


def aircraft(**changes):
    return {"hex": "abc123", "callsign": "DAL123", "registration": "N123AB", "distance_miles": 1,
            "observed_at": NOW.isoformat(), "latitude": 35.01, "longitude": -90.01,
            "altitude": 12000, "groundspeed": 230, "track": 180, "source": "ADSB.lol", **changes}


def provider(**changes):
    return {"ident": "DAL123", "fa_flight_id": "DAL123-synthetic", "registration": "N123AB",
            "origin": {"code_iata": "ATL"}, "destination": {"code_iata": "MEM"},
            "actual_off": (NOW - timedelta(hours=1)).isoformat(), "actual_on": None, "actual_in": None,
            "crew": "PRIVATE-CREW", "route": "PRIVATE-FILED-ROUTE", "api_key": "PRIVATE-KEY", **changes}


def data(*rows, **changes):
    return {"flights": list(rows or (provider(),)), "links": {"next": None}, "num_pages": 1, **changes}


def setup(tmp_path, **changes):
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    return FlightFeeds(tmp_path), DEFAULTS | {
        "demo": False, "flightaware_enabled": True, "nearby_enabled": True,
        "nearby_enrichment_enabled": True, "nearby_enrichment_monthly_limit_usd": 1,
        "home_latitude": 35.0, "home_longitude": -90.0, "nearby_radius_miles": 5, **changes}


def test_unique_airborne_callsign_is_projected_without_private_or_duplicate_telemetry():
    original = data()
    before = deepcopy(original)
    route = match_nearby_route(original, aircraft(), NOW)
    assert original == before
    assert route["origin"] == "ATL" and route["destination"] == "MEM"
    assert route["source"] == "FlightAware AeroAPI" and route["provider_flight_id"] == "DAL123-synthetic"
    assert set(route) == {"origin", "destination", "source", "fetched_at", "provider_flight_id", "callsign",
                          "registration", "aircraft_hex", "actual_off"}
    assert "PRIVATE" not in json.dumps(route)
    assert not {"latitude", "longitude", "groundspeed", "altitude", "track", "route"}.intersection(route)


@pytest.mark.parametrize("callsign", [None, "N123AB", "abc123", "DL123", "DAL 123", "DAL123/../", "search"])
def test_tail_hex_arbitrary_path_and_noncanonical_callsign_never_become_paid_lookups(callsign):
    assert nearby_commercial_ident(aircraft(callsign=callsign)) is None
    assert match_nearby_route(data(), aircraft(callsign=callsign), NOW) is None


@pytest.mark.parametrize("changes", [
    {"ident": "AAL123"}, {"ident": {"unexpected": "DAL123"}}, {"registration": "N999ZZ"},
    {"actual_off": None}, {"actual_off": (NOW + timedelta(seconds=1)).isoformat()},
    {"actual_off": (NOW - timedelta(hours=24, seconds=1)).isoformat()},
    {"actual_off": "2026-10-31T17:00:00"}, {"actual_on": NOW.isoformat()}, {"actual_in": NOW.isoformat()},
    {"blocked": True}, {"cancelled": True}, {"diverted": True}, {"position_only": True},
    {"origin": {"code_iata": "MEM"}}, {"destination": {"code_iata": "KJFK"}},
    {"fa_flight_id": "../../private"},
])
def test_mismatched_old_landed_terminated_and_unsupported_metadata_stay_unknown(changes):
    assert match_nearby_route(data(provider(**changes)), aircraft(), NOW) is None


@pytest.mark.parametrize("changes", [{"num_pages": 2}, {"num_pages": True}, {"links": {"next": "more"}},
                                      {"links": []}])
def test_incomplete_or_contradictory_pagination_cannot_choose_a_route(changes):
    assert match_nearby_route(data(**changes), aircraft(), NOW) is None


def test_multiple_current_candidates_are_ambiguous_even_if_one_timestamp_is_closer():
    second = provider(fa_flight_id="DAL123-other", actual_off=(NOW - timedelta(minutes=30)).isoformat())
    assert match_nearby_route(data(provider(), second), aircraft(), NOW) is None
    assert match_nearby_route(data(), aircraft(observed_at=(NOW - timedelta(seconds=121)).isoformat()), NOW) is None
    assert match_nearby_route(data(), aircraft(hex="invalid"), NOW) is None
    # A position from before takeoff cannot acquire that departure's metadata.
    assert match_nearby_route(data(provider(actual_off=NOW.isoformat())),
                              aircraft(observed_at=(NOW - timedelta(seconds=1)).isoformat()), NOW) is None


def test_metadata_and_position_ages_expire_separately_without_mutating_cached_values():
    route = match_nearby_route(data(), aircraft(), NOW)
    row = aircraft(route=route)
    original = deepcopy(row)
    assert fresh_nearby_route(row, NOW + timedelta(seconds=120)) is not None
    assert fresh_nearby_route(row, NOW + timedelta(seconds=120, microseconds=1)) is None
    fresh_position = aircraft(observed_at=(NOW + timedelta(seconds=600)).isoformat(), route=route)
    assert fresh_nearby_route(fresh_position, NOW + timedelta(seconds=600)) is not None
    assert fresh_nearby_route(fresh_position, NOW + timedelta(seconds=600, microseconds=1)) is None
    assert fresh_nearby_route(row, NOW - timedelta(microseconds=1)) is None
    assert row == original
    projected = fresh_nearby_route(row, NOW)
    projected["destination"] = "LAX"
    assert route["destination"] == "MEM"


@pytest.mark.parametrize("changes", [{"callsign": "DAL124"}, {"hex": "abc124"}, {"registration": "N124AB"}])
def test_cached_route_is_hidden_when_current_aircraft_association_changes(changes):
    route = match_nearby_route(data(), aircraft(), NOW)
    assert fresh_nearby_route(aircraft(route=route, **changes), NOW) is None


def test_malformed_cached_provider_identity_or_source_is_not_displayable():
    route = match_nearby_route(data(), aircraft(), NOW)
    for changes in ({"provider_flight_id": "../../private"}, {"source": "unknown"}, {"destination": "ATL"}):
        assert fresh_nearby_route(aircraft(route=route | changes), NOW) is None


def test_sparse_closest_three_lookups_have_distinct_success_and_miss_backoff(tmp_path):
    feed, config = setup(tmp_path)
    near = aircraft()
    farther = aircraft(hex="abc124", callsign="AAL124", distance_miles=2)
    feed.nearby = [aircraft(hex="abc126", callsign="UAL126", distance_miles=4), farther, near,
                   aircraft(hex="abc125", callsign="ASA125", distance_miles=3)]
    with patch("flightwall.feeds.utcnow", return_value=NOW) as now, \
            patch("flightwall.feeds.time.monotonic", return_value=1000) as tick, \
            patch.object(feed, "_flightaware", return_value=data()) as network:
        feed._poll_nearby_routes(config, NOW)
        assert network.call_count == 1 and network.call_args.args[0].startswith("/flights/DAL123?")
        assert network.call_args.kwargs == {"purpose": "nearby"}
        query = parse_qs(urlsplit(network.call_args.args[0]).query)
        assert query["max_pages"] == ["1"] and query["ident_type"] == ["designator"]
        tick.return_value = 1299
        feed._poll_nearby_routes(config, NOW)
        assert network.call_count == 1
        tick.return_value = 1300
        network.return_value = data(flights=[])
        feed._poll_nearby_routes(config, NOW)
        assert network.call_count == 2 and network.call_args.args[0].startswith("/flights/AAL124?")
        # At ten minutes a successful closest lookup refreshes; the failed
        # second aircraft remains backed off for thirty minutes.
        tick.return_value, now.return_value = 1600, NOW + timedelta(seconds=600)
        feed.nearby = [dict(row, observed_at=now.return_value.isoformat()) for row in feed.nearby]
        network.return_value = data()
        feed._poll_nearby_routes(config, now.return_value)
        assert network.call_count == 3 and network.call_args.args[0].startswith("/flights/DAL123?")
        tick.return_value = 1900
        feed._poll_nearby_routes(config, now.return_value)
        assert network.call_count == 4 and network.call_args.args[0].startswith("/flights/ASA125?")
        # The fourth aircraft remains outside the bounded candidate set.
        assert not any("UAL126" in call.args[0] for call in network.call_args_list)
    assert not (tmp_path / "flightaware-usage.json").exists()  # Stubbed network only.
    assert not list(tmp_path.glob("*route*"))


def test_failed_lookup_cannot_spam_paid_retries_or_overwrite_operating_error(tmp_path):
    feed, config = setup(tmp_path)
    feed.nearby = [aircraft()]
    feed.error = "Owner tracking unavailable"
    with patch("flightwall.feeds.utcnow", return_value=NOW), \
            patch("flightwall.feeds.time.monotonic", return_value=1000) as tick, \
            patch.object(feed, "_flightaware", side_effect=OSError("PRIVATE-PROVIDER-RESPONSE")) as network:
        feed._poll_nearby_routes(config, NOW)
        tick.return_value = 2799
        feed._poll_nearby_routes(config, NOW)
        assert network.call_count == 1
        tick.return_value = 2800
        feed._poll_nearby_routes(config, NOW)
        assert network.call_count == 2
    assert feed.error == "Owner tracking unavailable"
    assert "PRIVATE" not in repr(feed._nearby_route_cache)


def test_disabled_missing_key_and_stale_position_do_not_make_a_metadata_request(tmp_path):
    feed, config = setup(tmp_path)
    feed.nearby = [aircraft()]
    with patch.object(feed, "_flightaware") as network:
        for disabled in (config | {"nearby_enrichment_enabled": False}, config | {"flightaware_enabled": False},
                         config | {"demo": True}):
            feed._poll_nearby_routes(disabled, NOW)
        feed.nearby = [aircraft(observed_at=(NOW - timedelta(seconds=121)).isoformat())]
        feed._poll_nearby_routes(config, NOW)
        (tmp_path / "flightaware.key").unlink()
        feed.nearby = [aircraft()]
        feed._poll_nearby_routes(config, NOW)
        network.assert_not_called()


def test_current_pin_and_opt_in_gate_metadata_without_erasing_adsb_telemetry(tmp_path):
    feed, config = setup(tmp_path)
    row = aircraft()
    route = match_nearby_route(data(), row, NOW)
    feed._nearby_completed = ((35.0, -90.0, 5), [row])
    feed._nearby_route_cache[feed._nearby_route_identity(row)] = {"checked_at": 1, "route": route}
    result = feed.nearby_for_config(config, NOW)
    assert result[0]["route"]["destination"] == "MEM"
    assert result[0]["groundspeed"] == 230 and result[0]["source"] == "ADSB.lol"
    assert "route" not in feed._nearby_completed[1][0]
    assert "route" not in feed.nearby_for_config(config | {"nearby_enrichment_enabled": False}, NOW)[0]
    assert feed.nearby_for_config(config | {"home_latitude": 36.0}, NOW) == []
    result[0]["route"]["destination"] = "LAX"
    assert feed.nearby_for_config(config, NOW)[0]["route"]["destination"] == "MEM"


def test_aircraft_metadata_cache_is_bounded_and_old_or_future_entries_are_discarded(tmp_path):
    feed, config = setup(tmp_path)
    feed.nearby = [aircraft()]
    feed._nearby_route_cache = {(str(i), "DAL123", None): {"checked_at": 1000, "route": None}
                              for i in range(128)}
    feed._nearby_route_cache[("expired", "DAL123", None)] = {"checked_at": -1000, "route": None}
    feed._nearby_route_cache[("future", "DAL123", None)] = {"checked_at": 1001, "route": None}
    with patch("flightwall.feeds.utcnow", return_value=NOW), \
            patch("flightwall.feeds.time.monotonic", return_value=1000), \
            patch.object(feed, "_flightaware", return_value=data()):
        feed._poll_nearby_routes(config, NOW)
    assert len(feed._nearby_route_cache) == 128
    assert all(key[0] not in {"expired", "future"} for key in feed._nearby_route_cache)
    assert feed._nearby_route_identity(aircraft()) in feed._nearby_route_cache


def test_operating_then_watch_then_nearby_order_is_preserved_even_when_on_duty(tmp_path):
    feed, config = setup(tmp_path)
    order = []
    with patch("flightwall.feeds.utcnow", return_value=NOW), \
            patch.object(feed, "_poll_live", side_effect=lambda *args: order.append("operating")), \
            patch.object(feed, "_poll_watches", side_effect=lambda *args: order.append("watch")), \
            patch("flightwall.feeds.request_json", return_value={"now": NOW.timestamp() * 1000, "ac": []}) as adsb, \
            patch.object(feed, "_poll_nearby_routes", side_effect=lambda *args: order.append("nearby")):
        feed.poll(config, None)
        assert order == ["operating", "watch", "nearby"]
        order.clear()
        feed.poll(config, None, on_duty=True)
        assert order == ["operating", "watch"] and adsb.call_count == 1


def test_watch_contradictory_pagination_is_ambiguous_without_fetching_more_pages():
    watch = {"date": "2026-10-31", "timezone": "UTC", "ident": "DAL123"}
    assert FlightFeeds._watch_flight(data(num_pages=2), watch) == ("ambiguous", None)
