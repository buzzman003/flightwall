"""Read only Hospitable reservation movements and reduce them to daily counts."""

from datetime import date, datetime, timedelta
from http.client import HTTPException
import json
import re
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID


API = "https://public.api.hospitable.com/v2/"
WINDOW_DAYS = 4
PAGE_SIZE = 100
PAGE_LIMIT = 4
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TOKEN = re.compile(r"[\x21-\x7e]{4,4096}")


class GuestOperationsError(ValueError):
    """A safe, generic provider failure without response or credential data."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _get_page(opener, path, token, query):
    request = Request(API + path + "?" + urlencode(query), headers={
        "Accept": "application/json", "Authorization": "Bearer " + token, "User-Agent": "FlightWall",
    })
    with opener.open(request, timeout=8) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise GuestOperationsError("Hospitable returned too much data.")
    return json.loads(payload)


def _rows(opener, path, token, parameters):
    for page in range(1, PAGE_LIMIT + 1):
        document = _get_page(opener, path, token, [*parameters, ("page", page), ("per_page", PAGE_SIZE)])
        metadata = document.get("meta") if isinstance(document, dict) else None
        rows = document.get("data") if isinstance(document, dict) else None
        pages = metadata.get("last_page") if isinstance(metadata, dict) else None
        if (not isinstance(rows, list) or len(rows) > PAGE_SIZE or not all(isinstance(row, dict) for row in rows)
                or type(pages) is not int or not page <= pages <= PAGE_LIMIT
                or metadata.get("current_page") != page):
            raise GuestOperationsError("Hospitable results could not be verified.")
        yield from rows
        if page == pages:
            return


def _accepted_stay(row):
    record = row.get("reservation_status")
    current = record.get("current") if isinstance(record, dict) else None
    category = current.get("category") if isinstance(current, dict) else None
    status = row.get("status")
    statuses = (value for value in (status, category) if value is not None)
    if status is None and category is None:
        raise GuestOperationsError("A Hospitable reservation status could not be verified.")
    if not all(value == "accepted" for value in statuses):
        return None
    if row.get("owner_stay") or row.get("stay_type") == "owner_stay":
        return None
    if row.get("stay_type") not in {None, "guest_stay"}:
        raise GuestOperationsError("A Hospitable stay type could not be verified.")
    identity = row.get("id")
    if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
        raise GuestOperationsError("A Hospitable reservation identity could not be verified.")
    arrival = _local_day(row.get("arrival_date") or row.get("check_in"))
    departure = _local_day(row.get("departure_date") or row.get("check_out"))
    if arrival > departure:
        raise GuestOperationsError("Hospitable returned inconsistent reservation dates.")
    return identity, arrival, departure


def _local_day(value):
    if isinstance(value, str) and len(value) <= 40:
        try:
            if len(value) == 10:
                return date.fromisoformat(value)
            # Hospitable supplies property-local dates. Keep their civil day.
            return datetime.fromisoformat(value).date()
        except ValueError:
            pass
    raise GuestOperationsError("Hospitable returned an invalid stay date.")


def project_operations(today, reservations):
    """Drop provider records after counting accepted guest arrivals and departures."""
    dates = (today + timedelta(days=i) for i in range(WINDOW_DAYS))
    days = {day: {"date": day.isoformat(), "arrivals": 0, "departures": 0} for day in dates}
    stays = {}
    for row in reservations:
        if not isinstance(row, dict):
            raise GuestOperationsError("Hospitable returned an invalid reservation.")
        stay = _accepted_stay(row)
        if stay is None:
            continue
        identity, arrival, departure = stay
        if stays.setdefault(identity, (arrival, departure)) != (arrival, departure):
            raise GuestOperationsError("Hospitable returned inconsistent reservation dates.")
    for arrival, departure in stays.values():
        if arrival in days:
            days[arrival]["arrivals"] += 1
        if departure in days:
            days[departure]["departures"] += 1
    return list(days.values())


def fetch_operations(token, today):
    """Fetch four property-local days of scheduled guest activity over fixed HTTPS."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise GuestOperationsError("A valid Hospitable read-only token is required.")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        properties = sorted({str(UUID(row["id"])) for row in _rows(opener, "properties", token, [])})
        # Dates outside the visible window are discarded below. Padding keeps
        # the first and last visible days even if the API treats an edge as exclusive.
        filters = [("start_date", (today - timedelta(days=1)).isoformat()),
                   ("end_date", (today + timedelta(days=WINDOW_DAYS)).isoformat()), ("status[]", "accepted")]
        reservations = []
        for start in range(0, len(properties), PAGE_SIZE):
            property_filter = [("properties[]", identity) for identity in properties[start:start + PAGE_SIZE]]
            for date_query in ("checkin", "checkout"):
                query = [*filters, *property_filter, ("date_query", date_query)]
                reservations.extend(_rows(opener, "reservations", token, query))
        return project_operations(today, reservations)
    except (OSError, HTTPException, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise GuestOperationsError("Hospitable guest operations could not be refreshed.") from None
