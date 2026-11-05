"""Nearby route metadata cannot consume the operating flight reserve."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds, PaidLimitError, PRICING_CHECKED

NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)
LOOKUP = "/flights/DAL123?max_pages=1"
POSITION = "/flights/provider-id/position"


def config(**changes):
    return DEFAULTS | {"flightaware_monthly_limit_usd": 5, "nearby_enrichment_monthly_limit_usd": 1, **changes}


def ledger(**changes):
    return {"schema_version": 2, "day": "2026-10-31", "month": "2026-10", "count": 10,
            "reserved_milliusd": 3500, "pricing_checked": PRICING_CHECKED.isoformat(),
            "nearby_reserved_milliusd": 0, "nearby_day_count": 0, **changes}


def save(tmp_path, value):
    (tmp_path / "flightaware-usage.json").write_text(json.dumps(value))


def read(tmp_path):
    return json.loads((tmp_path / "flightaware-usage.json").read_text())


def test_known_prior_schema2_total_is_preserved_and_new_nearby_counter_starts_zero(tmp_path):
    previous = ledger(reserved_milliusd=120)
    previous.pop("nearby_reserved_milliusd")
    previous.pop("nearby_day_count")
    save(tmp_path, previous)
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        FlightFeeds(tmp_path)._allow_paid_request(config(), LOOKUP, purpose="nearby")
    assert read(tmp_path) == previous | {"count": 11, "reserved_milliusd": 125,
                                        "nearby_reserved_milliusd": 5, "nearby_day_count": 1}


def test_nearby_month_subcap_rejects_next_lookup_but_own_positions_remain_available(tmp_path):
    save(tmp_path, ledger(nearby_reserved_milliusd=1000))
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        with pytest.raises(PaidLimitError) as error:
            feed._allow_paid_request(config(), LOOKUP, purpose="nearby")
        assert error.value.reason == "nearby_limit"
        feed._allow_paid_request(config(), POSITION)
    assert read(tmp_path)["reserved_milliusd"] == 3510
    assert read(tmp_path)["nearby_reserved_milliusd"] == 1000


def test_nearby_stops_at_eighty_percent_total_and_does_not_use_final_operating_reserve(tmp_path):
    save(tmp_path, ledger(reserved_milliusd=4000))
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        with pytest.raises(PaidLimitError) as error:
            feed._allow_paid_request(config(), LOOKUP, purpose="nearby")
        assert error.value.reason == "nearby_limit"
        feed._allow_paid_request(config(), POSITION)
    assert read(tmp_path)["reserved_milliusd"] == 4010 and read(tmp_path)["count"] == 11
    assert read(tmp_path)["nearby_reserved_milliusd"] == 0


@pytest.mark.parametrize("daily, nearby", [(240, 20), (50, 10), (4, 0)])
def test_daily_nearby_subcap_is_twenty_or_twenty_percent_whichever_is_less(tmp_path, daily, nearby):
    save(tmp_path, ledger(count=nearby, reserved_milliusd=nearby * 5,
                          nearby_reserved_milliusd=nearby * 5, nearby_day_count=nearby))
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        with pytest.raises(PaidLimitError) as error:
            feed._allow_paid_request(config(flightaware_daily_limit=daily), LOOKUP, purpose="nearby")
        assert error.value.reason == "nearby_limit"
        feed._allow_paid_request(config(flightaware_daily_limit=daily), POSITION)
    assert read(tmp_path)["count"] == nearby + 1


def test_lower_budget_limits_nearby_to_twenty_percent_even_if_one_dollar_is_configured(tmp_path):
    save(tmp_path, ledger(reserved_milliusd=200, nearby_reserved_milliusd=200))
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        with pytest.raises(PaidLimitError) as error:
            FlightFeeds(tmp_path)._allow_paid_request(config(flightaware_monthly_limit_usd=1), LOOKUP, purpose="nearby")
    assert error.value.reason == "nearby_limit"


def test_day_and_month_rollovers_do_not_erase_monthly_nearby_spend(tmp_path):
    yesterday = NOW - timedelta(days=1)
    save(tmp_path, ledger(day=yesterday.date().isoformat(), nearby_reserved_milliusd=25, nearby_day_count=3))
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW) as now:
        feed._allow_paid_request(config(), LOOKUP, purpose="nearby")
        assert read(tmp_path)["nearby_reserved_milliusd"] == 30 and read(tmp_path)["nearby_day_count"] == 1
        now.return_value += timedelta(days=1)
        feed._allow_paid_request(config(), LOOKUP, purpose="nearby")
    assert read(tmp_path)["reserved_milliusd"] == read(tmp_path)["nearby_reserved_milliusd"] == 5
    assert read(tmp_path)["count"] == read(tmp_path)["nearby_day_count"] == 1


@pytest.mark.parametrize("changes", [
    {"nearby_day_count": True}, {"nearby_day_count": -1}, {"nearby_day_count": 11},
    {"nearby_reserved_milliusd": -1}, {"nearby_reserved_milliusd": 5.0},
    {"nearby_reserved_milliusd": 7}, {"reserved_milliusd": 3501},
    {"nearby_reserved_milliusd": 3501}, {"nearby_day_count": 2, "nearby_reserved_milliusd": 5},
])
def test_corrupt_subcounters_fail_closed_without_network_or_accounting_reset(tmp_path, changes):
    value = ledger(**changes)
    save(tmp_path, value)
    with patch("flightwall.feeds.utcnow", return_value=NOW), pytest.raises(PaidLimitError) as error:
        FlightFeeds(tmp_path)._allow_paid_request(config(), LOOKUP, purpose="nearby")
    assert error.value.reason == "usage_unknown" and read(tmp_path) == value


def test_partial_new_counter_pair_is_not_treated_as_a_prior_schema(tmp_path):
    value = ledger()
    value.pop("nearby_day_count")
    save(tmp_path, value)
    with patch("flightwall.feeds.utcnow", return_value=NOW), pytest.raises(PaidLimitError) as error:
        FlightFeeds(tmp_path)._allow_paid_request(config(), LOOKUP)
    assert error.value.reason == "usage_unknown"


def test_nearby_budget_never_buys_duplicate_paid_positions(tmp_path):
    with patch("flightwall.feeds.utcnow", return_value=NOW), pytest.raises(ValueError):
        FlightFeeds(tmp_path)._allow_paid_request(config(), POSITION, purpose="nearby")
    assert not (tmp_path / "flightaware-usage.json").exists()


def test_registration_request_is_priced_and_allowed_only_for_exact_nearby_tail(tmp_path):
    lookup = "/flights/N123AB?ident_type=registration&max_pages=1"
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        with pytest.raises(ValueError):
            feed._allow_paid_request(config(), lookup)
        for unsafe in ("search", "DAL123", "N000", "N123IO", "N12345AB", "N123AB%2Ftrack"):
            with pytest.raises(ValueError):
                feed._allow_paid_request(config(), lookup.replace("N123AB", unsafe), purpose="nearby")
        assert not (tmp_path / "flightaware-usage.json").exists()
        feed._allow_paid_request(config(), lookup, purpose="nearby")
    assert read(tmp_path)["reserved_milliusd"] == read(tmp_path)["nearby_reserved_milliusd"] == 5
    assert read(tmp_path)["count"] == read(tmp_path)["nearby_day_count"] == 1


def test_concurrent_nearby_requests_share_one_durable_subcap_and_leave_own_budget(tmp_path):
    code = """
import sys
from datetime import datetime,timezone
from flightwall.config import DEFAULTS
import flightwall.feeds as f
f.utcnow=lambda: datetime(2026,10,31,18,tzinfo=timezone.utc)
try:
    f.FlightFeeds(sys.argv[1])._allow_paid_request(DEFAULTS|{'nearby_enrichment_monthly_limit_usd':0.01}, '/flights/DAL123?max_pages=1', purpose='nearby')
except f.PaidLimitError:
    sys.exit(3)
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(6)]
    results = []
    for process in processes:
        output, error = process.communicate(timeout=30)
        assert output == b"" and error == b""
        results.append(process.returncode)
    assert sorted(results) == [0] * 2 + [3] * 4
    with patch("flightwall.feeds.utcnow", return_value=NOW):
        FlightFeeds(tmp_path)._allow_paid_request(config(), POSITION)
    assert read(tmp_path)["nearby_reserved_milliusd"] == 10
    assert read(tmp_path)["reserved_milliusd"] == 20 and read(tmp_path)["count"] == 3
