"""Local conservative fee reservations; these tests never call a paid API."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds, PaidLimitError, PRICING_CHECKED, PRICING_MAX_AGE_DAYS

NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)
LOOKUP = "/flights/DAL123?max_pages=1"
POSITION = "/flights/provider-id/position"


def config(**changes):
    return DEFAULTS | {"flightaware_monthly_limit_usd": 5, **changes}


def read(tmp_path):
    return json.loads((tmp_path / "flightaware-usage.json").read_text())


def test_each_endpoint_reserves_full_nominal_fee_and_cannot_cross_monthly_cap(tmp_path):
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        feed._allow_paid_request(config(flightaware_monthly_limit_usd=.014), LOOKUP)
        with pytest.raises(PaidLimitError) as error:
            feed._allow_paid_request(config(flightaware_monthly_limit_usd=.014), POSITION)
    assert error.value.reason == "monthly_limit"
    assert read(tmp_path)["reserved_milliusd"] == 5 and read(tmp_path)["count"] == 1


def test_unknown_unbounded_or_unpriced_endpoints_never_reach_network(tmp_path):
    feed = FlightFeeds(tmp_path)
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    paths = ["/flights/search", "/history/flights/DAL123", "/flights/DAL123?max_pages=2",
             "/flights/../position?start=unknown", "/flights/%2e%2e", "/flights/foo%2fbar",
             "/flights/DAL123?max_pages=1&max_pages=2", "https://example.invalid/flights/DAL123",
             "/flights/DAL123?cursor=page", "/flights/provider/position?max_pages=1"]
    with patch("flightwall.feeds.request_json") as network:
        for path in paths:
            with pytest.raises(ValueError):
                feed._flightaware(path, config())
        network.assert_not_called()
    assert not (tmp_path / "flightaware-usage.json").exists()


def test_daily_rollover_preserves_month_spend_and_month_rollover_resets_both(tmp_path):
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW) as now:
        feed._allow_paid_request(config(), POSITION)
        now.return_value = NOW + timedelta(days=1)  # UTC November rollover.
        feed._allow_paid_request(config(), LOOKUP)
        assert read(tmp_path)["reserved_milliusd"] == 5 and read(tmp_path)["count"] == 1
        now.return_value += timedelta(days=1)
        feed._allow_paid_request(config(), LOOKUP)
        assert read(tmp_path)["reserved_milliusd"] == 10 and read(tmp_path)["count"] == 1


def test_legacy_current_month_consumes_budget_without_losing_daily_counter(tmp_path):
    path = tmp_path / "flightaware-usage.json"
    path.write_text(json.dumps({"day": "2026-10-31", "count": 7}))
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        with pytest.raises(PaidLimitError) as error:
            feed._allow_paid_request(config(), LOOKUP)
    assert error.value.reason == "monthly_limit"
    assert read(tmp_path)["count"] == 7 and read(tmp_path)["reserved_milliusd"] == 5000
    with patch("flightwall.feeds.utcnow", return_value=NOW + timedelta(days=1)):
        feed._allow_paid_request(config(), LOOKUP)
    assert read(tmp_path)["count"] == 1 and read(tmp_path)["reserved_milliusd"] == 5


def test_corrupt_future_or_incomplete_usage_fails_closed(tmp_path):
    feed = FlightFeeds(tmp_path)
    good = {"schema_version": 2, "day": "2026-10-31", "month": "2026-10", "count": 2,
            "reserved_milliusd": 15, "pricing_checked": PRICING_CHECKED.isoformat()}
    bad = ["corrupt", [], good | {"schema_version": 2.0}, good | {"count": True}, good | {"reserved_milliusd": 1},
           good | {"day": "2026-11-01", "month": "2026-11"}, good | {"month": "2026-09"},
           good | {"reserved_milliusd": 1.5}, good | {"pricing_checked": "2026-09-01"}, {"count": 2}]
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        for value in bad:
            (tmp_path / "flightaware-usage.json").write_text(value if isinstance(value, str) else json.dumps(value))
            with pytest.raises(PaidLimitError) as error:
                feed._allow_paid_request(config(), LOOKUP)
            assert error.value.reason == "usage_unknown"


def test_uncertain_network_failure_is_reserved_and_failed_save_never_calls_api(tmp_path):
    feed = FlightFeeds(tmp_path)
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch("flightwall.feeds.request_json", side_effect=OSError("offline")):
        with pytest.raises(OSError):
            feed._flightaware(LOOKUP, config())
    assert read(tmp_path)["reserved_milliusd"] == 5
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch("flightwall.feeds.private_json", side_effect=OSError("disk")), patch("flightwall.feeds.request_json") as network:
        with pytest.raises(PaidLimitError):
            feed._flightaware(POSITION, config())
        network.assert_not_called()
    assert read(tmp_path)["reserved_milliusd"] == 5


def test_pricing_requires_review_after_ninety_days_and_no_credits_are_assumed(tmp_path):
    feed = FlightFeeds(tmp_path)
    final = datetime.combine(PRICING_CHECKED + timedelta(days=PRICING_MAX_AGE_DAYS), datetime.min.time(), timezone.utc)
    with patch("flightwall.feeds.utcnow", return_value=final):
        feed._allow_paid_request(config(), LOOKUP)
    with patch("flightwall.feeds.utcnow", return_value=final + timedelta(days=1)):
        with pytest.raises(PaidLimitError) as error:
            feed._allow_paid_request(config(), LOOKUP)
    assert error.value.reason == "pricing_expired"
    assert read(tmp_path)["reserved_milliusd"] == 5


def test_os_lock_prevents_separate_processes_from_overspending(tmp_path):
    code = """
import sys
from datetime import datetime,timezone
from flightwall.config import DEFAULTS
import flightwall.feeds as f
f.utcnow=lambda: datetime(2026,10,31,18,tzinfo=timezone.utc)
try:
    f.FlightFeeds(sys.argv[1])._allow_paid_request(DEFAULTS|{'flightaware_monthly_limit_usd':0.025}, '/flights/DAL123?max_pages=1')
except f.PaidLimitError:
    sys.exit(3)
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(10)]
    results = []
    for process in processes:
        output, error = process.communicate(timeout=30)
        assert output == b"" and error == b""
        results.append(process.returncode)
    assert sorted(results) == [0] * 5 + [3] * 5
    assert read(tmp_path)["count"] == 5 and read(tmp_path)["reserved_milliusd"] == 25
