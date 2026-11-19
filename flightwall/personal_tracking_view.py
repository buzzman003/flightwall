"""Personal Lisa flight context, separate from crew duty and phone presence."""
from __future__ import annotations

from datetime import timedelta
import math

from .flight_alerts import SOURCE, stamp
from .time_format import format_time


def tracking_rows(state, config, now):
    """Apply current settings and source clocks to retained/paused TV frames."""
    if (not isinstance(state, dict) or state.get("demo") is True
            or isinstance(state.get("snapshot"), dict) and state["snapshot"].get("demo") is True
            or state.get("backend_available", True) is not True):
        return []
    from .personal_tracking import fresh_personal_tracking
    return fresh_personal_tracking(state.get("personal_tracking"), config, now,
                                   backend_available=True)


def _phase(row):
    budget = {"monthly_limit": "APP BUDGET REACHED", "daily_limit": "REQUEST LIMIT REACHED",
              "pricing_expired": "PRICING NEEDS REVIEW", "usage_unknown": "USAGE NEEDS REVIEW"}
    if row.get("reason") in budget:
        return budget[row["reason"]]
    unavailable = {"pending": "WAITING FOR UPDATE", "unconfigured": "AEROAPI NOT CONFIGURED",
                   "not_found": "PROVIDER FLIGHT NOT FOUND", "ambiguous": "FLIGHT MATCH AMBIGUOUS",
                   "stale": "PROVIDER UPDATE STALE", "unavailable": "UPDATE UNAVAILABLE"}
    if row.get("status") in unavailable:
        return unavailable[row["status"]]
    provider = row.get("provider")
    provider = provider if isinstance(provider, dict) else {}
    if provider.get("status") == "fresh":
        if provider.get("diverted") is True:
            return "DIVERTED / CHECK AIRLINE"
        if provider.get("cancelled") is True:
            return "TRACKING ENDED / CHECK AIRLINE"
        return {"scheduled": "SCHEDULED", "departed_gate": "GATE OUT", "airborne": "AIRBORNE",
                "blocked": "TRACKING BLOCKED",
                "tracking_ended": "TRACKING ENDED"}.get(provider.get("phase"), "FLIGHT UPDATE")
    return "UPDATE UNAVAILABLE"


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _arrival_clock(row, now):
    """A past touchdown/gate arrival remains a fact after position fixes expire."""
    provider = row["provider"]
    if (provider.get("source") != SOURCE or provider.get("status") not in {"fresh", "stale"}
            or provider.get("phase") not in {"at_gate", "landed"}
            or provider.get("diverted") is True or provider.get("cancelled") is True):
        return None
    times = provider.get("times")
    if not isinstance(times, dict):
        return None
    try:
        observed = stamp(provider["fetched_at"])
        actual = {event: stamp(times["actual_" + event]) for event in ("out", "off", "on", "in")
                  if times.get("actual_" + event)}
        clocks = list(actual.values())
        if not clocks or clocks != sorted(clocks) or not clocks[-1] <= observed <= now:
            return None
        earliest = stamp(row["flight"]["start"]) - timedelta(hours=1)
        if provider["phase"] == "at_gate" and "in" in actual and actual["in"] >= earliest:
            return "AT GATE", actual["in"]
        if "on" in actual and actual["on"] >= earliest:
            return "LANDED", actual["on"]
    except (TypeError, ValueError, KeyError, OverflowError):
        pass
    return None


def render_personal_tracking(draw, image, state, config, now):
    """Render only the currently eligible exact target, never a cached slot body.

    A listing or confirmed reservation associates a flight with the household;
    neither proves that Lisa boarded. Only independently fresh provider fixes
    become a map position. Schedule, observations and boarding stay distinct.
    """
    from .render import AMBER, BG, CYAN, LINE, MUTED, _dt, _map, _selected_card, _text, _tz, deck_footer_width
    card = _selected_card(state, "personal_tracking") or {}
    row = next((item for item in tracking_rows(state, config, now) if item.get("id") == card.get("id")), None)
    if row is None:
        return False
    flight = row["flight"]
    live = row.get("live")
    live = live if isinstance(live, dict) else None
    map_state = dict(state, live=live)
    zone = _tz(config)
    label = "LISA / " + flight["number"] + " / " + row["reservation_status"].upper()
    plotted = _map(draw, image, map_state, config, now, flight, zone, page=label)
    # The shared map caption is replaced with this card's schedule/observation
    # distinction. Its geography and crisp LED composition are left untouched.
    draw.rectangle((15, 209, 497, 229), fill=BG)
    start, end = _dt(flight["start"]), _dt(flight["end"])
    schedule = "PLANNED ROUTE / SCHED " + format_time(start.astimezone(zone)) + " - " + format_time(end.astimezone(zone), include_zone=True)
    _text(draw, (16, 211), schedule, MUTED, max_width=480)
    arrival = _arrival_clock(row, now)
    if arrival:
        event, when = arrival
        _text(draw, (16, 222), event + " " + format_time(when.astimezone(zone), include_zone=True), CYAN, max_width=480)
    elif live and plotted:
        observed = _dt(live.get("observed_at"))
        bits = ["OBS " + format_time(observed.astimezone(zone))]
        if live.get("altitude_units") == "feet" and _number(live.get("altitude"), -2000, 100000):
            bits.append(str(round(live["altitude"])) + " FT")
        if _number(live.get("groundspeed"), 0, 2000):
            bits.append("GS " + str(round(live["groundspeed"])) + " KT")
        if _number(live.get("heading"), 0, 360):
            bits.append(f"HDG {round(live['heading']) % 360:03d}")
        _text(draw, (16, 222), " / ".join(bits), CYAN, max_width=480)
    else:
        text = "OBSERVED POSITION OUTSIDE PLANNED MAP" if live else "POSITION NOT AVAILABLE"
        _text(draw, (16, 222), text, AMBER if live else MUTED, max_width=480)
    draw.line((14, 232, 498, 232), fill=LINE)
    phase = "ARRIVAL OBSERVED" if arrival else _phase(row)
    _text(draw, (15, 241), "FLIGHTAWARE / " + phase + " / BOARDING UNKNOWN", MUTED,
          max_width=deck_footer_width(state))
    return True
