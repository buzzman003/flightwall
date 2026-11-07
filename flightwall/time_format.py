"""Human clock labels only; stored instants remain timezone-aware RFC3339."""
from __future__ import annotations

from datetime import datetime


def format_time(value: datetime, *, include_zone: bool = False,
                date_format: str | None = None) -> str:
    """Format an already-localized instant with an explicit AM/PM marker.

    The caller chooses the display timezone before formatting, preserving its
    date and DST abbreviation. Avoid platform-specific ``%-I``/``%#I`` flags.
    A naive value is rejected rather than assuming the host machine's zone.
    """
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Clock labels require a timezone-aware datetime")
    label = value.strftime("%I:%M %p").lstrip("0")
    if include_zone:
        label += " " + value.strftime("%Z")
    if date_format:
        label = value.strftime(date_format) + " " + label
    return label


def format_leave_clock(instant, start, end, zone):
    """Label a verified planning clock or same-day window in local time."""
    if start and end and start < end:
        first, last = start.astimezone(zone), end.astimezone(zone)
        if first.date() == last.date() and first.tzname() == last.tzname():
            left = format_time(first)
            if first.hour // 12 == last.hour // 12:
                left = left.rsplit(" ", 1)[0]
            return first.strftime("%a %m/%d ") + left + "-" + format_time(last, include_zone=True)
    return format_time(instant.astimezone(zone), date_format="%a %m/%d", include_zone=True) if instant else None
