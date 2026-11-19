"""Read explicit, passenger-matched FlightReservation markup from a booking email.

The caller checks consent, sender, subject, message age and MIME structure
before requesting the body. Markup is data only: never resolve linked objects,
fetch attachments, or infer airport pairs from unstructured email text.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from html.parser import HTMLParser
import json
import re

from .delta_travel import DeltaReview, MAX_LEGS, MAX_TEXT, REVIEW, _bodies, _local_clock, owner_names
from .mycrew import airport_zone


_SCHEMA = re.compile(r"https?://schema\.org/([A-Za-z]+)")
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_FIELDS = frozenset(("reservationNumber", "reservationStatus", "underName", "name",
                     "givenName", "additionalName", "familyName", "reservationFor",
                     "airline", "iataCode", "flightNumber", "departureAirport",
                     "arrivalAirport", "departureTime", "arrivalTime"))


def _term(value):
    if not isinstance(value, str):
        return None
    match = _SCHEMA.fullmatch(value)
    return match[1] if match else value


def _put(target, prop, value):
    if prop not in target:
        target[prop] = value
    elif isinstance(target[prop], list):
        target[prop].append(value)
    else:
        target[prop] = [target[prop], value]


class _Markup(HTMLParser):
    """Read inline JSON-LD and Microdata inside a FlightReservation scope."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.json_documents = []
        self.microdata = []
        self.script = None
        self.frames = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "script" and (attrs.get("type") or "").lower().strip() == "application/ld+json":
            self.script = []
        parent = self.frames[-1][1] if self.frames else None
        prop = attrs.get("itemprop")
        kind = attrs.get("itemtype") or ""
        if "itemscope" in attrs:
            if parent is None:
                if not _SCHEMA.fullmatch(kind) or _term(kind) != "FlightReservation":
                    return
                scope = {"@type": "FlightReservation"}
                self.microdata.append(scope)
            else:
                scope = {"@type": _term(kind)}
                if prop in _FIELDS:
                    _put(parent, prop, scope)
            prop = None
        elif parent is None:
            return
        else:
            scope = parent
        if len(self.frames) > 64 or len(self.microdata) > MAX_LEGS * 8:
            raise DeltaReview(REVIEW)
        raw = next((attrs[key] for key in ("content", "datetime", "href") if key in attrs), None)
        if prop in _FIELDS and raw is not None:
            _put(scope, prop, raw)
            prop = None
        if tag not in _VOID:
            self.frames.append((tag, scope, prop if prop in _FIELDS else None, []))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif self.frames:
            for _, scope, prop, chunks in reversed(self.frames):
                if scope is not self.frames[-1][1]:
                    break
                if prop is not None:
                    chunks.append(data)
                    break

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.json_documents.append("".join(self.script))
            self.script = None
        if self.frames and self.frames[-1][0] == tag:
            _, scope, prop, chunks = self.frames.pop()
            if prop is not None:
                _put(scope, prop, " ".join("".join(chunks).split()))


def _object(pairs):
    # Duplicate JSON keys can otherwise replace an earlier passenger or flight.
    result = dict(pairs)
    if len(result) != len(pairs):
        raise DeltaReview(REVIEW)
    return result


def _schema_nodes(value, context=None, depth=0):
    if depth > 12:
        raise DeltaReview(REVIEW)
    if isinstance(value, list):
        for node in value:
            yield from _schema_nodes(node, context, depth + 1)
    elif isinstance(value, dict):
        context = value.get("@context", context)
        if (isinstance(context, str) and context.rstrip("/") in
                {"http://schema.org", "https://schema.org"}
                and _term(value.get("@type")) == "FlightReservation"):
            yield value
        if "@graph" in value:
            yield from _schema_nodes(value["@graph"], context, depth + 1)


def _text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise DeltaReview(REVIEW)
    return value.strip()


def _typed(value, name):
    if not isinstance(value, dict) or _term(value.get("@type")) != name:
        raise DeltaReview(REVIEW)
    return value


def _passenger(person):
    person = _typed(person, "Person")
    names = []
    if "name" in person:
        names.append(_text(person["name"], 120))
    if "givenName" in person or "familyName" in person:
        parts = [_text(person[key], 120) for key in ("givenName", "additionalName", "familyName") if key in person]
        if not {"givenName", "familyName"} <= person.keys():
            raise DeltaReview(REVIEW)
        names.append(" ".join(parts))
    candidates = owner_names(names)
    if len(candidates) != 1:
        raise DeltaReview(REVIEW)
    return candidates[0]


def _station(value):
    code = _text(_typed(value, "Airport").get("iataCode"), 3).upper()
    if not re.fullmatch(r"[A-Z]{3}", code) or not airport_zone(code):
        raise DeltaReview(REVIEW)
    return code


def _instant(value, station):
    value = _text(value, 32)
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)?", value):
        raise DeltaReview(REVIEW)
    clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if clock.tzinfo is None:
        instant = _local_clock(clock.year, clock.month, clock.day, clock.hour, clock.minute, airport_zone(station))
        return instant + timedelta(seconds=clock.second)
    return clock.astimezone(timezone.utc)


def _flight(flight, received):
    flight = _typed(flight, "Flight")
    carrier = _text(_typed(flight.get("airline"), "Airline").get("iataCode"), 2).upper()
    if not re.fullmatch(r"[A-Z0-9]{2}", carrier) or carrier.isdigit():
        raise DeltaReview(REVIEW)
    number = _text(flight.get("flightNumber"), 12).upper()
    match = re.fullmatch(r"(?:" + carrier + r"\s*)?(\d{1,4})", number, re.ASCII)
    if not match or int(match[1]) == 0:
        raise DeltaReview(REVIEW)
    origin = _station(flight.get("departureAirport"))
    destination = _station(flight.get("arrivalAirport"))
    departure = _instant(flight.get("departureTime"), origin)
    arrival = _instant(flight.get("arrivalTime"), destination)
    if (origin == destination or not timedelta(0) < arrival - departure <= timedelta(days=1)
            or not received - timedelta(days=1) <= departure <= received + timedelta(days=366)):
        raise DeltaReview(REVIEW)
    return {"number": carrier + str(int(match[1])), "origin": origin, "destination": destination,
            "start": departure.isoformat(), "end": arrival.isoformat(), "status": "confirmed",
            "source": "american_email" if carrier == "AA" else "airline_email",
            "updated_at": received.isoformat(), "traveler": "lisa"}


def _itinerary(nodes, aliases, received, allow_cancellation):
    locator = None
    records = {}
    if len(nodes) > MAX_LEGS * 8:
        raise DeltaReview(REVIEW)
    for node in nodes:
        people = node.get("underName")
        people = people if isinstance(people, list) else [people]
        if not 1 <= len(people) <= 16:
            raise DeltaReview(REVIEW)
        if not aliases.intersection(_passenger(person) for person in people):
            continue
        status = _term(node.get("reservationStatus"))
        cancelled = status in {"ReservationCancelled", "ReservationCanceled"}
        if status != "ReservationConfirmed" and not (allow_cancellation and cancelled):
            raise DeltaReview(REVIEW)
        code = _text(node.get("reservationNumber"), 32).upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,31}", code) or locator not in {None, code}:
            raise DeltaReview(REVIEW)
        locator = code
        row = _flight(node.get("reservationFor"), received)
        if cancelled:
            row["status"] = "cancelled"
        identity = row["number"], row["origin"], row["destination"], row["start"]
        if identity in records and records[identity] != row:
            raise DeltaReview(REVIEW)
        records[identity] = row
    if not 1 <= len(records) <= MAX_LEGS:
        raise DeltaReview(REVIEW)
    return locator, sorted(records.values(), key=lambda row: (row["start"], row["number"]))


def parse_airline(payload, lisa_names, received, booking_salt, *, issuer="", allow_cancellation=False):
    """Extract explicit passenger, booking and leg states from verified markup."""
    if (not isinstance(received, datetime) or received.tzinfo is None or received.utcoffset() is None
            or not isinstance(booking_salt, bytes) or len(booking_salt) != 32):
        raise DeltaReview(REVIEW)
    aliases = set(owner_names(list(lisa_names)))
    bodies = _bodies(payload)
    try:
        markup = _Markup()
        markup.feed(bodies.get("text/html", ""))
        markup.close()
        if markup.frames or markup.script is not None:
            raise DeltaReview(REVIEW)
        nodes = [node for document in markup.json_documents
                 for node in _schema_nodes(json.loads(document, object_pairs_hook=_object))]
        candidates = [_itinerary(group, aliases, received.astimezone(timezone.utc), allow_cancellation)
                      for group in (nodes, markup.microdata) if group]
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise DeltaReview(REVIEW) from None
    if not candidates or any(result != candidates[0] for result in candidates[1:]):
        raise DeltaReview(REVIEW)
    locator, records = candidates[0]
    # The same locator can be reused by unrelated airlines or agencies.
    key = hmac.new(booking_salt, ("airline-reservation-v2\0" + issuer + "\0" + locator).encode(), hashlib.sha256)
    return {"records": records, "booking_key": key.hexdigest()}


# The ticket template sometimes prints an agency station label instead of an
# IATA code. Accept verified spellings only; never geocode an ambiguous city.
_CWT_STATIONS = {"FT WALTON BEACH,FL": "VPS", "CHARLOTTE, NC": "CLT", "ST LOUIS/LAMB,MO": "STL"}
# Marketing names and IATA designators in US DOT airline reporting. Operated-by
# text can name a different airline and is deliberately outside this grammar.
_CWT_CARRIERS = {
    "ALASKA AIRLINES": "AS", "ALLEGIANT AIR": "G4", "AMERICAN AIRLINES": "AA",
    "AVELO AIRLINES": "XP", "BREEZE AIRWAYS": "MX", "DELTA AIR LINES": "DL",
    "FRONTIER AIRLINES": "F9", "HAWAIIAN AIRLINES": "HA", "JETBLUE AIRWAYS": "B6",
    "SOUTHWEST AIRLINES": "WN", "SPIRIT AIRLINES": "NK", "SUN COUNTRY AIRLINES": "SY",
    "UNITED AIRLINES": "UA",
}
_CWT_DATE = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[ ]+\d{1,2},[ ]+\d{4}"
_CWT_SEGMENT = re.compile(r"^[ \t]*Flight[ \t]+(.+?)[ \t]+([0-9]{1,4})[ \t]*$", re.M)


def _cwt_carrier(label):
    label = " ".join(label.upper().split())
    if label in _CWT_CARRIERS:
        return _CWT_CARRIERS[label]
    if re.fullmatch(r"[A-Z0-9]{2}", label, re.ASCII) and not label.isdigit():
        return label
    named = re.fullmatch(r"([A-Z ]+) \(([A-Z0-9]{2})\)", label, re.ASCII)
    if named and _CWT_CARRIERS.get(named[1]) == named[2]:
        return named[2]
    raise DeltaReview(REVIEW)


def _cwt_station(label):
    explicit = re.fullmatch(r"(?:[^()]{1,80} *\(([A-Z]{3})\)|([A-Z]{3}))", label)
    if explicit:
        return explicit[1] or explicit[2]
    if label not in _CWT_STATIONS:
        raise DeltaReview(REVIEW)
    return _CWT_STATIONS[label]


def _cwt_clock(value):
    if not re.fullmatch(r"(?:1[0-2]|0?[1-9]):[0-5]\d [AP]M, " + _CWT_DATE, value, re.ASCII):
        raise DeltaReview(REVIEW)
    return datetime.strptime(value, "%I:%M %p, %b %d, %Y").isoformat(timespec="minutes")


def _cwt_leg(carrier, number, block, received, allow_cancellation):
    status = re.findall(r"^[ \t]*Status[ \t]+([^\r\n]+)$", block, re.M)
    label = status[0].strip().casefold() if len(status) == 1 else ""
    cancelled = label in {"cancelled", "canceled"}
    if label != "confirmed" and not (allow_cancellation and cancelled):
        raise DeltaReview(REVIEW)
    pairs = re.findall(r"^[ \t]*DEPARTURE[ \t]+ARRIVAL[ \t]*\n[ \t]*([^\n]+)\n[ \t]*([^\n]+)",
                       block, re.M)
    if len(pairs) != 1:
        raise DeltaReview(REVIEW)
    stations, clocks = (re.split(r"[ \t]{2,}", line.strip()) for line in pairs[0])
    if len(stations) != 2 or len(clocks) != 2:
        raise DeltaReview(REVIEW)
    origin, destination = map(_cwt_station, stations)
    depart, arrive = map(_cwt_clock, clocks)
    return _flight({"@type": "Flight", "airline": {"@type": "Airline", "iataCode": _cwt_carrier(carrier)},
                    "flightNumber": number, "departureAirport": {"@type": "Airport", "iataCode": origin},
                    "arrivalAirport": {"@type": "Airport", "iataCode": destination},
                    "departureTime": depart, "arrivalTime": arrive}, received) | {
                        "status": "cancelled" if cancelled else "confirmed"}


def parse_cwt_invoice(text, lisa_names, received, booking_salt, *, allow_cancellation=False):
    """Read only the verified columns of an authenticated CWT issued-ticket PDF.

    Input must retain the original columns. The caller verifies sender,
    attachment and decoding limits and discards the extracted text after use.
    """
    if (not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT
            or not isinstance(received, datetime) or received.utcoffset() is None
            or not isinstance(booking_salt, bytes) or len(booking_salt) != 32):
        raise DeltaReview(REVIEW)
    text = text.replace("\r\n", "\n").replace("\f", "\n")
    if "Invoice / Ticket / Date" not in text:
        raise DeltaReview(REVIEW)
    locators = set(re.findall(r"\bLocator:[ \t]*([A-Z0-9]{6})[ \t]+Date:[ \t]*" + _CWT_DATE + r"\b", text))
    names = re.findall(r"^[ \t]*Traveler[ \t]{2,}([^\n]+)$", text, re.M)
    if len(locators) != 1 or not names or len(names) > MAX_LEGS:
        raise DeltaReview(REVIEW)
    aliases = set(owner_names(list(lisa_names)))
    if any(owner_names([name.strip()])[0] not in aliases for name in names):
        raise DeltaReview(REVIEW)
    segments = list(_CWT_SEGMENT.finditer(text))
    if not 1 <= len(segments) <= MAX_LEGS:
        raise DeltaReview(REVIEW)
    records = []
    try:
        for position, match in enumerate(segments):
            end = segments[position + 1].start() if position + 1 < len(segments) else len(text)
            records.append(_cwt_leg(match[1], match[2], text[match.end():end],
                                    received.astimezone(timezone.utc), allow_cancellation))
    except (ValueError, TypeError, OverflowError):
        raise DeltaReview(REVIEW) from None
    if len({(row["number"], row["start"], row["origin"], row["destination"]) for row in records}) != len(records):
        raise DeltaReview(REVIEW)
    key = hmac.new(booking_salt, ("cwt-ticket-v1\0" + locators.pop()).encode(), hashlib.sha256)
    return {"booking_key": key.hexdigest(), "records": sorted(records, key=lambda row: (row["start"], row["number"]))}
