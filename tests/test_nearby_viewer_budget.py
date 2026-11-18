"""Paid nearby route lookups require a current display-view activity signal."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds


NOW = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
CONFIG = DEFAULTS | {"demo": False, "flightaware_enabled": True, "nearby_enabled": True,
                     "nearby_enrichment_enabled": True, "home_latitude": 0.0,
                     "home_longitude": 0.0, "nearby_radius_miles": 5}


def snapshot(now, ident="abc123", callsign="DAL123"):
    return {"now": now.timestamp() * 1000, "ac": [{"hex": ident, "lat": 0.01, "lon": 0,
        "flight": callsign, "seen_pos": 0, "alt_baro": 12000}]}


@pytest.mark.parametrize("visible", [False, None, 1, "nearby"])
def test_direct_metadata_lookup_is_fail_closed_without_explicit_boolean_visibility(tmp_path, visible):
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    feed = FlightFeeds(tmp_path)
    feed.nearby = [{"hex": "abc123", "callsign": "DAL123", "distance_miles": 1,
                    "observed_at": NOW.isoformat()}]
    with patch.object(feed, "_flightaware") as paid:
        feed._poll_nearby_routes(CONFIG, NOW)
        feed._poll_nearby_routes(CONFIG, NOW, visible=visible)
        paid.assert_not_called()
    assert feed._nearby_route_cache == {}


def test_hidden_card_keeps_free_positions_independent_but_does_not_spend_nearby_budget(tmp_path):
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    feed = FlightFeeds(tmp_path)
    with (patch("flightwall.feeds.utcnow", return_value=NOW) as clock,
          patch("flightwall.feeds.time.monotonic", return_value=1000) as tick,
          patch("flightwall.feeds.request_json", return_value=snapshot(NOW)) as free,
          patch.object(feed, "_flightaware", return_value={"flights": []}) as paid):
        # The background service has no display heartbeat by default.
        feed.poll(CONFIG, None)
        assert free.call_count == 1 and paid.call_count == 0
        assert len(feed.nearby_for_config(CONFIG, NOW)) == 1
        # Showing or pausing on the current card can buy one bounded lookup.
        clock.return_value, tick.return_value = NOW + timedelta(seconds=2), 1002
        feed.poll(CONFIG, None, nearby_visible=True)
        assert paid.call_count == 1 and paid.call_args.kwargs == {"purpose": "nearby"}
        # Leaving the card stops even a new-aircraft lookup when its clock is due.
        clock.return_value, tick.return_value = NOW + timedelta(seconds=302), 1302
        free.return_value = snapshot(clock.return_value, "abc124", "DAL124")
        feed.poll(CONFIG, None, nearby_visible=False)
        assert free.call_count == 2 and paid.call_count == 1
        assert feed.nearby_for_config(CONFIG, clock.return_value)[0]["callsign"] == "DAL124"
        clock.return_value, tick.return_value = NOW + timedelta(seconds=304), 1304
        feed.poll(CONFIG, None, nearby_visible=True)
        assert paid.call_count == 2
        # Heartbeats never bypass the existing five-minute paid cooldown.
        clock.return_value, tick.return_value = NOW + timedelta(seconds=305), 1305
        feed.poll(CONFIG, None, nearby_visible=True)
        assert paid.call_count == 2 and free.call_count == 2


def test_operating_priority_still_suppresses_paid_nearby_even_with_visible_card(tmp_path):
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    feed = FlightFeeds(tmp_path)
    order = []
    with (patch("flightwall.feeds.utcnow", return_value=NOW),
          patch("flightwall.feeds.request_json", return_value=snapshot(NOW)) as free,
          patch.object(feed, "_poll_live", side_effect=lambda *a: order.append("operating")),
          patch.object(feed, "_poll_watches", side_effect=lambda *a: order.append("watch")),
          patch.object(feed, "_flightaware") as paid):
        feed.poll(CONFIG, None, on_duty=True, nearby_visible=True)
        assert order == ["operating", "watch"] and free.call_count == 1
        paid.assert_not_called()
