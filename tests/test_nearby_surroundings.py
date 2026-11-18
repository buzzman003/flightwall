"""Nearby fallback, source freshness and unkeyed provider allowance."""
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
import json
import math
from unittest.mock import patch
import urllib.error

import pytest

from flightwall.config import DEFAULTS, private_json
from flightwall.feeds import FlightFeeds, NEARBY_POLL_SECONDS, _nearby_retry_seconds


NOW = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)
CONFIG = DEFAULTS | {"demo": False, "flightaware_enabled": False, "nearby_enabled": True,
                     "home_latitude": 0.0, "home_longitude": 0.0, "nearby_radius_miles": 5}


def aircraft(ident="abc123", miles=1, **changes):
    return {"hex": ident, "lat": math.degrees(miles * 1609.344 / 6_371_008.8), "lon": 0,
            "alt_baro": 12000, "seen_pos": 0, "r": "N123AB", "t": "B350", **changes}


def snapshot(rows=(), now=NOW):
    return {"now": now.timestamp() * 1000, "ac": list(rows)}


def poll(feed, response, now=NOW, tick=0, config=CONFIG):
    with (patch("flightwall.feeds.utcnow", return_value=now),
          patch("flightwall.feeds.time.monotonic", return_value=tick),
          patch("flightwall.feeds.request_json", return_value=response) as network):
        feed.poll(config, None)
    return network


def test_outer_nearest_ten_exact_statute_ring_deduplication_and_safe_fields(tmp_path):
    feed = FlightFeeds(tmp_path)
    rows = [aircraft(f"{index:06x}", miles) for index, miles in enumerate(
        [30.0001, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 5.001], start=1)]
    rows.append(aircraft("00000d", 6, seen_pos=40))  # Older duplicate loses.
    network = poll(feed, snapshot(rows))
    assert network.call_args.args[0].endswith("/0.0/0.0/27")
    assert feed.nearby_for_config(CONFIG, NOW) == []
    outer = feed.nearby_outer_for_config(CONFIG, NOW)
    assert len(outer) == 10
    assert [round(row["distance_miles"], 3) for row in outer] == [5.001, 20, 21, 22, 23, 24, 25, 26, 27, 28]
    assert outer[0]["registration"] == "N123AB" and outer[0]["aircraft_type"] == "B350"
    assert all("route" not in row and "origin" not in row and "destination" not in row for row in outer)
    outer[0]["registration"] = "CHANGED"
    assert feed.nearby_outer_for_config(CONFIG, NOW)[0]["registration"] == "N123AB"
    assert feed.nearby_outer_for_config(CONFIG, NOW + timedelta(seconds=120, microseconds=1)) == []


@pytest.mark.parametrize("radius", [2.5, 5, 10, 25, 50, 100])
def test_configured_inner_radius_is_unchanged_and_outer_is_not_a_second_map(tmp_path, radius):
    feed = FlightFeeds(tmp_path)
    config = CONFIG | {"nearby_radius_miles": radius}
    poll(feed, snapshot([aircraft(miles=radius)]), config=config)
    assert len(feed.nearby_for_config(config, NOW)) == 1
    assert feed.nearby_outer_for_config(config, NOW) == []
    assert feed.nearby_outer_for_config(config | {"nearby_radius_miles": radius + 1}, NOW) == []


def test_outer_boundary_thirty_included_and_five_excluded(tmp_path):
    feed = FlightFeeds(tmp_path)
    poll(feed, snapshot([aircraft("abc001", 30), aircraft("abc002", 30.00001)]))
    assert [row["hex"] for row in feed.nearby_outer_for_config(CONFIG, NOW)] == ["abc001"]
    poll(feed, snapshot([aircraft("abc003", 5), aircraft("abc001", 30)], NOW + timedelta(seconds=180)),
         NOW + timedelta(seconds=180), 180)
    assert [row["hex"] for row in feed.nearby_for_config(CONFIG, NOW + timedelta(seconds=180))] == ["abc003"]
    assert feed.nearby_outer_for_config(CONFIG, NOW + timedelta(seconds=180)) == []


def test_expired_inner_position_never_turns_snapshot_into_observed_empty_ring(tmp_path):
    feed = FlightFeeds(tmp_path)
    poll(feed, snapshot([aircraft(seen_pos=119), aircraft("abc222", 20)]))
    later = NOW + timedelta(seconds=2)
    assert feed.nearby_for_config(CONFIG, later) == []
    assert feed.nearby_status_for_config(CONFIG, later) == "stale"
    assert feed.nearby_outer_for_config(CONFIG, later) == []


def test_empty_snapshot_freshness_uses_source_clock_not_successful_receipt(tmp_path):
    feed = FlightFeeds(tmp_path)
    poll(feed, snapshot([aircraft(miles=20, seen_pos=0)], NOW - timedelta(seconds=100)))
    assert feed.nearby_status_for_config(CONFIG, NOW) == "ready"
    assert len(feed.nearby_outer_for_config(CONFIG, NOW)) == 1
    later = NOW + timedelta(seconds=21)
    assert feed.nearby_status_for_config(CONFIG, later) == "stale"
    assert feed.nearby_outer_for_config(CONFIG, later) == []
    poll(feed, snapshot([], NOW - timedelta(seconds=100)), NOW + timedelta(seconds=180), 180)
    assert feed.nearby_status_for_config(CONFIG, NOW + timedelta(seconds=180)) == "stale"


def test_error_and_invalid_config_never_publish_outer_or_claim_empty_success(tmp_path):
    feed = FlightFeeds(tmp_path)
    poll(feed, snapshot([aircraft(miles=20)]))
    for changes in ({"demo": True}, {"nearby_enabled": False}, {"home_latitude": 1},
                    {"nearby_radius_miles": 10}, {"home_latitude": None}):
        assert feed.nearby_outer_for_config(CONFIG | changes, NOW) == []
    with (patch("flightwall.feeds.utcnow", return_value=NOW + timedelta(seconds=180)),
          patch("flightwall.feeds.time.monotonic", return_value=180),
          patch("flightwall.feeds.request_json", side_effect=OSError("offline"))):
        feed.poll(CONFIG, None)
    assert feed.nearby_for_config(CONFIG, NOW + timedelta(seconds=180)) == []
    assert feed.nearby_outer_for_config(CONFIG, NOW + timedelta(seconds=180)) == []
    assert feed.nearby_status_for_config(CONFIG, NOW + timedelta(seconds=180)) == "stale"


def test_unkeyed_allowance_persists_cadence_across_restart_and_pin_change(tmp_path):
    feed = FlightFeeds(tmp_path)
    assert poll(feed, snapshot()).call_count == 1
    usage = json.loads((tmp_path / "adsblol-usage.json").read_text())
    assert set(usage) == {"schema_version", "requested_at", "retry_until"}
    assert len(usage["requested_at"]) == 1
    assert "latitude" not in json.dumps(usage) and "abc123" not in json.dumps(usage)
    restarted = FlightFeeds(tmp_path)
    assert poll(restarted, snapshot([], NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1), 1).call_count == 0
    assert poll(restarted, snapshot([], NOW + timedelta(seconds=179)), NOW + timedelta(seconds=179), 179,
                CONFIG | {"home_latitude": 1}).call_count == 0
    assert poll(restarted, snapshot([], NOW + timedelta(seconds=180)), NOW + timedelta(seconds=180), 180,
                CONFIG | {"home_latitude": 1}).call_count == 1
    assert NEARBY_POLL_SECONDS == 180


def test_rolling_allowance_cannot_be_reset_at_midnight_and_corruption_fails_closed(tmp_path):
    feed = FlightFeeds(tmp_path)
    stamps = [(NOW - timedelta(seconds=(480 - index) * 179)).isoformat() for index in range(480)]
    private_json(feed._nearby_usage_path, {"schema_version": 1, "requested_at": stamps, "retry_until": None})
    assert feed._reserve_nearby_request(NOW) == 480
    assert poll(feed, snapshot()).call_count == 0
    feed._nearby_usage_path.write_text("broken", encoding="utf-8")
    assert poll(FlightFeeds(tmp_path), snapshot()).call_count == 0
    assert feed._nearby_usage_path.read_text() == "broken"


@pytest.mark.parametrize("header,expected", [
    ("3600", 3600), (format_datetime(NOW + timedelta(seconds=700), usegmt=True), 700),
    ("bad", 0), ("-5", 0), ("9999999999", 7 * 86400),
])
def test_retry_after_seconds_and_http_date_are_bounded(header, expected):
    headers = Message()
    headers["Retry-After"] = header
    error = urllib.error.HTTPError("https://example.invalid", 429, "quota", headers, None)
    assert _nearby_retry_seconds(error, NOW) == expected


def test_server_retry_after_survives_restart_and_recovers(tmp_path):
    headers = Message()
    headers["Retry-After"] = "3600"
    error = urllib.error.HTTPError("https://example.invalid", 429, "quota", headers, None)
    feed = FlightFeeds(tmp_path)
    with (patch("flightwall.feeds.utcnow", return_value=NOW),
          patch("flightwall.feeds.time.monotonic", return_value=0),
          patch("flightwall.feeds.request_json", side_effect=error) as network):
        feed.poll(CONFIG, None)
        assert network.call_count == 1
    restarted = FlightFeeds(tmp_path)
    assert poll(restarted, snapshot([], NOW + timedelta(seconds=3599)), NOW + timedelta(seconds=3599), 10).call_count == 0
    assert poll(restarted, snapshot([], NOW + timedelta(seconds=3600)), NOW + timedelta(seconds=3600), 11).call_count == 1
    assert restarted.nearby_status_for_config(CONFIG, NOW + timedelta(seconds=3600)) == "ready"


def test_failure_backoff_grows_and_overlapping_requests_are_skipped(tmp_path):
    feed = FlightFeeds(tmp_path)
    for attempt, tick in enumerate((0, 180, 540), start=1):
        with (patch("flightwall.feeds.utcnow", return_value=NOW + timedelta(seconds=tick)),
              patch("flightwall.feeds.time.monotonic", return_value=tick),
              patch("flightwall.feeds.request_json", side_effect=OSError("offline")) as network):
            feed.poll(CONFIG, None)
            assert network.call_count == 1
            assert feed._nearby_retry_at == tick + 180 * 2 ** (attempt - 1)
    feed._nearby_lock.acquire()
    try:
        assert poll(feed, snapshot([], NOW + timedelta(seconds=2000)), NOW + timedelta(seconds=2000), 2000).call_count == 0
    finally:
        feed._nearby_lock.release()
