"""A household view of verified clocks, observed status and airport context."""
from .time_format import format_leave_clock, format_time


PHASE_LABELS = {
    "schedule_only": "SCHEDULE ONLY", "scheduled": "NO ACTUAL EVENT YET", "taxi_out": "TAXI-OUT PHASE",
    "airborne": "AIRBORNE", "taxi_in": "TAXI-IN PHASE", "at_gate": "AT ARRIVAL GATE",
    "diverted": "DIVERTED", "tracking_ended": "TRACKING ENDED", "blocked": "TRACKING BLOCKED",
}
AIRPORT_LABELS = {"ground_stop": "GROUND STOP", "ground_delay": "GROUND DELAY",
                  "arrival_delay": "ARRIVAL DELAY", "departure_delay": "DEPARTURE DELAY"}
EVENT_LABELS = {"out": "GATE OUT", "off": "TAKEOFF", "on": "LANDING", "in": "GATE IN"}


def brief_rows(context, config):
    """Return fixed, source-labeled rows. Only optional rows have a focus category."""
    if not context or config.get("guest_mode") is True:
        return []
    from .render import _dt, _tz

    def clock(value):
        return format_time(_dt(value).astimezone(_tz(config)), date_format="%a %m/%d", include_zone=True)

    rows = []
    phase = context["phase"]
    if phase in {"diverted", "tracking_ended", "blocked"}:
        rows.append(("", PHASE_LABELS[phase], "FLIGHTAWARE UPDATE / CHECK AIRLINE", True))
    visit = context.get("vps_visit") or {}
    if visit.get("brief_leave_conflict"):
        rows.append(("", "VPS LEAVE-TIME CONFLICT", "CHECK BUZZ FLIGHT BRIEF", True))
    for advisory in context["airport_advisories"]:
        if advisory["kind"] == "ground_stop":
            rows.append(("", f"{advisory['airport']} GROUND STOP", "FAA AIRPORT ADVISORY / CHECK AIRLINE", True))
    leave = format_leave_clock(_dt(visit.get("brief_leave_at")), _dt(visit.get("brief_leave_window_start")),
                               _dt(visit.get("brief_leave_window_end")), _tz(config))
    if leave:
        detail = "BUZZ BRIEF / " + leave
        boundary = _dt(visit.get("brief_leave_boundary_at"))
        if boundary:
            detail += " / HARD BOUNDARY " + format_time(boundary.astimezone(_tz(config)), include_zone=True)
        rows.append(("leave_time", "VPS / LEAVE HOME BY", detail, True))
    if visit.get("scheduled_departure_at"):
        rows.append(("vps_departure", "VPS / NEXT DEPARTURE", "SCHEDULE / " + clock(visit["scheduled_departure_at"]), False))
    for direction in ("arrival", "departure"):
        update = (context.get("provider") or {}).get(direction) or {}
        delta = update.get("delta_minutes")
        if delta is not None and abs(delta) >= 10:
            timing = "LATE" if delta > 0 else "EARLY"
            label = f"{EVENT_LABELS[update['event']]} / {round(abs(delta))} MIN {timing}"
            source = f"FLIGHTAWARE {update['basis'].upper()} / " + clock(update["at"])
            rows.append((direction + "_change", label, source, False))
    for advisory in context["airport_advisories"]:
        if advisory["kind"] != "ground_stop":
            label = f"{advisory['airport']} {AIRPORT_LABELS[advisory['kind']]}"
            rows.append(("airport_delay", label, "FAA AIRPORT ADVISORY / CHECK AIRLINE", False))
    for weather in context.get("weather", ()):
        if weather["flight_category"] != "VFR":
            label = f"{weather['airport']} / {weather['flight_category']} OBSERVED"
            rows.append(("weather", label, "AIRPORT METAR / " + clock(weather["observed_at"]), False))
    return rows


def render_brief(draw, state, config, now):
    from .render import AMBER, CYAN, LINE, MUTED, PANEL, WHITE, _header, _text

    _header(draw, state, now, "FLIGHT BRIEF")
    context = state.get("flight_context")
    rows = brief_rows(context, config)
    if not rows:
        _text(draw, (256, 125), "NO CURRENT FLIGHT BRIEF", MUTED, 2, align="center")
        return
    _text(draw, (15, 51), f"{context['flight_number']}  {context['origin']} > {context['destination']}", CYAN,
          max_width=240)
    _text(draw, (496, 51), PHASE_LABELS[context["phase"]], CYAN, align="right", max_width=220)
    preference = state.get("brief_focus")
    pinned = {"leave_time", "vps_departure"} if any(row[0] == "leave_time" for row in rows) else set()
    def fixed(row):
        return row[3] or row[0] in pinned

    rows.sort(key=lambda row: (not fixed(row), row[0] != preference))
    count = min(len(rows), max(4, min(6, sum(fixed(row) for row in rows) + 1)))
    spacing = 168 // max(4, count)
    for index, (_, title, detail, important) in enumerate(rows[:count]):
        y = 62 + index * spacing
        draw.rectangle((15, y, 497, y + spacing - 3), fill=PANEL, outline=LINE)
        _text(draw, (23, y + 3), title, AMBER if important else WHITE, 2, max_width=459)
        _text(draw, (23, y + 18), detail, MUTED, max_width=459)
