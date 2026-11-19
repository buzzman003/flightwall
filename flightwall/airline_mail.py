"""Lisa's opt-in, read-only reservation feed. Raw mail and tickets stay in memory."""
import base64
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
import re

from .airline_travel import parse_airline
from .delta_travel import DeltaReview, DeltaTravel, MAX_TEXT, _header, _inline_fields, owner_names
from .messages import MessageError
from .personal_travel import BookingReview, RETENTION


# Any marketing carrier can appear on an agency ticket. Gmail-classified
# reservations can also supply explicitly flight-related mail from other issuers.
ISSUERS = frozenset("""
aa.com delta.com united.com southwest.com jetblue.com alaskaair.com hawaiianairlines.com
allegiantair.com flyfrontier.com spirit.com suncountry.com aircanada.ca aircanada.com
westjet.com aeromexico.com britishairways.com lufthansa.com airfrance.com klm.com
virginatlantic.com iberia.com flysas.com ryanair.com easyjet.com emirates.com
qatarairways.com singaporeair.com qantas.com.au ana.co.jp jal.co.jp airnewzealand.co.nz
latam.com avianca.com cwtsato.com mycwt.com expedia.com travelocity.com
""".split())
CONSUMER_DOMAINS = frozenset("gmail.com googlemail.com hotmail.com outlook.com live.com yahoo.com icloud.com aol.com proton.me protonmail.com".split())
TOPIC = re.compile(r"\b(?:e[- ]?ticket|ticket|receipt|invoice|itinerary|confirmation|reservation|booking|booked|confirmed|trip)\b", re.I)
CHANGED = re.compile(r"\b(?:cancel(?:ed|led|lation)|reschedul\w*|chang(?:e|ed|es)|refund\w*|void(?:ed)?)\b", re.I)
CANCELLATION = re.compile(r"\b(?:cancel(?:ed|led|lation)|refund\w*|void(?:ed)?)\b", re.I)
TICKETED = re.compile(r"^\s*(?:e[- ]?)?ticket\s*[/ -]\s*invoice\s+issued\b", re.I)
CWT_CHANGE = re.compile(r"^\s*(?:e[- ]?)?ticket\s*[/ -]\s*invoice\b", re.I)
NONFLIGHT = re.compile(r"\b(?:hotel|rental car|cruise|concert|train|price alert|special offer|earn miles)\b", re.I)
RECEIPTS = ('{subject:flight subject:itinerary subject:receipt subject:confirmation subject:reservation '
            'subject:booking subject:ticket subject:invoice subject:trip}')
MAIL_FILTER = 'newer_than:1y -in:spam -in:trash -in:sent -in:drafts '
KNOWN_QUERY = MAIL_FILTER + RECEIPTS + ' {' + ' '.join('from:' + d for d in sorted(ISSUERS)) + '}'
CATEGORY_QUERY = MAIL_FILTER + RECEIPTS + ' category:reservations'
TICKET_QUERY = MAIL_FILTER + 'from:cwtsato.com subject:issued subject:invoice'
STRUCTURE = ('payload(mimeType,filename,body(size,attachmentId),parts(mimeType,filename,body(size,attachmentId),'
             'parts(mimeType,filename,body(size,attachmentId))))')


def _issuer(headers, subject):
    addresses = getaddresses([_header(headers, "From")])
    if len(addresses) != 1 or not re.fullmatch(r"[^@\s]+@[a-zA-Z0-9.-]+", addresses[0][1]):
        return None
    domain = addresses[0][1].rsplit("@", 1)[1].casefold()
    # Gmail prepends its own results. Never search lower, forwarded headers
    # for a passing result when Google's first result failed or is absent.
    results = next((r.get("value", "") for r in headers if isinstance(r, dict)
                    and str(r.get("name")).lower() == "authentication-results"), "")
    if (not isinstance(results, str) or len(results) > 8192
            or not re.match(r"^\s*mx\.google\.com\s*;", results, re.I)):
        return None
    dmarc = [item for item in results.split(";") if re.match(r"\s*dmarc=", item, re.I)]
    if (len(dmarc) != 1 or not re.match(r"\s*dmarc=pass\b", dmarc[0], re.I)
            or not re.search(r"\bheader\.from=" + re.escape(domain) + r"(?=\s|$)", dmarc[0], re.I)):
        return None
    flight_notice = CHANGED.search(subject) and re.search(r"\bflight\b", subject, re.I)
    if not (TOPIC.search(subject) or flight_notice) or NONFLIGHT.search(subject):
        return None
    registered = next((known for known in ISSUERS if domain == known or domain.endswith("." + known)), None)
    if registered:
        return registered
    if (domain not in CONSUMER_DOMAINS and
            re.search(r"\b(?:flight|airline|airfare|air travel)\b", subject, re.I)):
        return domain
    return None


def _pdf_part(structure):
    parts = structure.get("parts", [])
    if (structure.get("mimeType") != "multipart/mixed" or not isinstance(parts, list)
            or not 1 <= len(parts) <= 12):
        raise DeltaReview()
    files = [p for p in parts if isinstance(p, dict) and p.get("mimeType") == "application/pdf"
             and isinstance(p.get("filename"), str) and p["filename"].lower().endswith(".pdf")]
    if len(files) != 1:
        raise DeltaReview()
    body = files[0].get("body", {})
    if not isinstance(body, dict):
        raise DeltaReview()
    size, aid = body.get("size"), body.get("attachmentId")
    # The Gmail client bounds JSON responses to 2 MiB including base64 overhead.
    if (type(size) is not int or not 1 <= size <= 1400000
            or not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", aid)):
        raise DeltaReview()
    return aid, size


def _bounded_fields(structure):
    fields = _inline_fields(structure)
    parts = [structure]
    total = 0
    while parts:
        part = parts.pop()
        if part.get("filename"):
            continue
        if part.get("mimeType") in {"text/html", "text/plain"}:
            body = part.get("body")
            if not isinstance(body, dict) or body.get("attachmentId"):
                raise DeltaReview()
            size = body.get("size")
            if type(size) is not int or not 0 < size <= MAX_TEXT:
                raise DeltaReview()
            total += size
        elif part.get("mimeType") in {"multipart/alternative", "multipart/mixed"}:
            parts.extend(part["parts"])
    if not 0 < total <= MAX_TEXT:
        raise DeltaReview()
    return fields


class AirlineMail(DeltaTravel):
    def __init__(self, data_dir, config_getter):
        super().__init__(data_dir, config_getter, person="lisa")
        self._scope = None
        self._seen = OrderedDict()
        self._unmatched_changes = OrderedDict()
        self._next = {}
        self._result = {}

    def _selection(self):
        cfg = self.config_getter()
        if cfg.get("demo", True) is not False or cfg.get("lisa_airline_mail_enabled") is not True:
            return None
        return owner_names(cfg.get("delta_travel_lisa_names", "[]")), cfg.get("lisa_ticket_pdfs_enabled") is True

    def status(self):
        try:
            selection = self._selection()
        except DeltaReview:
            return {"status": "review_needed"}
        if selection is None:
            return {"status": "disabled"}
        if not self.client.connected():
            return {"status": "not_connected"}
        try:
            current = self._authorization()
        except (ValueError, OSError):
            return {"status": "not_connected"}
        if self._scope != (selection, current):
            return {"status": "waiting"}
        return dict(self._result) or {"status": "waiting"}

    def _reservation(self, identifier, now, selection, authorization, salt, categorized):
        def active():
            if not self._still_selected(selection, authorization):
                raise MessageError("Booking selection changed.")

        path = "/messages/" + identifier
        active()
        meta = self.client.api(path, {"format": "metadata", "metadataHeaders": ["From", "Subject", "Authentication-Results"],
                                     "fields": "id,internalDate,labelIds,payload(headers)"})
        if meta.get("id") != identifier or set(meta.get("labelIds", [])) & {"SPAM", "TRASH", "DRAFT", "SENT"}:
            return []
        headers = meta.get("payload", {}).get("headers", [])
        subject = _header(headers, "Subject")
        issuer = _issuer(headers, subject)
        if not issuer:
            return []
        if issuer not in ISSUERS and not categorized:
            # A Gmail from: query can also match a display name. An unknown
            # authenticated issuer needs a separate positive reservation query.
            return None
        changed = bool(CHANGED.search(subject))
        received = datetime.fromtimestamp(int(meta["internalDate"]) / 1000, timezone.utc)
        if not now - timedelta(days=367) <= received <= now + timedelta(minutes=5):
            raise DeltaReview()
        if issuer == "cwtsato.com" and (not selection[1] or not (
                TICKETED.search(subject) or changed and CWT_CHANGE.search(subject))):
            if selection[1] and changed:
                raise DeltaReview()
            return []
        active()
        structure = self.client.api(path, {"format": "full", "fields": STRUCTURE})["payload"]
        if issuer == "cwtsato.com":
            from .airline_travel import parse_cwt_invoice
            from .itinerary_pdf import extract_itinerary_text
            aid, size = _pdf_part(structure)
            active()
            result = self.client.ticket_attachment(identifier, aid)
            active()
            encoded = result.get("data")
            if result.get("size") != size or not isinstance(encoded, str) or len(encoded) > (size + 2) // 3 * 4:
                raise DeltaReview()
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            if len(raw) != size:
                raise DeltaReview()
            ticket = parse_cwt_invoice(extract_itinerary_text(raw), selection[0], received, salt,
                                       allow_cancellation=changed)
        else:
            fields = _bounded_fields(structure)
            active()
            payload = self.client.api(path, {"format": "full", "fields": fields})["payload"]
            active()
            ticket = parse_airline(payload, selection[0], received, salt, issuer=issuer, allow_cancellation=True)
        if CANCELLATION.search(subject) and not any(row["status"] == "cancelled" for row in ticket["records"]):
            raise DeltaReview()
        rows = [row for row in ticket["records"] if datetime.fromisoformat(row["end"]) > now - RETENTION]
        return (ticket["booking_key"], rows, changed) if rows else []

    def sync(self, now=None):
        now = now or datetime.now(timezone.utc)
        try:
            selection = self._selection()
        except DeltaReview:
            return {"status": "review_needed"}
        if not selection or not self.client.connected():
            return self.status()
        try:
            authorization = self._authorization()
            scope = selection, authorization
            if self._scope != scope:
                self._scope, self._next, self._result = scope, {}, {}
                self._seen.clear()
                self._unmatched_changes.clear()
            counts = {"inspected": 0, "matched_legs": 0, "review_needed": 0}
            salt = self._booking_salt()
            queries = [(KNOWN_QUERY, 12), (CATEGORY_QUERY, 12)]
            if selection[1]:
                queries.insert(0, (TICKET_QUERY, 1))  # First look at the newest issued agency ticket.
            completed = set()
            for identifier, categorized in self._candidates(queries, selection, authorization):
                if identifier in completed:
                    continue
                if identifier not in self._seen:
                    try:
                        ticket = self._reservation(identifier, now, selection, authorization, salt, categorized)
                        if ticket is None:
                            continue
                        if not self._still_selected(selection, authorization):
                            return self.status()
                    except (ValueError, KeyError, TypeError, OverflowError) as exc:
                        if isinstance(exc, MessageError):
                            raise
                        outcome = 0, 1
                    else:
                        if ticket:
                            key, rows, changed = ticket
                            try:
                                self.travel.import_airline_receipt(key, rows, now, change_notice=changed)
                            except BookingReview:
                                # Gmail returns newest first. Try this notice again after older
                                # receipts have established the exact booking association.
                                completed.add(identifier)
                                counts["inspected"] += 1
                                self._unmatched_changes[identifier] = None
                                if len(self._unmatched_changes) > 1024:
                                    self._unmatched_changes.popitem(last=False)
                                continue
                        outcome = len(ticket[1]) if ticket else 0, 0
                    self._unmatched_changes.pop(identifier, None)
                    self._seen[identifier] = outcome
                match_count, review = self._seen[identifier]
                completed.add(identifier)
                self._seen.move_to_end(identifier)
                counts["inspected"] += 1
                counts["matched_legs"] += match_count
                counts["review_needed"] += review
                if len(self._seen) > 1024:
                    self._seen.popitem(last=False)
            counts["review_needed"] = sum(review for _, review in self._seen.values()) + len(self._unmatched_changes)
            self._result = dict(counts, status="review_needed" if counts["review_needed"] else "ready",
                                last_sync=now.isoformat())
        except (ValueError, TypeError, OSError, KeyError, OverflowError):
            self._next.clear()
            self._result = {"status": "error"}
        return self.status()

    def _candidates(self, queries, selection, authorization):
        for query, limit in queries:
            if not self._still_selected(selection, authorization):
                raise MessageError("Booking selection changed.")
            parameters = {"q": query, "maxResults": limit, "fields": "messages(id),nextPageToken,resultSizeEstimate"}
            if self._next.get(query):
                parameters["pageToken"] = self._next[query]
            response = self.client.api("/messages", parameters)
            if not isinstance(response, dict):
                raise DeltaReview()
            items = response.get("messages", [])
            page = response.get("nextPageToken")
            if (not isinstance(items, list) or len(items) > limit or (page is not None and
                    (not isinstance(page, str) or not 1 <= len(page) <= 2048 or page == self._next.get(query)))):
                raise DeltaReview()
            for item in items:
                ident = item.get("id") if isinstance(item, dict) else None
                if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", ident):
                    raise DeltaReview()
                yield ident, query == CATEGORY_QUERY
            if query != TICKET_QUERY:
                self._next[query] = page
