"""Calendar-derived outlooks; schedules never establish physical presence."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib

UTC = timezone.utc


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Missing timezone")
    return result.astimezone(UTC)


def custody_outlook(intervals, last_sync, config, now, *, failed=False):
    zone = ZoneInfo(config.get("timezone", "UTC"))
    try:
        fresh = not failed and -300 <= (now - stamp(last_sync)).total_seconds() <= config.get("kids_stale_minutes", 30) * 60
    except (ValueError, TypeError, AttributeError):
        fresh = False
    spans = []
    if fresh:
        for item in intervals:
            try:
                start, end = stamp(item["start"]), stamp(item["end"])
                parent = item["parent"]
                if end > start and parent in {"Mom", "Dad"}:
                    spans.append((start, end, "home" if parent == "Mom" else "away"))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
    rows = []
    for offset in range(config.get("custody_outlook_days", 10)):
        day = now.astimezone(zone).date() + timedelta(days=offset)
        start = datetime.combine(day, time(), zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
        points = sorted({start, end} | {max(start, min(end, point)) for a, b, _ in spans for point in (a, b)})
        segments = []
        for a, b in zip(points, points[1:]):
            states = {state for s, e, state in spans if s <= a < e}
            state = next(iter(states)) if len(states) == 1 else "unknown"
            if segments and segments[-1]["state"] == state:
                segments[-1]["end"] = b.isoformat()
            else:
                segments.append({"state": state, "start": a.isoformat(), "end": b.isoformat()})
        states = {item["state"] for item in segments}
        # Partial coverage or a conflict is explicitly unknown, never guessed.
        state = "unknown" if "unknown" in states else next(iter(states)) if len(states) == 1 else "mixed"
        rows.append({"date": day.isoformat(), "state": state, "segments": segments})
    return {"days": rows, "stale": not fresh, "last_sync": last_sync,
            "timezone": str(zone), "error": "Calendar unavailable or stale" if not fresh else None}


def local_visits(snapshot, now, *, include_future=False):
    """Highlight today's local arrivals, including an overnight VPS stay.

    Match only the very next non-deadhead operating leg. A later flight from
    the same airport does not establish an uninterrupted layover.
    """
    if snapshot.get("demo"):
        return []
    zone = ZoneInfo("America/Chicago")
    legs = []
    for flight in snapshot.get("flights", []):
        try:
            start, end = stamp(flight["start"]), stamp(flight["end"])
            if start < end:
                legs.append((start, end, flight))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    legs.sort(key=lambda row: (row[0], row[1]))
    result = []
    for index, (_, arrival, flight) in enumerate(legs):
        airport = flight.get("destination")
        if airport not in {"VPS", "PNS", "ECP"} or flight.get("deadhead"):
            continue
        local_day = arrival.astimezone(zone).date()
        display_start = datetime.combine(local_day, time(), zone).astimezone(UTC)
        display_end = datetime.combine(local_day + timedelta(days=1), time(), zone).astimezone(UTC)
        item = {"id": hashlib.sha256((str(flight.get("id")) + arrival.isoformat()).encode()).hexdigest()[:20],
                "airport": airport, "arrival": arrival.isoformat(), "timezone": str(zone),
                "source": "Calendar schedule"}
        if airport == "VPS" and index + 1 < len(legs):
            departure, _, outbound = legs[index + 1]
            if not outbound.get("deadhead") and outbound.get("origin") == airport and arrival < departure <= arrival + timedelta(days=4):
                item.update(departure=departure.isoformat(), duration_minutes=int((departure - arrival).total_seconds() / 60))
                display_end = max(display_end, departure)
        item.update(display_start=display_start.isoformat(), display_end=display_end.isoformat())
        if display_start <= now < display_end or include_future and now < display_end:
            result.append(item)
    return result if include_future else result[:6]


def trip_preview(snapshot, config, now):
    """The next five calendar days with flights, starting at the next leg.

    A date range is deliberate: individual flight titles don't establish a
    roster rotation boundary. The display must not invent one from gaps.
    """
    if snapshot.get("demo"):
        return {}
    zone = ZoneInfo(config.get("timezone", "UTC"))
    flights = []
    for flight in snapshot.get("flights", []):
        try:
            start, end = stamp(flight["start"]), stamp(flight["end"])
            if end > start and end > now and (config.get("show_deadheads", True) or not flight.get("deadhead")):
                flights.append((start, end, flight))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    flights.sort(key=lambda row: (row[0], row[1]))
    if not flights or flights[0][0] > now + timedelta(days=config.get("flight_preview_days", 5)):
        return {}
    first_day = flights[0][0].astimezone(zone).date()
    end_day = first_day + timedelta(days=5)
    days, selected = {}, []
    for start, _, flight in flights:
        day = start.astimezone(zone).date()
        if day >= end_day:
            break
        legs = days.setdefault(day.isoformat(), [])
        item = {**flight, "leg_number": len(legs) + 1}
        legs.append(item)
        selected.append(item)
    return {"start_date": first_day.isoformat(), "end_date": max(days), "timezone": str(zone),
            "days": [{"date": day, "legs": legs} for day, legs in days.items()],
            "flights": selected, "local_visits": [item for item in local_visits(snapshot, now, include_future=True)
                if first_day <= stamp(item["arrival"]).astimezone(zone).date() < end_day],
            "last_sync": snapshot.get("last_sync"), "error": snapshot.get("error"),
            "source": "Calendar schedule"}
