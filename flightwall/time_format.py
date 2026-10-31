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
