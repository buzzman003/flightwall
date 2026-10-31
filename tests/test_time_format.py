"""AM/PM is presentation only; dates, timezone offsets and DST remain exact."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from flightwall.time_format import format_time


@pytest.mark.parametrize(("hour", "expected"), (
    (0, "12:05 AM"), (1, "1:05 AM"), (11, "11:05 AM"),
    (12, "12:05 PM"), (13, "1:05 PM"), (23, "11:05 PM"),
))
def test_midnight_noon_and_every_half_of_the_day(hour, expected):
    stamp = datetime(2026, 10, 31, hour, 5, tzinfo=timezone.utc)
    original = stamp.isoformat()
    assert format_time(stamp) == expected
    assert stamp.isoformat() == original


def test_central_date_and_repeated_dst_hour_have_explicit_zone_labels():
    zone = ZoneInfo("America/Chicago")
    before = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc).astimezone(zone)
    after = datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc).astimezone(zone)
    assert format_time(before, include_zone=True, date_format="%m/%d") == "11/01 1:30 AM CDT"
    assert format_time(after, include_zone=True, date_format="%m/%d") == "11/01 1:30 AM CST"
    previous_date = datetime(2026, 11, 1, 4, 59, tzinfo=timezone.utc).astimezone(zone)
    assert format_time(previous_date, include_zone=True, date_format="%m/%d") == "10/31 11:59 PM CDT"


@pytest.mark.parametrize("value", (None, "2026-10-31T18:00:00Z", datetime(2026, 10, 31, 18)))
def test_ambiguous_naive_clock_is_never_assumed_to_use_the_host_zone(value):
    with pytest.raises(ValueError, match="timezone-aware"):
        format_time(value)
