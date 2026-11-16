"""Opt-in, Chris-only import of one verified Delta non-revenue email format.

The message payload, locator and passenger names exist only during parsing.
Only narrow flight records, a private booking hash and aggregate status persist.
No OpenAI calls, inbox mutations, attachments or Calendar writes are performed.
"""
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import getaddresses
import base64
import hashlib
import hmac
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import secrets
from urllib.parse import quote

from .config import private_json
from .messages import GmailClient, MessageError
from .mycrew import airport_zone
from .personal_travel import PersonalTravel


UTC = timezone.utc
SENDER = "no-reply@delta.com"
SUBJECT = "Non Revenue Listing"
MAX_MESSAGES = 60
MAX_TEXT = 131072
MAX_LEGS = 16
ERROR = "Delta travel could not be refreshed. Check Chris's separate Gmail connection."
REVIEW = "Some Delta messages need review; unsupported changes were not guessed."
_HEADERS = ("carrier", "flight", "from", "to", "departure time", "arrival time")
_CLOCK = re.compile(r"([0-3]?\d)([A-Za-z]{3})\s+([0-1]?\d):([0-5]\d)\s*(AM|PM)", re.I)
_ROW = re.compile(r"DL\s*([0-9]{1,4})\s*([A-Z]{3})\s*([A-Z]{3})\s*"
                  r"([0-3]?\d[A-Za-z]{3}\s+[0-1]?\d:[0-5]\d\s*(?:AM|PM))\s*"
                  r"([0-3]?\d[A-Za-z]{3}\s+[0-1]?\d:[0-5]\d\s*(?:AM|PM))")
_DASH_ROW = re.compile(r"DL\s*-\s*Flight\s+([0-9]{1,4})\s*:\s*([A-Z]{3})\s*-\s*([A-Z]{3})\s*"
                       r"Departure\s+Time\s*:\s*([0-3]?\d[A-Za-z]{3}\s+[0-1]?\d:[0-5]\d\s*(?:AM|PM))\s*/\s*"
                       r"Arrival\s+Time\s*:\s*([0-3]?\d[A-Za-z]{3}\s+[0-1]?\d:[0-5]\d\s*(?:AM|PM))")
_MONTHS = {name: index for index, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_STRUCTURE_FIELDS = "payload(mimeType,filename,parts(mimeType,filename,parts(mimeType,filename)))"


class DeltaReview(ValueError):
    """A fixed, nonsecret error for an unsupported or ambiguous message."""


def owner_names(value):
    """Validate the private exact-name aliases without inferring initials."""
    try:
        names = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise DeltaReview(REVIEW) from None
    if not isinstance(names, list) or not 1 <= len(names) <= 8:
        raise DeltaReview(REVIEW)
    result = set()
    for name in names:
        if (not isinstance(name, str) or not 3 <= len(name) <= 120
                or any(not (character.isalpha() or character in " .'-") for character in name)):
            raise DeltaReview(REVIEW)
        result.add(" ".join(name.upper().split()))
    return tuple(sorted(result))


def _normal(value):
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


class _Tables(HTMLParser):
    """Handle Delta's implied td/tr closures without executing HTML or URLs."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.tables, self.text = [], [], []

    def _cell(self):
        if self.stack and self.stack[-1]["cell"] is not None:
            current = self.stack[-1]
            current["row"].append(_normal(" ".join(current["cell"])))
            current["cell"] = None

    def _row(self):
        if self.stack:
            self._cell()
            current = self.stack[-1]
            if current["row"]:
                current["rows"].append(current["row"])
                current["row"] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if len(self.stack) >= 12:
                raise DeltaReview(REVIEW)
            self.stack.append({"rows": [], "row": [], "cell": None})
        elif tag == "tr":
            self._row()
        elif tag in {"td", "th"} and self.stack:
            self._cell()
            self.stack[-1]["cell"] = []
        elif tag in {"br", "div", "p"}:
            self.handle_data(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self._cell()
        elif tag == "tr":
            self._row()
        elif tag == "table" and self.stack:
            self._row()
            self.tables.append(self.stack.pop()["rows"])

    def handle_data(self, value):
        self.text.append(value)
        if self.stack and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"].append(value)


def _template(text):
    flat = _normal(text)
    compact = re.sub(r"\s+", "", flat).casefold()
    if ("thankyouforbookingnonrevenuelisting" not in compact
            or "flightinformation" not in compact or "passengerinformation" not in compact
            or re.search(r"\b(?:cancel(?:led|ed|lation)?|rebooked|schedule change|itinerary change)\b", flat, re.I)):
        raise DeltaReview(REVIEW)
    locators = re.findall(r"Record\s+Locator(?:\s*:\s*|\s+)([A-Z0-9]{6})(?![A-Z0-9])", flat)
    if len(locators) != 1:
        raise DeltaReview(REVIEW)
    return locators[0]


def _html_parts(text):
    parser = _Tables()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise DeltaReview(REVIEW)
    locator = _template(" ".join(parser.text))
    flights = [table[1:] for table in parser.tables if table and
               tuple(cell.casefold() for cell in table[0]) == _HEADERS]
    passengers = [table[1:] for table in parser.tables if table and
                  [cell.casefold() for cell in table[0]] == ["#", "name"]]
    if len(flights) != 1 or len(passengers) != 1:
        raise DeltaReview(REVIEW)
    names = []
    for index, row in enumerate(passengers[0], 1):
        if len(row) != 2 or row[0] != str(index):
            raise DeltaReview(REVIEW)
        names.append(row[1])
    return locator, flights[0], names


def _plain_parts(text):
    locator = _template(text)
    flight = re.split(r"(?:^|\r?\n)\s*Passenger\s+Information", text, flags=re.I)
    if len(flight) != 2:
        raise DeltaReview(REVIEW)
    header = re.split(r"Carrier\s*Flight\s*From\s*To\s*Departure\s*Time\s*Arrival\s*Time",
                      flight[0], flags=re.I)
    if len(header) == 2:
        remaining, pattern = header[1], _ROW
    elif len(header) == 1:
        heading = re.split(r"Flight\s+Information\s*\(\s*Record\s+Locator\s*:?\s*[A-Z0-9]{6}\s*\)\s*-\s*",
                           flight[0], flags=re.I)
        if len(heading) != 2:
            raise DeltaReview(REVIEW)
        remaining, pattern = heading[1], _DASH_ROW
    else:
        raise DeltaReview(REVIEW)
    rows = []
    while remaining.strip():
        match = pattern.match(remaining.lstrip())
        if not match:
            raise DeltaReview(REVIEW)
        rows.append(["DL", *match.groups()])
        remaining = remaining.lstrip()[match.end():]
    section = re.split(r"Additional\s+Comments", flight[1], flags=re.I)[0]
    section = re.sub(r"^\s*#\s*Name\s*", "", section, count=1, flags=re.I)
    flat, names, end = _normal(section), [], 0
    for index, match in enumerate(re.finditer(r"(?:^|\s)([1-9]\d?)\s+(?:-\s*)?(.+?)(?=(?:\s+[1-9]\d?\s+)|$)", flat), 1):
        if flat[end:match.start()].strip() or match[1] != str(index):
            raise DeltaReview(REVIEW)
        names.append(match[2])
        end = match.end()
    if flat[end:].strip():
        raise DeltaReview(REVIEW)
    return locator, rows, names


def _local_clock(year, month, day, hour, minute, zone):
    naive = datetime(year, month, day, hour, minute)
    candidates = {naive.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)
                  if naive.replace(tzinfo=zone, fold=fold).astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive}
    if len(candidates) != 1:
        raise DeltaReview(REVIEW)
    return candidates.pop()


def _clock(value):
    match = _CLOCK.fullmatch(value)
    if not match or match[2].upper() not in _MONTHS or not 1 <= int(match[3]) <= 12:
        raise DeltaReview(REVIEW)
    return (_MONTHS[match[2].upper()], int(match[1]),
            int(match[3]) % 12 + (12 if match[5].upper() == "PM" else 0), int(match[4]))


def _records(rows, received):
    if not 1 <= len(rows) <= MAX_LEGS:
        raise DeltaReview(REVIEW)
    result = []
    for row in rows:
        if (len(row) != 6 or row[0] != "DL" or not re.fullmatch(r"[0-9]{1,4}", row[1])
                or not 1 <= int(row[1]) <= 9999
                or not all(re.fullmatch(r"[A-Z]{3}", code) for code in row[2:4]) or row[2] == row[3]):
            raise DeltaReview(REVIEW)
        origin, destination = airport_zone(row[2]), airport_zone(row[3])
        if origin is None or destination is None:
            raise DeltaReview(REVIEW)
        depart_clock, arrive_clock = _clock(row[4]), _clock(row[5])
        local_year = received.astimezone(origin).year
        departures = []
        for year in (local_year - 1, local_year, local_year + 1):
            try:
                value = _local_clock(year, *depart_clock, origin)
            except (ValueError, OverflowError):
                continue
            if received - timedelta(days=1) <= value <= received + timedelta(days=330):
                departures.append(value)
        if len(departures) != 1:
            raise DeltaReview(REVIEW)
        departure = departures[0]
        arrivals = []
        for year in (departure.year - 1, departure.year, departure.year + 1):
            try:
                value = _local_clock(year, *arrive_clock, destination)
            except (ValueError, OverflowError):
                continue
            if timedelta(0) < value - departure <= timedelta(days=1):
                arrivals.append(value)
        if len(arrivals) != 1:
            raise DeltaReview(REVIEW)
        result.append({"number": "DL" + str(int(row[1])), "origin": row[2], "destination": row[3],
                       "start": departure.isoformat(), "end": arrivals[0].isoformat(),
                       "status": "listed", "source": "delta_email", "updated_at": received.isoformat()})
    if len({(row["number"], row["origin"], row["destination"], row["start"]) for row in result}) != len(result):
        raise DeltaReview(REVIEW)
    return result


def _bodies(payload):
    found, total, nodes = {}, 0, 0
    def visit(part, depth=0):
        nonlocal total, nodes
        nodes += 1
        if not isinstance(part, dict) or depth > 5 or nodes > 48:
            raise DeltaReview(REVIEW)
        if part.get("filename"):
            return
        mime = part.get("mimeType")
        if mime in {"text/plain", "text/html"}:
            body = part.get("body", {})
            if not isinstance(body, dict):
                raise DeltaReview(REVIEW)
            data, size = body.get("data"), body.get("size")
            if (not isinstance(data, str) or len(data) > MAX_TEXT * 2
                    or type(size) is not int or not 0 < size <= MAX_TEXT):
                raise DeltaReview(REVIEW)
            try:
                raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
                text = raw.decode("utf-8")
            except (ValueError, UnicodeError):
                raise DeltaReview(REVIEW) from None
            total += len(raw)
            if len(raw) != size or total > MAX_TEXT or mime in found:
                raise DeltaReview(REVIEW)
            found[mime] = text
        elif mime in {"multipart/mixed", "multipart/alternative"}:
            parts = part.get("parts")
            if not isinstance(parts, list) or len(parts) > 16:
                raise DeltaReview(REVIEW)
            for child in parts:
                visit(child, depth + 1)
        # Other MIME types, including attachments, are not decoded or requested.
    visit(payload)
    if not found:
        raise DeltaReview(REVIEW)
    return found


def _inline_fields(payload):
    """Request bytes only at a verified text-only alternative depth.

    Gmail fields apply to every array item at that depth. A structure read is
    necessary to avoid inadvertently requesting a small attachment's data.
    Other MIME layouts require review instead of broader body access.
    """
    def alternatives(part):
        parts = part.get("parts")
        return (part.get("mimeType") == "multipart/alternative" and not part.get("filename")
                and isinstance(parts, list) and 1 <= len(parts) <= 2
                and len({child.get("mimeType") for child in parts if isinstance(child, dict)}) == len(parts)
                and all(isinstance(child, dict) and child.get("mimeType") in {"text/plain", "text/html"}
                        and not child.get("filename") and not child.get("parts") for child in parts))
    if not isinstance(payload, dict) or payload.get("filename"):
        raise DeltaReview(REVIEW)
    if payload.get("mimeType") in {"text/plain", "text/html"} and not payload.get("parts"):
        return "payload(mimeType,filename,body(data,size))"
    if alternatives(payload):
        return "payload(mimeType,filename,parts(mimeType,filename,body(data,size)))"
    parts = payload.get("parts")
    if payload.get("mimeType") == "multipart/mixed" and isinstance(parts, list) and 1 <= len(parts) <= 16:
        texts = [part for part in parts if isinstance(part, dict) and alternatives(part)]
        if len(texts) == 1 and all(isinstance(part, dict) and
            (part is texts[0] or (not part.get("parts") and
             (part.get("filename") or part.get("mimeType") not in {"text/plain", "text/html", "multipart/alternative"})))
            for part in parts):
            return "payload(mimeType,filename,parts(mimeType,filename,parts(mimeType,filename,body(data,size))))"
    raise DeltaReview(REVIEW)


def parse_listing(payload, names, received, booking_salt, *, lisa_names=()):
    """Parse a supported inline MIME payload into narrow rows and a private hash."""
    if (received.tzinfo is None or received.utcoffset() is None
            or not isinstance(booking_salt, bytes) or len(booking_salt) != 32):
        raise DeltaReview(REVIEW)
    aliases, candidates = owner_names(list(names)), []
    lisa_aliases = owner_names(list(lisa_names)) if lisa_names else ()
    if set(aliases) & set(lisa_aliases):
        raise DeltaReview(REVIEW)
    for mime, text in _bodies(payload).items():
        try:
            locator, rows, passengers = (_html_parts(text) if mime == "text/html" else _plain_parts(text))
            normalized = {_normal(name).upper() for name in passengers}
            travelers = [person for person, approved in (("chris", aliases), ("lisa", lisa_aliases))
                         if set(approved) & normalized]
            if not normalized or not travelers:
                raise DeltaReview(REVIEW)
            flights = _records(rows, received.astimezone(UTC))
            records = [dict(row, traveler=person) for person in travelers for row in flights]
            key = hmac.new(booking_salt, ("delta-listing-v1\0" + locator).encode(), hashlib.sha256).hexdigest()
            candidates.append({"booking_key": key, "records": records})
        except (ValueError, TypeError, OverflowError):
            # If one supplied alternative is ambiguous, do not select another
            # alternative that might describe different passengers or flights.
            raise DeltaReview(REVIEW) from None
    if not candidates or any(value != candidates[0] for value in candidates[1:]):
        raise DeltaReview(REVIEW)
    return candidates[0]


def _header(headers, name):
    values = [row.get("value") for row in headers if isinstance(row, dict)
              and isinstance(row.get("name"), str) and row["name"].casefold() == name.casefold()]
    if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > 2048:
        raise DeltaReview(REVIEW)
    try:
        return "".join(part.decode(charset or "utf-8") if isinstance(part, bytes) else part
                       for part, charset in decode_header(values[0]))
    except (LookupError, ValueError, UnicodeError):
        raise DeltaReview(REVIEW) from None


class DeltaTravel:
    def __init__(self, data_dir, config_getter):
        self.data_dir, self.config_getter = Path(data_dir), config_getter
        self.client = GmailClient(self.data_dir / "accounts" / "chris")
        self.travel = PersonalTravel(data_dir, config_getter)
        self.path = self.data_dir / "delta-travel-status.json"
        self.key_path = self.data_dir / "delta-travel-association.key"

    def _authorization(self):
        token = self.client._token()
        # Access-token refresh does not change account identity; a new consent
        # or account does. The fingerprint is used in memory for this poll only.
        return hashlib.sha256(json.dumps([token.get("client_id"),
            token.get("refresh_token") or token.get("access_token")], separators=(",", ":")).encode()).digest()

    def _still_selected(self, selection, authorization):
        try:
            return self._selection() == selection and self.client.connected() and self._authorization() == authorization
        except (MessageError, ValueError, OSError, TypeError):
            return False

    def _booking_salt(self):
        # The HMAC key makes low-entropy locators impossible to recover from
        # an isolated association hash. It never leaves private storage.
        with self.travel.storage._locked():
            try:
                raw = self.key_path.read_text(encoding="ascii")
                if len(raw) > 256:
                    raise ValueError()
                value = json.loads(raw)
                key = base64.b64decode(value["key"], validate=True)
                if len(key) != 32:
                    raise ValueError()
            except FileNotFoundError:
                key = secrets.token_bytes(32)
                private_json(self.key_path, {"key": base64.b64encode(key).decode("ascii")})
            return key

    def _selection(self):
        config = self.config_getter()
        if config.get("delta_travel_enabled") is not True or config.get("demo", True) is not False:
            return None
        chris = owner_names(config.get("delta_travel_owner_names", "[]"))
        try:
            lisa = json.loads(config.get("delta_travel_lisa_names", "[]"))
        except (ValueError, TypeError):
            raise DeltaReview(REVIEW) from None
        if not isinstance(lisa, list):
            raise DeltaReview(REVIEW)
        lisa = owner_names(lisa) if lisa else ()
        if set(chris) & set(lisa):
            raise DeltaReview(REVIEW)
        return chris, lisa

    def status(self):
        try:
            enabled = self._selection() is not None
        except DeltaReview:
            return {"enabled": True, "status": "review_needed", "last_sync": None, "error": REVIEW,
                    "inspected": 0, "imported": 0, "review_needed": 1}
        if not enabled:
            return {"enabled": False, "status": "disabled", "last_sync": None, "error": None,
                    "inspected": 0, "imported": 0, "review_needed": 0}
        if not self.client.connected():
            return {"enabled": True, "status": "not_connected", "last_sync": None, "error": ERROR,
                    "inspected": 0, "imported": 0, "review_needed": 0}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if len(raw) > 4096:
                raise ValueError()
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("status") not in {"ready", "review_needed", "error", "not_connected"}
                    or value.get("error") not in {None, REVIEW, ERROR}
                    or any(type(value.get(key)) is not int or not 0 <= value[key] <= MAX_MESSAGES * MAX_LEGS * 2
                           for key in ("inspected", "imported", "review_needed"))):
                raise ValueError()
            stamp = value.get("last_sync")
            if stamp is not None:
                datetime.fromisoformat(stamp)
            return {key: value.get(key) for key in ("status", "last_sync", "error", "inspected", "imported", "review_needed")} | {"enabled": True}
        except (OSError, ValueError, TypeError, OverflowError):
            return {"enabled": True, "status": "not_connected" if not self.client.connected() else "ready",
                    "last_sync": None, "error": None, "inspected": 0, "imported": 0, "review_needed": 0}

    def sync(self, now=None):
        now = now or datetime.now(UTC)
        inspected = imported = review = 0
        try:
            selection = self._selection()
            if selection is None:
                return self.status()
            if not self.client.connected():
                result = {"status": "not_connected", "last_sync": None, "error": ERROR,
                          "inspected": 0, "imported": 0, "review_needed": 0}
            else:
                authorization = self._authorization()
                booking_salt = self._booking_salt()
                listings, seen = [], set()
                parameters = {"q": "from:" + SENDER + " newer_than:30d -in:spam -in:trash "
                              '{subject:"Non Revenue Listing" subject:reservation subject:itinerary '
                              'subject:cancellation subject:"schedule change" subject:"flight cancelled" subject:"flight canceled"}',
                              "maxResults": 20, "fields": "messages(id),nextPageToken"}
                for page in range(3):
                    response = self.client.api("/messages", parameters)
                    messages = response.get("messages", [])
                    if not isinstance(messages, list) or len(messages) > 20:
                        raise DeltaReview(REVIEW)
                    for summary in messages:
                        if not self._still_selected(selection, authorization):
                            return self.status()
                        identifier = summary.get("id") if isinstance(summary, dict) else None
                        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier) or identifier in seen:
                            raise DeltaReview(REVIEW)
                        seen.add(identifier)
                        metadata = self.client.api("/messages/" + quote(identifier, safe=""), {
                            "format": "metadata", "metadataHeaders": ["From", "Subject"],
                            "fields": "id,internalDate,labelIds,payload(headers)",
                        })
                        inspected += 1
                        try:
                            metadata_payload = metadata.get("payload")
                            headers = metadata_payload.get("headers", []) if isinstance(metadata_payload, dict) else []
                            if not isinstance(headers, list) or metadata.get("id") != identifier:
                                raise DeltaReview(REVIEW)
                            sender = getaddresses([_header(headers, "From")])
                            subject = _header(headers, "Subject")
                            if len(sender) != 1 or sender[0][1].casefold() != SENDER or set(metadata.get("labelIds", [])) & {"SPAM", "TRASH"}:
                                continue
                            if subject != SUBJECT:
                                if re.search(r"non.?rev|listing|reserv|itinerary|cancel|schedule.?change", subject, re.I):
                                    review += 1
                                continue
                            milliseconds = metadata.get("internalDate")
                            if not isinstance(milliseconds, str) or not re.fullmatch(r"\d{1,16}", milliseconds):
                                raise DeltaReview(REVIEW)
                            received = datetime.fromtimestamp(int(milliseconds) / 1000, UTC)
                            if not now - timedelta(days=30) <= received <= now + timedelta(minutes=5):
                                raise DeltaReview(REVIEW)
                            structure = self.client.api("/messages/" + quote(identifier, safe=""), {
                                "format": "full", "fields": _STRUCTURE_FIELDS,
                            }).get("payload")
                            fields = _inline_fields(structure)
                            if not self._still_selected(selection, authorization):
                                return self.status()
                            payload = self.client.api("/messages/" + quote(identifier, safe=""), {
                                "format": "full", "fields": fields,
                            }).get("payload")
                            listings.append(parse_listing(payload, selection[0], received, booking_salt, lisa_names=selection[1]))
                        except (DeltaReview, ValueError, TypeError, KeyError, OverflowError, OSError):
                            review += 1
                    token = response.get("nextPageToken")
                    if not token:
                        break
                    if page == 2 or not isinstance(token, str) or len(token) > 2048:
                        raise DeltaReview(REVIEW)
                    parameters["pageToken"] = token
                if not self._still_selected(selection, authorization):
                    return self.status()
                for listing in sorted(listings, key=lambda row: row["records"][0]["updated_at"]):
                    if not self._still_selected(selection, authorization):
                        return self.status()
                    try:
                        self.travel.import_listing(listing["booking_key"], listing["records"], now)
                        imported += len(listing["records"])
                    except (ValueError, OSError):
                        review += 1
                result = {"status": "review_needed" if review else "ready", "last_sync": now.isoformat(),
                          "error": REVIEW if review else None, "inspected": inspected, "imported": imported,
                          "review_needed": review}
        except DeltaReview:
            result = {"status": "review_needed", "last_sync": None, "error": REVIEW,
                      "inspected": inspected, "imported": 0, "review_needed": max(1, review)}
        except (MessageError, ValueError, OSError, TypeError, KeyError, OverflowError):
            result = {"status": "error", "last_sync": None, "error": ERROR,
                      "inspected": inspected, "imported": 0, "review_needed": review}
        private_json(self.path, result)
        return result | {"enabled": True}
