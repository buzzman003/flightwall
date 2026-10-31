"""Narrow Buzz overnight leave-time extraction; no note text is retained.

The owner's supported note format names a VPS Home Overnight and separates
the requested formula's RECOMMENDED clock from an explicit report-safe clock.
These are planning notes, never live tracking or a recalculated report time.
"""
from datetime import datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from .calendar_client import _event_time

UTC = timezone.utc
_ZONE = ZoneInfo("America/Chicago")
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_CLOCK = r"(\d{1,2}:[0-5]\d\s*(?:AM|PM))"
_TITLE = re.compile(
    r"^(?:🏠\s*)?BUZZ FLIGHT BRIEF\s*[—–-]\s*VPS HOME OVERNIGHT\s*"
    r"\(([A-Z]{3})\s+(\d{1,2})\s*[—–-]\s*(?:([A-Z]{3})\s+)?(\d{1,2})\)$"
)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Missing timezone")
    return result.astimezone(UTC)


def _clock_before_report(value, arrival, report):
    """Accept exactly one civil-time candidate within the overnight interval."""
    match = re.fullmatch(r"(\d{1,2}):([0-5]\d)\s*(AM|PM)", value.upper())
    if not match or not 1 <= int(match[1]) <= 12:
        return None
    hour = int(match[1]) % 12 + (12 if match[3] == "PM" else 0)
    candidates = set()
    for day in (report.astimezone(_ZONE).date(), report.astimezone(_ZONE).date() - timedelta(days=1)):
        naive = datetime.combine(day, time(hour, int(match[2])))
        for fold in (0, 1):
            candidate = naive.replace(tzinfo=_ZONE, fold=fold).astimezone(UTC)
            if (candidate.astimezone(_ZONE).replace(tzinfo=None) == naive
                    and max(arrival, report - timedelta(days=1)) < candidate <= report):
                candidates.add(candidate)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _latest(events):
    unique, anonymous = {}, []
    def updated(event):
        try:
            return _stamp(event.get("updated"))
        except (ValueError, TypeError, OverflowError):
            return datetime.min.replace(tzinfo=UTC)
    for event in events:
        if not isinstance(event, dict):
            continue
        key = event.get("id")
        if isinstance(key, str) and key:
            previous = unique.get(key)
            if previous is None or updated(event) > updated(previous) or (updated(event) == updated(previous) and event.get("status") == "cancelled"):
                unique[key] = event
        else:
            anonymous.append(event)
    return [*unique.values(), *anonymous]


def parse_brief_leave_notes(events, timezone_name):
    """Recognize only the confirmed Central-time VPS overnight note format."""
    if timezone_name != "America/Chicago":
        return []
    rows = []
    for event in _latest(events):
        if event.get("status") == "cancelled":
            continue
        title = event.get("summary")
        title_match = _TITLE.fullmatch(title.strip().upper()) if isinstance(title, str) else None
        if not title_match:
            continue
        arrival, report = _event_time(event.get("start")), _event_time(event.get("end"))
        if arrival is None or report is None:
            continue
        arrival, report = arrival.astimezone(UTC), report.astimezone(UTC)
        if not arrival < report <= arrival + timedelta(days=4):
            continue
        first, last = arrival.astimezone(_ZONE).date(), report.astimezone(_ZONE).date()
        if (_MONTHS[first.month - 1] != title_match[1] or first.day != int(title_match[2])
                or _MONTHS[last.month - 1] != (title_match[3] or title_match[1]) or last.day != int(title_match[4])):
            continue
        note = event.get("description")
        if not isinstance(note, str) or len(note) > 32768 or "<" in note or ">" in note:
            continue
        lines = note.splitlines()
        recommended = []
        for index, line in enumerate(lines):
            if re.match(r"^\s*LEAVE HOME FOR VPS\s*[—–:-]", line, re.IGNORECASE):
                block = " ".join(lines[index:index + 3])
                recommended.extend(re.findall(r"\bRECOMMENDED\s+" + _CLOCK + r"\b", block, re.IGNORECASE))
        compact = " ".join(note.split())
        report_safe = re.findall(r"\bTo meet scheduled report on time,\s*leave by\s+" + _CLOCK + r"\b", compact, re.IGNORECASE)
        # A partly recognized conflict cannot silently become an authoritative
        # single time. Competing values within one label are also unsupported.
        recommended = {value.upper().replace(" ", "") for value in recommended}
        report_safe = {value.upper().replace(" ", "") for value in report_safe}
        if len(recommended) > 1 or len(report_safe) > 1:
            continue
        if re.search(r"\bREPORT-TIME CONFLICT\b", compact, re.IGNORECASE) and not (recommended and report_safe):
            continue
        row = {"airport": "VPS", "arrival": arrival.isoformat(), "report": report.isoformat()}
        invalid = False
        for key, values in (("recommended_at", recommended), ("report_safe_at", report_safe)):
            if values:
                instant = _clock_before_report(next(iter(values)), arrival, report)
                if instant is None:
                    invalid = True
                    break
                row[key] = instant.isoformat()
        if not invalid and len(row) > 3 and row not in rows:
            rows.append(row)
    return rows[:100]


def clean_brief_leaves(value):
    """Validate minimal private cache records without relaying extra fields."""
    rows = []
    if not isinstance(value, list):
        return rows
    for item in value[:100]:
        if not isinstance(item, dict) or not {"airport", "arrival", "report"}.issubset(item):
            continue
        if set(item) - {"airport", "arrival", "report", "recommended_at", "report_safe_at"} or item["airport"] != "VPS":
            continue
        try:
            arrival, report = _stamp(item["arrival"]), _stamp(item["report"])
            if not arrival < report <= arrival + timedelta(days=4):
                continue
            row = {"airport": "VPS", "arrival": arrival.isoformat(), "report": report.isoformat()}
            for key in ("recommended_at", "report_safe_at"):
                if key in item:
                    instant = _stamp(item[key])
                    if not max(arrival, report - timedelta(days=1)) < instant <= report:
                        raise ValueError("Leave time is outside its overnight interval")
                    row[key] = instant.isoformat()
            if len(row) > 3 and row not in rows:
                rows.append(row)
        except (ValueError, TypeError, OverflowError):
            continue
    return rows


def attach_brief_leaves(flights, notes, policy="conflict"):
    """Attach only an exact, uninterrupted arrival/report/outbound match.

    Input flights are caller-owned merged copies. An uncertain match never
    supplies a leave time. Conflicting notes remain flagged under every policy.
    """
    if policy not in {"conflict", "recommended", "report_safe"}:
        policy = "conflict"
    legs = []
    for flight in flights:
        flight.pop("leave_at", None)
        flight.pop("leave_conflict", None)
        try:
            start, end = _stamp(flight["start"]), _stamp(flight["end"])
            if start < end:
                legs.append((start, end, flight))
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    legs.sort(key=lambda row: (row[0], row[1]))
    matched = {}
    for note in clean_brief_leaves(notes):
        arrival, report = _stamp(note["arrival"]), _stamp(note["report"])
        possible = []
        for index, (departure, _, flight) in enumerate(legs):
            if not index or flight.get("origin") != "VPS" or flight.get("deadhead"):
                continue
            try:
                if _stamp(flight.get("report")) != report or not report <= departure <= report + timedelta(days=1):
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            _, previous_end, previous = legs[index - 1]
            if previous_end == arrival and previous.get("destination") == "VPS" and not previous.get("deadhead"):
                possible.append(index)
        if len(possible) == 1:
            matched.setdefault(possible[0], []).append(note)
    for index, records in matched.items():
        flight = legs[index][2]
        values = {row[key] for row in records for key in ("recommended_at", "report_safe_at") if key in row}
        if len(values) > 1:
            flight["leave_conflict"] = True
        selected = ({row[policy + "_at"] for row in records if policy + "_at" in row}
                    if policy != "conflict" else values)
        if len(selected) == 1:
            flight["leave_at"] = next(iter(selected))
