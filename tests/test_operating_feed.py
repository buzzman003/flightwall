"""Operating status polling is synthetic, associated and independently aged."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds

NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)


def chosen(**changes):
    return {"id": "operating-calendar-id", "number": "DL123", "origin": "ATL", "destination": "MEM",
            "start": "2026-10-31T17:00:00Z", "end": "2026-10-31T19:00:00Z", "deadhead": False, **changes}


def response(**changes):
    return {"flights": [{"fa_flight_id": "DAL123-provider-id", "ident": "DAL123", "ident_iata": "DL123",
                         "origin": {"code_iata": "ATL"}, "destination": {"code_iata": "MEM"},
                         "scheduled_out": "2026-10-31T17:00:00Z", "scheduled_in": "2026-10-31T19:00:00Z",
                         "actual_out": "2026-10-31T17:05:00Z", "estimated_in": "2026-10-31T19:10:00Z", **changes}]}


def position(at=NOW, **changes):
    return {"fa_flight_id": "DAL123-provider-id", "last_position": {
        "timestamp": at.isoformat(), "latitude": 34.0, "longitude": -87.0,
        "altitude": 330, "groundspeed": 420, "heading": 280, **changes}}


def setup(tmp_path):
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    return FlightFeeds(tmp_path), DEFAULTS | {"demo": False, "flightaware_enabled": True, "airline": "DAL"}


def test_status_repolls_while_positions_keep_their_own_observation_timestamp(tmp_path):
    feed, config = setup(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW) as now, patch("flightwall.feeds.time.monotonic", return_value=1000) as tick:
        with patch.object(feed, "_flightaware", side_effect=[response(), position()]) as network:
            feed.poll(config, chosen(), on_duty=True)
            assert network.call_count == 2
        assert feed.operating_status["arrival"]["delta_minutes"] == 10
        initial_observed = feed.live["observed_at"]
        now.return_value, tick.return_value = NOW + timedelta(seconds=120), 1120
        with patch.object(feed, "_flightaware", return_value=response(estimated_in="2026-10-31T19:35:00Z")) as network:
            feed.poll(config, chosen(), on_duty=True)
            assert network.call_count == 1 and "/position" not in network.call_args.args[0]
        assert feed.operating_status["arrival"]["delta_minutes"] == 35
        assert feed.live["observed_at"] == initial_observed
        fetched = feed.operating_status["fetched_at"]
        now.return_value, tick.return_value = NOW + timedelta(seconds=240), 1240
        with patch.object(feed, "_flightaware", return_value=position(now.return_value)) as network:
            feed.poll(config, chosen(), on_duty=True)
            assert network.call_count == 1 and network.call_args.args[0] == "/flights/DAL123-provider-id/position"
        assert feed.operating_status["fetched_at"] == fetched
        assert feed.live["flight_id"] == chosen()["id"] and feed.live["provider_flight_id"] == "DAL123-provider-id"


def test_old_position_does_not_erase_fresh_status_or_claim_a_live_marker(tmp_path):
    feed, config = setup(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch.object(feed, "_flightaware", side_effect=[response(), position(NOW - timedelta(seconds=301))]):
        feed.poll(config, chosen(), on_duty=True)
    assert feed.live is None
    assert feed.operating_status_for_config(config, chosen(), NOW)["status"] == "fresh"


def test_wrong_response_flight_identity_never_attaches_its_position(tmp_path):
    feed, config = setup(tmp_path)
    bad = position() | {"fa_flight_id": "different-provider-id"}
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch.object(feed, "_flightaware", side_effect=[response(), bad]):
        feed.poll(config, chosen(), on_duty=True)
    assert feed.live is None
    assert feed.operating_status["provider_flight_id"] == "DAL123-provider-id"


def test_actual_gate_arrival_stops_paid_polling_without_redating_cached_status(tmp_path):
    feed, config = setup(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW) as now, patch("flightwall.feeds.time.monotonic", return_value=1000) as tick:
        with patch.object(feed, "_flightaware", return_value=response(actual_on="2026-10-31T17:45:00Z", actual_in="2026-10-31T17:55:00Z")) as network:
            feed.poll(config, chosen(), on_duty=True)
            assert network.call_count == 1
        assert feed.operating_status["phase"] == "at_gate" and feed.live is None
        fetched = feed.operating_status["fetched_at"]
        now.return_value, tick.return_value = NOW + timedelta(seconds=600), 1600
        with patch.object(feed, "_flightaware") as network:
            feed.poll(config, chosen(), on_duty=True)
            network.assert_not_called()
        assert feed.operating_status["fetched_at"] == fetched
        assert feed.operating_status_for_config(config, chosen(), now.return_value)["status"] == "stale"


def test_readiness_window_and_config_identity_checks_are_immediate_and_immutable(tmp_path):
    feed, config = setup(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch.object(feed, "_flightaware", side_effect=[response(), position()]):
        feed.poll(config, chosen(), on_duty=True)
    status = feed.operating_status_for_config(config, chosen(), NOW)
    status["times"].clear()
    assert feed.operating_status["times"]
    assert feed.operating_status_for_config(config, chosen(origin="LAX"), NOW)["status"] == "pending"
    assert feed.operating_status_for_config(config | {"flightaware_enabled": False}, chosen(), NOW)["status"] == "off"
    assert feed.operating_status_for_config(config | {"demo": True}, chosen(), NOW)["status"] == "demo"
    assert feed.operating_status_for_config(config, chosen(start="2026-11-10T17:00:00Z", end="2026-11-10T19:00:00Z"), NOW)["status"] == "outside_window"
    (tmp_path / "flightaware.key").unlink()
    assert feed.operating_status_for_config(config, chosen(), NOW)["status"] == "unconfigured"
    with patch.object(feed, "_flightaware") as network:
        feed.poll(config, chosen(), on_duty=True)
        network.assert_not_called()


def test_provider_failure_marks_prior_status_stale_without_leaking_error_contents(tmp_path):
    feed, config = setup(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch("flightwall.feeds.time.monotonic", return_value=1000) as tick:
        with patch.object(feed, "_flightaware", side_effect=[response(), position()]):
            feed.poll(config, chosen(), on_duty=True)
        tick.return_value = 1120
        with patch.object(feed, "_flightaware", side_effect=OSError("PRIVATE-PROVIDER-CONTENT")):
            feed.poll(config, chosen(), on_duty=True)
    assert feed.operating_status["status"] == "stale"
    assert "PRIVATE" not in json.dumps(feed.operating_status) + feed.error


def test_predeparture_status_window_does_not_buy_positions_three_hours_early(tmp_path):
    feed, config = setup(tmp_path)
    before = NOW - timedelta(hours=4)
    with patch("flightwall.feeds.utcnow", return_value=before), patch.object(feed, "_flightaware", return_value=response(actual_out=None)) as network:
        feed.poll(config, chosen(), on_duty=True)
    assert network.call_count == 1 and "/position" not in network.call_args.args[0]
    assert feed.operating_status["status"] == "fresh" and feed.live is None
