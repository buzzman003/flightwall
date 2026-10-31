"""Pure 512x256 operating-flight provider card; no network or persistence."""
from __future__ import annotations

import math
import re

from .time_format import format_time


def _code(value):
    return value.upper() if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value) else "---"


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _column(draw, data, x, name, zone):
    from .render import AMBER, CYAN, GREEN, LINE, MUTED, PANEL, WHITE, _dt, _text
    data = data if isinstance(data, dict) else {}
    event = data.get("event")
    allowed = {"out", "off"} if name == "DEPARTURE" else {"in", "on"}
    event_name = {"out": "GATE OUT", "off": "TAKEOFF", "in": "GATE IN", "on": "LANDING"}.get(event, "EVENT UNKNOWN")
    draw.rectangle((x, 110, x + 236, 224), fill=PANEL, outline=LINE)
    _text(draw, (x + 9, 118), name + " / " + event_name, CYAN, max_width=220)
    basis = data.get("basis")
    selected = _dt(data.get("time")) if basis in {"estimated", "actual"} else None
    # Never display a timestamp whose event or stated basis is unsupported.
    matching_basis = _dt(data.get(basis)) if basis in {"estimated", "actual"} else None
    selected = selected if selected and selected == matching_basis and event in allowed else None
    label = basis.upper() if selected else "TIME UNKNOWN"
    _text(draw, (x + 9, 136), label, MUTED)
    if selected:
        local = selected.astimezone(zone)
        _text(draw, (x + 8, 148), format_time(local), WHITE, 3, max_width=220)
        _text(draw, (x + 9, 174), local.strftime("%a %m/%d / %Z"), MUTED, max_width=220)
    else:
        _text(draw, (x + 9, 150), "UNKNOWN", MUTED, 2, max_width=220)
    scheduled = _dt(data.get("scheduled")) if event in allowed else None
    baseline = ("SCHED " + format_time(scheduled.astimezone(zone), date_format="%m/%d")
                if scheduled else "SCHEDULED BASELINE UNKNOWN")
    _text(draw, (x + 9, 189), baseline, MUTED, max_width=220)
    delta = data.get("delta_minutes") if selected and scheduled else None
    if _number(delta) and abs(delta - (selected - scheduled).total_seconds() / 60) > 1e-6:
        delta = None
    if not _number(delta):
        text, color = "DELTA UNKNOWN", MUTED
    elif delta == 0:
        text, color = "ON TIME", GREEN
    else:
        magnitude = "<1" if abs(delta) < 1 else str(round(abs(delta)))
        text, color = magnitude + " MIN " + ("LATE" if delta > 0 else "EARLY"), AMBER if delta > 0 else GREEN
    _text(draw, (x + 9, 205), text, color, 2, max_width=220)


def _footer(draw, state, status, config, now, *, fresh):
    from .render import LINE, MUTED, _dt, _text, _tz, deck_footer_width
    draw.line((14, 232, 498, 232), fill=LINE)
    fetched = _dt(status.get("fetched_at"))
    if fetched and fetched <= now:
        age = max(0, int((now - fetched).total_seconds() // 60))
        stamp = format_time(fetched.astimezone(_tz(config)), date_format="%m/%d", include_zone=True)
        source = "FLIGHTAWARE / " + stamp + (f" / {age}M AGO" if fresh else " / STALE")
    else:
        source = "FLIGHTAWARE / NO CURRENT OBSERVATION"
    _text(draw, (15, 241), source, MUTED, max_width=deck_footer_width(state))


def render_operating(draw, image, state, config, now):
    """Render received status, hiding expired estimates on every frame.

    Gate-out/in and takeoff/landing events retain their own provider baselines.
    A provider tracking-ended flag never becomes an airline-cancellation claim.
    Calendar/report times remain in their existing independent schedule views.
    """
    from .flight_alerts import fresh_status
    from .render import AMBER, CYAN, LINE, MUTED, PANEL, WHITE, _header, _text, _tz
    _header(draw, state, now, "MY FLIGHT / PROVIDER UPDATE")
    original = state.get("operating_status")
    original = original if isinstance(original, dict) else {}
    enabled = config.get("flightaware_enabled") is True
    demo = state.get("demo", config.get("demo", False)) is True
    if not enabled or demo:
        status = {"status": "demo" if demo else "off"}
    else:
        status = fresh_status(original, now, backend_available=state.get("backend_available", True) is True)
    mode = status.get("status")
    fresh = mode == "fresh"
    if not fresh:
        title = {"off": "FLIGHT STATUS OFF", "demo": "DEMO / NO PROVIDER REQUESTS",
                 "unconfigured": "AEROAPI KEY NOT CONFIGURED", "pending": "WAITING FOR FLIGHT STATUS",
                 "outside_window": "OUTSIDE STATUS CHECK WINDOW", "idle": "NO OPERATING FLIGHT TO CHECK",
                 "not_found": "NO MATCHING PROVIDER FLIGHT", "ambiguous": "FLIGHT MATCH AMBIGUOUS",
                 "stale": "PROVIDER UPDATE STALE"}.get(mode, "STATUS UPDATE UNAVAILABLE")
        title = {"monthly_limit": "MONTHLY APP BUDGET REACHED",
                 "daily_limit": "DAILY REQUEST LIMIT REACHED",
                 "pricing_expired": "AEROAPI PRICING NEEDS REVIEW",
                 "usage_unknown": "API USAGE COUNTER NEEDS REVIEW",
                 "airline_required": "CHOOSE YOUR OPERATING AIRLINE"}.get(status.get("reason"), title)
        _text(draw, (256, 110), title, AMBER, 2, align="center", max_width=464)
        _text(draw, (256, 146), "CALENDAR TIMES REMAIN PLANNED", MUTED, align="center", max_width=464)
        _text(draw, (256, 171), "CHECK AIRLINE FOR OPERATING DECISIONS", MUTED, align="center", max_width=464)
        _footer(draw, state, status, config, now, fresh=False)
        return
    draw.rectangle((14, 56, 498, 84), fill=PANEL, outline=LINE)
    number = status.get("number")
    number = number.upper() if isinstance(number, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{0,15}", number) else "MY FLIGHT"
    _text(draw, (23, 64), number, WHITE, 2, max_width=206)
    _text(draw, (489, 64), _code(status.get("origin")) + " > " + _code(status.get("destination")), WHITE, 2,
          align="right", max_width=220)
    phase = status.get("phase")
    if status.get("diverted") is True:
        label = "DIVERTED / CHECK AIRLINE"
        if status.get("cancelled") is True or phase == "tracking_ended":
            label = "DIVERTED / TRACKING ENDED"
        color = AMBER
    elif status.get("cancelled") is True or phase == "tracking_ended":
        label, color = "TRACKING ENDED / CHECK AIRLINE", AMBER
    elif phase == "blocked":
        label, color = "TRACKING BLOCKED / CHECK AIRLINE", AMBER
    else:
        label = {"scheduled": "SCHEDULED / PROVIDER UPDATE", "departed_gate": "DEPARTED GATE",
                 "airborne": "AIRBORNE", "landed": "LANDED", "at_gate": "AT ARRIVAL GATE"}.get(phase, "FLIGHT PHASE UNKNOWN")
        color = CYAN
    _text(draw, (256, 93), label, color, align="center", max_width=464)
    zone = _tz(config)
    _column(draw, status.get("departure"), 14, "DEPARTURE", zone)
    _column(draw, status.get("arrival"), 262, "ARRIVAL", zone)
    _footer(draw, state, status, config, now, fresh=True)
