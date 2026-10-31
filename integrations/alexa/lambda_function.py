"""Household Flight Wall custom skill. Handler: lambda_function.lambda_handler.

Deploy only behind an Alexa Skills Kit Lambda trigger restricted to your Skill
ID. This module uses Python's standard library and never logs events or titles.
Google Calendar remains read-only. Voice routes cannot expose operating rosters.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hmac
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_RESPONSE_BYTES = 64 * 1024
HTTP_TIMEOUT_SECONDS = 2.5  # Context + write stay within Alexa's response budget.
MAX_TITLE = 160
MAX_REQUEST_ID = 512
PENDING_FIELDS = {"kind", "title", "date", "time", "duration_minutes", "request_id",
                  "timezone", "dinner_start", "dinner_end", "phase", "label", "duration_seconds",
                  "airline", "flight_number", "action", "item"}
WELCOME = "Flight Wall. What would you like?"
HELP = ("You can say, start a pasta timer for five minutes, list timers, or cancel the pasta timer. "
        "For an appointment, say, add a dentist appointment on Friday at nine A.M. "
        "To watch a public flight, say, track Delta flight one twenty three on Friday. "
        "Say shopping list to show it, add olives, or remove olives. Clear shopping list asks for confirmation. "
        "I confirm appointments and flight requests before saving. For dinner instructions, say dinner help.")
DINNER_HELP = ("Say, we are having chicken and rice for dinner. To choose another day, first say, "
               "set dinner for tomorrow, then give the meal. A meal replaces that date's earlier dinner card.")
AIRLINES = {"delta": "DL", "delta air lines": "DL", "delta airlines": "DL", "dl": "DL",
            "american": "AA", "american airlines": "AA", "aa": "AA",
            "united": "UA", "united airlines": "UA", "ua": "UA",
            "allegiant": "G4", "allegiant air": "G4", "g4": "G4", "g four": "G4",
            "frontier": "F9", "frontier airlines": "F9", "f9": "F9", "f nine": "F9",
            "air arabia": "G9", "g9": "G9", "g nine": "G9"}


class BoardError(Exception):
    def __init__(self, *, retryable=True, public_message=None):
        self.retryable = retryable
        self.public_message = public_message
        super().__init__("The board request could not be confirmed.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the dedicated credential to another host, even over HTTPS.
        return None


def _origin(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise BoardError(retryable=False)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BoardError(retryable=False) from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or parsed.username is not None
            or parsed.password is not None or port == 0):
        raise BoardError(retryable=False)
    return value.rstrip("/")


class BoardClient:
    def __init__(self):
        self.base = _origin(os.environ.get("FLIGHTWALL_BASE_URL", ""))
        self.token = os.environ.get("FLIGHTWALL_ALEXA_TOKEN", "")
        if (not 24 <= len(self.token) <= 1024
                or any(ord(c) < 33 or ord(c) > 126 for c in self.token)):
            raise BoardError(retryable=False)
        self.opener = build_opener(_NoRedirect())

    def _request(self, path, payload=None):
        if path not in ("/api/v1/voice-context", "/api/v1/personal-items", "/api/v1/timers",
                        "/api/v1/timers/cancel", "/api/v1/flight-watches", "/api/v1/shopping-list",
                        "/api/v1/shopping-list/add", "/api/v1/shopping-list/remove",
                        "/api/v1/shopping-list/clear", "/api/v1/shopping-list/show"):
            raise BoardError(retryable=False)
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(self.base + path, data=data, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "FlightWall-Alexa",
        })
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                if response.geturl() != self.base + path or not 200 <= response.status < 300:
                    raise BoardError(retryable=False)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise BoardError()
                result = json.loads(raw)
                if not isinstance(result, dict) or "error" in result:
                    raise BoardError(retryable=False)
                return result
        except HTTPError as exc:
            message = None
            if exc.code == 400:
                try:
                    raw = exc.read(4097)
                    body = json.loads(raw) if len(raw) <= 4096 else {}
                    candidate = body.get("error") if isinstance(body, dict) else None
                    if (isinstance(candidate, str) and 1 <= len(candidate) <= 300
                            and not any(ord(c) < 32 or ord(c) == 127 for c in candidate)
                            and "://" not in candidate and self.token not in candidate
                            and not re.search(r"token|secret|bearer|traceback", candidate, re.I)):
                        # Only sanitized PersonalError validation messages are exposed
                        # by the board. PlainText cannot interpret them as markup.
                        message = candidate
                except Exception:
                    pass
            exc.close()
            raise BoardError(retryable=exc.code >= 500, public_message=message) from None
        except BoardError:
            raise
        except Exception:
            # TLS, DNS, proxy, socket, and JSON errors are never exposed to speech/logs.
            raise BoardError() from None

    def context(self):
        context = self._request("/api/v1/voice-context")
        try:
            zone = context["timezone"]
            if not isinstance(zone, str) or not 1 <= len(zone) <= 100:
                raise ValueError
            ZoneInfo(zone)
            duration = context["appointment_duration_minutes"]
            if type(duration) is not int or not 1 <= duration <= 1440:
                raise ValueError
            start, end = context["dinner_start"], context["dinner_end"]
            _exact_time(start)
            _exact_time(end)
            if start >= end:
                raise ValueError
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
            raise BoardError(retryable=False) from None
        return {"timezone": zone, "appointment_duration_minutes": duration,
                "dinner_start": start, "dinner_end": end}

    def save(self, payload):
        result = self._request("/api/v1/personal-items", payload)
        if not (type(result.get("created")) is bool and isinstance(result.get("item"), dict)):
            raise BoardError()
        return result

    def start_timer(self, payload):
        result = self._request("/api/v1/timers", payload)
        if type(result.get("created")) is not bool:
            raise BoardError()
        _timer(result.get("timer"))
        return result

    def list_timers(self):
        result = self._request("/api/v1/timers")
        rows = result.get("timers")
        if not isinstance(rows, list) or len(rows) > 5:
            raise BoardError()
        for row in rows:
            _timer(row)
        if len({row["id"] for row in rows}) != len(rows):
            raise BoardError()
        return rows

    def cancel_timer(self, item_id):
        result = self._request("/api/v1/timers/cancel", {"id": item_id})
        if type(result.get("cancelled")) is not bool:
            raise BoardError()
        timer = _timer(result.get("timer"))
        if timer["id"] != item_id or not timer["cancelled"]:
            raise BoardError()
        return result

    def watch_flight(self, payload):
        result = self._request("/api/v1/flight-watches", payload)
        watch = result.get("watch")
        if (type(result.get("created")) is not bool or type(result.get("tracking_available")) is not bool
                or not isinstance(watch, dict)):
            raise BoardError()
        try:
            if not re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,4}", watch.get("ident", "")):
                raise ValueError
            _exact_date(watch.get("date"))
        except (ValueError, TypeError):
            raise BoardError() from None
        return result

    def shopping_list(self):
        return _shopping(self._request("/api/v1/shopping-list"))

    def update_shopping(self, action, payload):
        if action not in {"add", "remove", "clear", "show"}:
            raise BoardError(retryable=False)
        result = _shopping(self._request("/api/v1/shopping-list/" + action, payload))
        if type(result.get("changed")) is not bool:
            raise BoardError()
        return result


def _now():
    return datetime.now(timezone.utc)


def _text(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_TITLE
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("title")
    return value.strip()


def _exact_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("date")
    return date.fromisoformat(value).isoformat()


def _exact_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise ValueError("time")
    return time.fromisoformat(value).strftime("%H:%M")


def _duration(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("duration")
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if not match:
        raise ValueError("duration")
    days, hours, minutes, seconds = (Decimal(x or "0") for x in match.groups())
    total = days * 1440 + hours * 60 + minutes + seconds / 60
    if total != total.to_integral_value() or not 1 <= total <= 1440:
        raise ValueError("duration")
    return int(total)


def _timer_duration(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("duration")
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if not match:
        raise ValueError("duration")
    days, hours, minutes, seconds = (Decimal(x or "0") for x in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total != total.to_integral_value() or not 1 <= total <= 86400:
        raise ValueError("duration")
    return int(total)


def _timer_label(value):
    value = _text(value)
    if len(value) > 80 or any(0xD800 <= ord(c) <= 0xDFFF or c in "\u2028\u2029" for c in value):
        raise ValueError("label")
    return value


def _shopping(value):
    try:
        if not isinstance(value, dict) or type(value.get("visible")) is not bool:
            raise ValueError
        rows = value.get("items")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not 1 <= len(row["id"]) <= 80:
                raise ValueError
            _timer_label(row.get("name"))
            if row["id"] in ids:
                raise ValueError
            ids.add(row["id"])
        return value
    except (TypeError, ValueError):
        raise BoardError() from None


def _timer(value):
    try:
        if not isinstance(value, dict) or not re.fullmatch(r"[a-f0-9]{24}", value.get("id", "")):
            raise ValueError
        _timer_label(value.get("label"))
        if type(value.get("duration_seconds")) is not int or not 1 <= value["duration_seconds"] <= 86400:
            raise ValueError
        if type(value.get("remaining_seconds")) is not int or value["remaining_seconds"] < 0:
            raise ValueError
        if type(value.get("cancelled")) is not bool or value.get("status") not in {"running", "expired", "cancelled"}:
            raise ValueError
        ends = datetime.fromisoformat(value["ends_at"].replace("Z", "+00:00"))
        if ends.tzinfo is None or ends.utcoffset() is None:
            raise ValueError
        return value
    except (KeyError, ValueError, TypeError, AttributeError):
        raise BoardError() from None


def _duration_words(seconds):
    hours, remaining = divmod(seconds, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts = [f"{count} {unit}{'' if count == 1 else 's'}" for count, unit in
             ((hours, "hour"), (minutes, "minute"), (seconds, "second")) if count]
    return ", ".join(parts) or "zero seconds"


def _airline(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("airline")
    normalized = " ".join(value.casefold().replace(".", "").split())
    if normalized in AIRLINES:
        return AIRLINES[normalized]
    code = normalized.replace(" ", "").replace(".", "").upper()
    if re.fullmatch(r"(?:[A-Z0-9]{2}|[A-Z]{3})", code) and re.search(r"[A-Z]", code):
        return code
    raise ValueError("airline")


def _flight_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}", value) or int(value) == 0:
        raise ValueError("flight number")
    return value


def _request_id(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_REQUEST_ID
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise ValueError("request id")
    return value


def _speech(text, pending=None, *, end=False):
    # PlainText deliberately avoids interpreting user titles as SSML/XML markup.
    response = {"outputSpeech": {"type": "PlainText", "text": text}, "shouldEndSession": end}
    if not end:
        response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": text}}
    return {"version": "1.0", "sessionAttributes": {"pending": pending} if pending and not end else {},
            "response": response}


def _pending(event):
    value = event.get("session", {}).get("attributes", {}).get("pending")
    if not isinstance(value, dict) or set(value) - PENDING_FIELDS:
        return None
    if value.get("kind") not in ("appointment", "dinner", "timer", "flight_watch", "shopping"):
        return None
    if len(json.dumps(value)) > 4096:
        return None
    if any(not isinstance(item, (str, int, type(None))) for item in value.values()):
        return None
    try:
        _request_id(value.get("request_id"))
    except ValueError:
        return None
    return dict(value)


def _slots(intent):
    result = {}
    slots = intent.get("slots", {})
    if not isinstance(slots, dict):
        return result
    for name in ("Title", "Meal", "Date", "Time", "Duration", "TimerLabel", "TimerDuration",
                 "TimerNumber", "Airline", "FlightNumber", "Item"):
        slot = slots.get(name)
        if isinstance(slot, dict) and slot.get("value") is not None:
            result[name] = slot["value"]
    return result


def _new_pending(kind, request, context):
    return {"kind": kind, "request_id": _request_id(request.get("requestId")),
            "duration_minutes": context["appointment_duration_minutes"],
            "timezone": context["timezone"], "phase": "collect"}


def _human_date(value):
    day = date.fromisoformat(value)
    return f"{day.strftime('%A, %B')} {day.day}, {day.year}"


def _human_time(value):
    hour, minute = map(int, value.split(":"))
    suffix = "A.M." if hour < 12 else "P.M."
    return f"{hour % 12 or 12}{(':' + str(minute).zfill(2)) if minute else ''} {suffix}"


def _local_datetime(day, clock, zone):
    naive = datetime.combine(date.fromisoformat(day), time.fromisoformat(clock))
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive:
            if not any(other.utcoffset() == candidate.utcoffset() for other in candidates):
                candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError("The local time is missing or repeated because the clocks change.")
    return candidates[0]


def _confirm(pending):
    pending["phase"] = "confirm"
    return _speech(f"Add {pending['title']} on {_human_date(pending['date'])}, at "
                   f"{_human_time(pending['time'])}, for {pending['duration_minutes']} minutes, "
                   f"in the board's {pending['timezone'].replace('_', ' ').replace('/', ' ')} time zone. "
                   "Should I save it? Say yes or no.", pending)


def _continue_appointment(pending, context):
    pending["phase"] = "collect"
    if not pending.get("title"):
        return _speech("What is the appointment called? Say, it's called, followed by the name.", pending)
    if not pending.get("date"):
        return _speech("What day is the appointment? Say, on Friday, or give the exact date.", pending)
    if not pending.get("time"):
        return _speech("What exact time is the appointment? Say, at three P.M., including A.M. or P.M.", pending)
    if type(pending.get("duration_minutes")) is not int or not 1 <= pending["duration_minutes"] <= 1440:
        return _speech("How long is the appointment? Say, for one hour, or give a duration in minutes.", pending)
    try:
        _text(pending["title"])
        _exact_date(pending["date"])
        _exact_time(pending["time"])
        zone = ZoneInfo(context["timezone"])
        _local_datetime(pending["date"], "00:00", zone)
        start = _local_datetime(pending["date"], pending["time"], zone)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        pending["time"] = None
        return _speech("That date or time is incomplete or affected by a clock change. "
                       "Please give an exact unambiguous date and time, such as on Friday at three P.M.", pending)
    if start.astimezone(timezone.utc) + timedelta(minutes=pending["duration_minutes"]) <= _now():
        pending["date"] = None
        return _speech("That appointment would already have ended. What future date should I use? Say, on Friday.", pending)
    pending["timezone"] = context["timezone"]
    return _confirm(pending)


def _save(client, pending, context):
    payload = {key: pending[key] for key in ("kind", "title", "date", "request_id")}
    payload["source"] = "alexa"
    if pending["kind"] == "appointment":
        payload.update(time=pending["time"], duration_minutes=pending["duration_minutes"])
    try:
        result = client.save(payload)
    except BoardError as exc:
        if not exc.retryable:
            if exc.public_message:
                return _speech("The board could not accept that entry. " + exc.public_message, end=True)
            return _speech("The board could not accept that entry. Please check the Alexa connection "
                           "and board settings, then try again.", end=True)
        pending["phase"] = "retry"
        return _speech("I could not confirm whether the board saved that. Say yes to retry this same entry, "
                       "or no to stop and check the board. Retrying this entry will not create a duplicate.", pending)
    item = result.get("item", {})
    hidden = " Personal cards are currently hidden on the display." if result.get("display_enabled") is False else ""
    if item.get("cancelled") is True:
        return _speech("That entry was already replaced or cancelled. I have not restored it.", end=True)
    saved = "Already saved" if result.get("created") is False else "Saved"
    duplicate = " No duplicate was added." if result.get("created") is False else ""
    try:
        title = _text(item["title"])
        zone = ZoneInfo(item["timezone"])
        stamps = []
        for field in ("start", "end", "display_start", "display_end"):
            value = datetime.fromisoformat(item[field].replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError
            stamps.append(value)
        start, end, display_start, display_end = stamps
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        actual_day = local_start.date().isoformat()
        if pending["kind"] == "dinner":
            if display_end <= _now():
                return _speech(f"{saved} {title} for dinner on {_human_date(actual_day)}. "
                               "Its display window has already ended, so it will not appear. "
                               "I have not moved it to tomorrow." + duplicate + hidden, end=True)
            return _speech(f"{saved} {title} for dinner on {_human_date(actual_day)}. "
                           f"Its display window is from {_human_time(display_start.astimezone(zone).strftime('%H:%M'))} "
                           f"to {_human_time(display_end.astimezone(zone).strftime('%H:%M'))}, "
                           "in the board's time zone." + duplicate + hidden, end=True)
        minutes = int((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 60)
        until = _human_time(local_end.strftime("%H:%M"))
        if local_end.date() != local_start.date():
            until += " on " + _human_date(local_end.date().isoformat())
        return _speech(f"{saved} {title} on {_human_date(actual_day)}, at "
                       f"{_human_time(local_start.strftime('%H:%M'))}, for {minutes} minutes. "
                       f"Its display window is from midnight that day until {until}, when the appointment ends."
                       + duplicate + hidden, end=True)
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return _speech(f"{saved} the entry. Check the board for its actual display window." + duplicate + hidden, end=True)


def _authorized(event):
    expected = os.environ.get("ALEXA_SKILL_ID", "")
    if not re.fullmatch(r"amzn1\.ask\.skill\.[A-Za-z0-9._-]{1,220}", expected):
        return False
    identifiers = []
    context_id = event.get("context", {}).get("System", {}).get("application", {}).get("applicationId")
    session_id = event.get("session", {}).get("application", {}).get("applicationId")
    for value in (context_id, session_id):
        if value is not None:
            if not isinstance(value, str):
                return False
            identifiers.append(value)
    return bool(identifiers) and all(hmac.compare_digest(value, expected) for value in identifiers)


def _action_error(error, pending):
    if not error.retryable:
        message = error.public_message or "Check the board settings, then try again."
        return _speech("The board could not accept that request. " + message, end=True)
    pending["phase"] = "retry"
    return _speech("I could not confirm whether the board saved that. Say yes to retry this same request, "
                   "or no to stop and check the board. A retry will not apply the same request twice.", pending)


def _save_timer(client, pending):
    label = _timer_label(pending.get("label"))
    seconds = pending.get("duration_seconds")
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise ValueError("duration")
    payload = {"label": label, "duration_seconds": seconds, "source": "alexa",
               "request_id": _request_id(pending.get("request_id"))}
    try:
        result = client.start_timer(payload)
        timer = _timer(result.get("timer"))
    except BoardError as exc:
        return _action_error(exc, pending)
    if timer["cancelled"]:
        return _speech("That timer was already cancelled. I have not restarted it.", end=True)
    if timer["status"] == "expired":
        return _speech(f"The {timer['label']} timer has already finished. I have not restarted it.", end=True)
    if result.get("created") is False:
        return _speech(f"The {timer['label']} timer is already running, with "
                       f"{_duration_words(timer['remaining_seconds'])} remaining. No new timer was started.", end=True)
    return _speech(f"Started the {timer['label']} timer for {_duration_words(timer['duration_seconds'])} "
                   "on Flight Wall.", end=True)


def _timer_dialog(client, name, values, pending, request):
    if not pending or pending["kind"] != "timer" or pending.get("phase") == "retry":
        pending = {"kind": "timer", "phase": "collect", "request_id": _request_id(request.get("requestId"))}
    for slot, field, validator in (("TimerLabel", "label", _timer_label),
                                    ("TimerDuration", "duration_seconds", _timer_duration)):
        if slot in values:
            try:
                pending[field] = validator(values[slot])
            except (ValueError, ArithmeticError):
                pending[field] = None
    if not pending.get("label"):
        return _speech("What should I call the timer? Say, call the timer pasta.", pending)
    if not pending.get("duration_seconds"):
        return _speech("How long should it run? Say, for five minutes. Choose one second to twenty four hours.", pending)
    return _save_timer(client, pending)


def _list_timers(client):
    timers = client.list_timers()
    if not timers:
        return _speech("There are no Flight Wall timers.", end=True)
    entries = []
    for number, timer in enumerate(timers, 1):
        timer = _timer(timer)
        detail = "finished" if timer["status"] == "expired" else _duration_words(timer["remaining_seconds"]) + " remaining"
        entries.append(f"Timer {number}, {timer['label']}, {detail}.")
    return _speech(" ".join(entries), end=True)


def _cancel_timer(client, values):
    label, number = values.get("TimerLabel"), values.get("TimerNumber")
    if label is None and number is None:
        return _speech("Which Flight Wall timer? Say, cancel the pasta timer, or cancel timer number one.")
    timers = client.list_timers()
    if number is not None:
        if not isinstance(number, str) or not re.fullmatch(r"[1-5]", number) or int(number) > len(timers):
            return _speech("That timer number is not available. Say list timers to hear the current timers.")
        matches = [timers[int(number) - 1]]
        if label is not None and _timer_label(label).casefold() != matches[0]["label"].casefold():
            return _speech("The timer name and number do not match. Say list timers to check.")
    else:
        normalized = _timer_label(label).casefold()
        matches = [timer for timer in timers if timer["label"].casefold() == normalized]
    if not matches:
        return _speech("No Flight Wall timer has that label. Say list timers to hear the current timers.")
    if len(matches) != 1:
        return _speech("More than one timer has that label. Say list timers, then cancel timer number one, or another number.")
    timer = _timer(matches[0])
    try:
        client.cancel_timer(timer["id"])
    except BoardError:
        return _speech("I could not confirm that cancellation. Say list timers to check before trying again.", end=True)
    return _speech(f"Cancelled the {timer['label']} timer on Flight Wall.", end=True)


def _confirm_flight(pending):
    pending["phase"] = "confirm"
    return _speech(f"Add {pending['airline']} flight {pending['flight_number']} on {_human_date(pending['date'])} "
                   "to the flight board? Say yes or no.", pending)


def _flight_dialog(values, pending, request):
    if not pending or pending["kind"] != "flight_watch" or pending.get("phase") == "retry":
        pending = {"kind": "flight_watch", "phase": "collect", "request_id": _request_id(request.get("requestId"))}
    for slot, field, validator in (("Airline", "airline", _airline), ("FlightNumber", "flight_number", _flight_number),
                                    ("Date", "date", _exact_date)):
        if slot in values:
            try:
                pending[field] = validator(values[slot])
            except (ValueError, TypeError):
                pending[field] = None
    if not pending.get("airline"):
        return _speech("Which airline? Say, the airline is Delta, American, United, Allegiant, Frontier, or an airline code.", pending)
    if not pending.get("flight_number"):
        return _speech("What is the flight number? Say, the flight number is one hundred twenty three.", pending)
    if not pending.get("date"):
        return _speech("What date is that flight? Say, on Friday, or give the exact date.", pending)
    return _confirm_flight(pending)


def _save_flight(client, pending):
    payload = {"airline": _airline(pending.get("airline")), "flight_number": _flight_number(pending.get("flight_number")),
               "date": _exact_date(pending.get("date")), "source": "alexa", "request_id": _request_id(pending.get("request_id"))}
    try:
        result = client.watch_flight(payload)
    except BoardError as exc:
        return _action_error(exc, pending)
    watch = result["watch"]
    saved = "Added" if result["created"] else "Already added"
    availability = (" Flight data will appear when available." if result["tracking_available"] else
                    " Live flight data is not configured.")
    return _speech(f"{saved} {watch['ident']} on {_human_date(watch['date'])} to the flight board." + availability, end=True)


def _shopping_words(result):
    rows = result["items"]
    if not rows:
        return "Your Flight Wall shopping list is empty."
    names = ", ".join(row["name"] for row in rows[:10])
    more = f", and {len(rows) - 10} more items on the board" if len(rows) > 10 else ""
    return "Shopping list: " + names + more + "."


def _confirm_shopping(pending):
    pending["phase"] = "confirm"
    return _speech("Clear the entire Flight Wall shopping list? Say yes to clear it, or no to keep it.", pending)


def _save_shopping(client, pending):
    action = pending.get("action")
    if action not in {"add", "remove", "clear", "show"}:
        raise ValueError("shopping action")
    payload = {"request_id": _request_id(pending.get("request_id"))}
    if action in {"add", "remove"}:
        payload.update(item=_timer_label(pending.get("item")), source="alexa")
    elif action == "clear":
        if pending.get("phase") not in {"confirm", "retry"}:
            return _confirm_shopping(pending)
        payload["confirmed"] = True
    else:
        payload["visible"] = True
    try:
        result = _shopping(client.update_shopping(action, payload))
        names = {" ".join(row["name"].casefold().split()) for row in result["items"]}
        if (action == "clear" and result["items"] or action == "show" and not result["visible"]
                or action == "add" and " ".join(payload["item"].casefold().split()) not in names
                or action == "remove" and " ".join(payload["item"].casefold().split()) in names):
            raise BoardError()
    except BoardError as exc:
        return _action_error(exc, pending)
    if action == "show":
        return _speech("Showing the shopping list on Flight Wall. " + _shopping_words(result), end=True)
    if action == "clear":
        return _speech("The Flight Wall shopping list is clear.", end=True)
    if action == "add":
        message = f"Added {payload['item']} to the shopping list." if result["changed"] else f"{payload['item']} is already on the shopping list."
    else:
        message = f"Removed {payload['item']} from the shopping list." if result["changed"] else f"{payload['item']} is not on the shopping list."
    return _speech(message, end=True)


def _shopping_dialog(client, name, values, pending, request):
    if name == "ReadShoppingListIntent":
        return _speech(_shopping_words(client.shopping_list()), end=True)
    action = {"ShowShoppingListIntent": "show", "AddShoppingItemIntent": "add",
              "RemoveShoppingItemIntent": "remove", "ClearShoppingListIntent": "clear"}[name]
    if (not pending or pending["kind"] != "shopping" or pending.get("action") != action
            or pending.get("phase") in {"retry", "confirm"}):
        pending = {"kind": "shopping", "action": action, "phase": "collect",
                   "request_id": _request_id(request.get("requestId"))}
    if action == "clear":
        return _confirm_shopping(pending)
    if action in {"add", "remove"}:
        try:
            pending["item"] = _timer_label(values.get("Item"))
        except ValueError:
            pending["item"] = None
            return _speech(f"What item should I {action}? Say, {action} olives. Use a name up to eighty characters.", pending)
    return _save_shopping(client, pending)


def lambda_handler(event, lambda_context):
    """ASK Lambda entry point. Credentials and arbitrary errors never enter speech."""
    try:
        if not isinstance(event, dict) or not _authorized(event):
            return _speech("This request is not authorized for this Flight Wall skill.", end=True)
        request = event.get("request", {})
        if request.get("locale", "en-US") != "en-US":
            return _speech("This Flight Wall voice model is configured for U.S. English.", end=True)
        request_type = request.get("type")
        if request_type == "SessionEndedRequest":
            return {"version": "1.0", "response": {}}
        if request_type == "LaunchRequest":
            return _speech(WELCOME)
        if request_type != "IntentRequest":
            return _speech(WELCOME)
        intent = request.get("intent", {})
        name = intent.get("name")
        pending = _pending(event)
        if name in ("AMAZON.CancelIntent", "AMAZON.StopIntent", "AMAZON.NoIntent"):
            if pending and pending.get("phase") == "retry":
                return _speech("Okay, I will not retry. Please check the board, because the earlier save may have completed.", end=True)
            return _speech("Okay, cancelled. I did not save an entry.", end=True)
        if name == "AMAZON.HelpIntent":
            return _speech(HELP, pending)
        if name == "DinnerHelpIntent":
            return _speech(DINNER_HELP, pending)
        if name == "AMAZON.FallbackIntent":
            return _speech("I did not catch that. What would you like? Say help for options.", pending)
        if name == "AMAZON.RepeatIntent":
            if pending and pending.get("phase") == "confirm":
                if pending["kind"] == "shopping":
                    return _confirm_shopping(pending)
                return _confirm_flight(pending) if pending["kind"] == "flight_watch" else _confirm(pending)
            return _speech(WELCOME, pending)
        if name == "AMAZON.YesIntent" and not pending:
            return _speech("There is no request waiting for confirmation. What would you like?")
        client = BoardClient()
        values = _slots(intent)
        if name in {"ShowShoppingListIntent", "ReadShoppingListIntent", "AddShoppingItemIntent",
                    "RemoveShoppingItemIntent", "ClearShoppingListIntent"}:
            return _shopping_dialog(client, name, values, pending, request)
        if name == "ListTimersIntent":
            return _list_timers(client)
        if name == "CancelTimerIntent":
            return _cancel_timer(client, values)
        if name in ("StartTimerIntent", "SetTimerLabelIntent", "SetTimerDurationIntent") or (
                name == "SetDurationIntent" and pending and pending["kind"] == "timer"):
            if "Duration" in values:
                values["TimerDuration"] = values["Duration"]
            return _timer_dialog(client, name, values, pending, request)
        if name in ("TrackFlightIntent", "SetFlightAirlineIntent", "SetFlightNumberIntent") or (
                name == "SetDateIntent" and pending and pending["kind"] == "flight_watch"):
            return _flight_dialog(values, pending, request)
        if name == "AMAZON.YesIntent" and pending["kind"] in ("timer", "flight_watch", "shopping"):
            if pending["kind"] == "shopping":
                if pending.get("phase") in {"confirm", "retry"}:
                    return _save_shopping(client, pending)
                return _speech("Please give the shopping-list item first, or say no to stop.", pending)
            if pending["kind"] == "timer":
                if pending.get("phase") != "retry":
                    return _timer_dialog(client, name, {}, pending, request)
                return _save_timer(client, pending)
            if pending.get("phase") not in ("confirm", "retry"):
                return _flight_dialog({}, pending, request)
            return _save_flight(client, pending)
        context = client.context()
        if name == "AMAZON.YesIntent":
            if pending["kind"] == "appointment":
                previous_phase, previous_zone = pending.get("phase"), pending.get("timezone")
                validation = _continue_appointment(pending, context)
                if pending.get("phase") != "confirm" or previous_phase not in ("confirm", "retry"):
                    return validation
                if previous_zone != context["timezone"]:
                    return _speech("The board's time zone changed. " + validation["response"]["outputSpeech"]["text"], pending)
            elif pending.get("phase") != "retry":
                return _speech("Tell me the meal first. Say, we are having chicken for dinner.", pending)
            _text(pending["title"])
            _exact_date(pending["date"])
            _request_id(pending["request_id"])
            return _save(client, pending, context)
        if name == "AddDinnerIntent":
            if not pending or pending["kind"] != "dinner" or pending.get("phase") == "retry":
                pending = _new_pending("dinner", request, context)
            pending["timezone"] = context["timezone"]
            if "Date" in values:
                try:
                    pending["date"] = _exact_date(values["Date"])
                except ValueError:
                    pending["date"] = None
                    return _speech("Which exact day is dinner for? Say, set dinner for tomorrow, or give the exact date.", pending)
            if "Meal" in values:
                try:
                    pending["title"] = _text(values["Meal"])
                except ValueError:
                    return _speech("Please give a dinner name of one to 160 characters. Say, dinner is chicken and rice.", pending)
            if not pending.get("title"):
                return _speech("What is for dinner? Say, we are having chicken and rice for dinner.", pending)
            if "date" not in pending:
                pending["date"] = _now().astimezone(ZoneInfo(context["timezone"])).date().isoformat()
            if not pending.get("date"):
                return _speech("Which exact day is dinner for? Say, set dinner for tomorrow.", pending)
            return _save(client, pending, context)
        if name in ("AddAppointmentIntent", "SetAppointmentTitleIntent"):
            if not pending or pending["kind"] != "appointment" or pending.get("phase") == "retry":
                pending = _new_pending("appointment", request, context)
        elif name in ("SetDateIntent", "SetTimeIntent", "SetDurationIntent"):
            if not pending or pending["kind"] != "appointment":
                return _speech("First say, add an appointment, or add an appointment called, followed by its name.")
        else:
            return _speech("What would you like? Say help for options.", pending)
        for key, validator in (("Title", _text), ("Date", _exact_date), ("Time", _exact_time), ("Duration", _duration)):
            if key not in values:
                continue
            field = {"Title": "title", "Date": "date", "Time": "time", "Duration": "duration_minutes"}[key]
            try:
                pending[field] = validator(values[key])
            except ValueError:
                pending[field] = None
        return _continue_appointment(pending, context)
    except BoardError:
        return _speech("I could not connect to your board. Check that it is online and its Alexa connection is configured, then try again.", end=True)
    except Exception:
        # Do not log the exception or event: appointment titles can be private.
        return _speech("I could not understand that request. Please try again, or say help for options.", end=True)
