"""Reviewed personal reservations, separate from all operating schedules."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from .flight_alerts import stamp
from .mycrew import airport_zone
from .personal import PersonalError, PersonalStore


MAX_BYTES = 65536
MAX_RECORDS = 64
ROWS_PER_PAGE = 3
DISPLAY_WINDOW = timedelta(days=14)
RETENTION = timedelta(days=30)
STATUSES = {"listed", "jumpseat_requested", "confirmed", "cancelled", "flown"}
FIELDS = {"number", "origin", "destination", "start", "end", "status", "source", "updated_at"}
_FLIGHT_NUMBER = re.compile(r"(?:[A-Z][A-Z0-9]|[0-9][A-Z])[1-9][0-9]{0,3}")
_AIRLINE_RECEIPTS = {"american_email", "airline_email"}


class BookingReview(ValueError):
    """A notice needs an earlier receipt or a clearer reference to the changed leg."""


def reservation_schedule(row):
    """Validate the flight identity and return its scheduled times in UTC."""
    if not _FLIGHT_NUMBER.fullmatch(row["number"]):
        raise ValueError("Use a two-character airline code and a flight number from 1 to 9999.")
    origin, destination = row["origin"], row["destination"]
    if origin == destination or not all(re.fullmatch(r"[A-Z]{3}", code) and airport_zone(code)
                                        for code in (origin, destination)):
        raise ValueError("Use two distinct supported three-letter airport codes.")
    start, end = stamp(row["start"]), stamp(row["end"])
    if not timedelta(0) < end - start <= timedelta(days=1):
        raise ValueError("The scheduled arrival must follow departure by no more than one day.")
    return start, end


def _reservation(row):
    if not isinstance(row, dict) or row.keys() not in (FIELDS, FIELDS | {"traveler"}):
        raise ValueError("Use only the documented personal travel fields.")
    if any(not isinstance(value, str) or len(value) > 40 for value in row.values()):
        raise ValueError("Personal travel fields must be short text values.")
    start, end = reservation_schedule(row)
    updated = stamp(row["updated_at"])
    source = row["source"]
    if row["status"] not in STATUSES or (source not in _AIRLINE_RECEIPTS and source not in {"delta_email", "calendar_copy"}):
        raise ValueError("Use a documented personal travel status and source.")
    if source in _AIRLINE_RECEIPTS:
        if (row.get("traveler") != "lisa" or row["status"] not in {"confirmed", "cancelled"}
                or (source == "american_email" and not row["number"].startswith("AA"))):
            raise ValueError("Use an identified Lisa airline reservation with a matching carrier and status.")
    elif not row["number"].startswith("DL"):
        raise ValueError("Delta listings and calendar copies require a Delta flight number.")
    if row.get("traveler", "chris") not in {"chris", "lisa"}:
        raise ValueError("Use a supported personal traveler.")
    if row["source"] == "calendar_copy" and row["status"] not in {"listed", "jumpseat_requested"}:
        raise ValueError("A calendar copy alone cannot confirm, cancel or prove travel.")
    if row["status"] == "flown" and updated < start:
        raise ValueError("Use flown only with a post-departure Delta record of actual travel.")
    return dict(row, start=start.isoformat(), end=end.isoformat(), updated_at=updated.isoformat())


def _identity(row):
    day = stamp(row["start"]).astimezone(airport_zone(row["origin"])).date().isoformat()
    return row.get("traveler", "chris"), row["number"], day, row["origin"], row["destination"]


def _precedence(row):
    return row["source"] != "calendar_copy", stamp(row["updated_at"])


def _resolve(rows):
    by_identity = {}
    for row in rows:
        by_identity.setdefault(_identity(row), []).append(row)
    resolved = {}
    for key, candidates in by_identity.items():
        if any(row["status"] == "flown" for row in candidates):
            # A receipt confirms a booking; it cannot undo evidence of actual travel.
            candidates = [row for row in candidates if row["source"] not in _AIRLINE_RECEIPTS]
        best = max(candidates, key=_precedence)
        # A carrier and an agency may report the same leg at the same instant.
        # Its times and booking status must agree; the source label can differ.
        if any(_precedence(row) == _precedence(best)
               and any(row[k] != best[k] for k in ("start", "end", "status")) for row in candidates):
            raise ValueError("Conflicting travel records share the same source time. Review the latest evidence.")
        resolved[key] = best
    return resolved


def visible_travel(rows, config, now):
    """Apply the same privacy and schedule bounds at the service and every frame."""
    if (config.get("demo", True) is not False or config.get("guest_mode", False) is not False
            or config.get("personal_travel_cards_visible", True) is not True
            or not isinstance(rows, list) or len(rows) > MAX_RECORDS):
        return []
    valid = []
    for value in rows:
        try:
            row = _reservation(value)
            if (row["status"] in {"listed", "jumpseat_requested", "confirmed"} and stamp(row["end"]) > now
                    and stamp(row["start"]) < now + DISPLAY_WINDOW):
                valid.append(row)
        except (ValueError, TypeError, OverflowError):
            continue
    return sorted(valid, key=lambda row: (stamp(row["start"]), _identity(row)))[:12]


def _read_json(path):
    with path.open(encoding="utf-8") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Personal travel JSON exceeds its size limit.")
    return json.loads(raw)


class PersonalTravel:
    def __init__(self, data_dir, config_getter):
        self.storage = PersonalStore(data_dir, config_getter)
        self.storage.path = Path(data_dir) / "personal-travel.json"
        self.storage.lock_path = Path(data_dir) / "personal-travel.lock"
        self.airline_evidence = Path(data_dir) / "airline-bookings.json"

    def _document(self):
        try:
            document = _read_json(self.storage.path)
            if (not isinstance(document, dict) or type(document.get("schema")) is not int
                    or (document["schema"] == 1 and document.keys() != {"schema", "reservations"})
                    or (document["schema"] == 2 and document.keys() != {"schema", "reservations", "bookings"})
                    or document["schema"] not in {1, 2}
                    or not isinstance(document["reservations"], list)
                    or len(document["reservations"]) > MAX_RECORDS):
                raise ValueError("Unsupported travel store.")
            rows = [_reservation(row) for row in document["reservations"]]
            if len({_identity(row) for row in rows}) != len(rows):
                raise ValueError("Duplicate travel records.")
            bookings = document.get("bookings", {})
            if not isinstance(bookings, dict) or len(bookings) > MAX_RECORDS:
                raise ValueError("Unsupported booking associations.")
            for key, value in bookings.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key)
                        or not isinstance(value, dict) or value.keys() != {"updated_at", "identities"}
                        or not isinstance(value["identities"], list) or not 1 <= len(value["identities"]) <= MAX_RECORDS
                        or len(set(value["identities"])) != len(value["identities"])
                        or any(not isinstance(item, str) or not re.fullmatch(
                            r"(?:chris|lisa)\|DL[1-9][0-9]{0,3}\|\d{4}-\d{2}-\d{2}\|[A-Z]{3}\|[A-Z]{3}", item)
                               for item in value["identities"])):
                    raise ValueError("Unsupported booking association.")
                stamp(value["updated_at"])
            return {"schema": document["schema"], "reservations": rows, "bookings": bookings}
        except FileNotFoundError:
            return {"schema": 1, "reservations": [], "bookings": {}}
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            raise ValueError("The private travel store could not be read. Its contents were preserved.") from None

    def list_records(self):
        if not self.airline_evidence.exists():
            return self._document()["reservations"]
        with self.storage._locked():
            base = self._document()["reservations"]
            return self._apply_airline_evidence(base, self._read_airline_evidence())

    def _read_airline_evidence(self):
        try:
            document = _read_json(self.airline_evidence)
        except FileNotFoundError:
            return {}
        if not isinstance(document, dict) or len(document) > MAX_RECORDS:
            raise ValueError("Invalid private airline booking associations.")
        evidence = {}
        for key, entries in document.items():
            if (not re.fullmatch(r"[a-f0-9]{64}", key) or not isinstance(entries, list)
                    or not 1 <= len(entries) <= MAX_RECORDS):
                raise ValueError("Invalid private airline booking association.")
            rows = [_reservation(row) for row in entries]
            if any(row["source"] not in _AIRLINE_RECEIPTS for row in rows) or len({_identity(r) for r in rows}) != len(rows):
                raise ValueError("Invalid private airline booking leg.")
            evidence[key] = rows
        return evidence

    @staticmethod
    def _apply_airline_evidence(base, evidence):
        # Two independently booked reservations can cover the same flight.
        # Closing one leaves the other usable until it is also cancelled.
        grouped = {}
        for rows in evidence.values():
            for row in rows:
                grouped.setdefault(_identity(row), []).append(row)
        resolved = {_identity(row): row for row in base}
        for leg, rows in grouped.items():
            active = [row for row in rows if row["status"] == "confirmed"]
            winner = max(active or rows, key=_precedence)
            latest = max(stamp(row["updated_at"]) for row in rows)
            winner = dict(winner, updated_at=latest.isoformat())
            prior = resolved.get(leg)
            if prior is None or (prior["source"] in _AIRLINE_RECEIPTS and _precedence(prior) <= _precedence(winner)):
                resolved[leg] = winner
            else:
                resolved[leg] = _resolve([prior, winner])[leg]
        if len(resolved) > MAX_RECORDS:
            raise ValueError("Private travel store is full.")
        return [resolved[key] for key in sorted(resolved)]

    def import_airline_receipt(self, booking_key, payload, now, *, change_notice=False):
        """Reconcile only mentioned legs. A cancellation must match a known ticket."""
        rows = self._records(payload, now)
        if (not re.fullmatch(r"[a-f0-9]{64}", booking_key) or any(r["source"] not in _AIRLINE_RECEIPTS for r in rows)
                or len({r["updated_at"] for r in rows}) != 1 or len({_identity(r) for r in rows}) != len(rows)):
            raise BookingReview("Ambiguous airline booking.")
        with self.storage._locked():
            document = self._document()
            prior_evidence = self._read_airline_evidence()
            evidence = {key: [r for r in entries if stamp(r["end"]) > now - RETENTION]
                        for key, entries in prior_evidence.items()}
            evidence = {key: entries for key, entries in evidence.items() if entries}
            associated = {_identity(r): r for r in evidence.get(booking_key, [])}
            if change_notice and not any(_identity(r) in associated for r in rows):
                raise BookingReview("Verify the original booking before applying this change.")
            if change_notice and any(_identity(r) not in associated for r in rows) and not any(
                    r["status"] == "cancelled" for r in rows):
                raise BookingReview("An itinerary change needs the replaced leg.")
            if change_notice and all(
                    r["status"] == "confirmed" and _identity(r) in associated and
                    all(r[k] == associated[_identity(r)][k] for k in ("status", "start", "end")) and
                    stamp(r["updated_at"]) > stamp(associated[_identity(r)]["updated_at"]) for r in rows):
                raise BookingReview("The change notice does not identify a changed flight.")
            for row in rows:
                identity = _identity(row)
                previous = associated.get(identity)
                if previous and stamp(row["updated_at"]) < stamp(previous["updated_at"]):
                    continue
                if row["status"] == "cancelled" and (not previous or row["start"] != previous["start"]):
                    raise BookingReview("Cancellation needs the exact confirmed departure.")
                if previous and row["updated_at"] == previous["updated_at"] and row != previous:
                    raise BookingReview("Conflicting airline notices have the same time.")
                associated[identity] = row
            evidence[booking_key] = [associated[key] for key in sorted(associated)]
            if len(evidence) > MAX_RECORDS or len(json.dumps(evidence).encode()) > MAX_BYTES:
                raise ValueError("Private airline booking evidence is full.")
            base = [r for r in document["reservations"] if stamp(r["end"]) > now - RETENTION]
            result = self._apply_airline_evidence(base, evidence)
            # Publish the evidence first. Even after an interrupted projection,
            # list_records applies it under the same lock before any paid tracking.
            if evidence != prior_evidence:
                self.storage._write_document(self.airline_evidence, evidence)
            if result != document["reservations"]:
                self._write(result, document["bookings"])

    def import_file(self, path, now=None):
        now = now or datetime.now(timezone.utc)
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ValueError()
            payload = _read_json(path)
        except (OSError, ValueError, RecursionError):
            raise ValueError("Choose a regular local personal travel JSON file under 64 KiB.") from None
        return self.import_records(payload, now)

    @staticmethod
    def _records(payload, now):
        if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_RECORDS:
            raise ValueError("Provide a list of 1 to 64 reviewed personal travel records.")
        rows = [_reservation(row) for row in payload]
        if any(stamp(row["updated_at"]) > now + timedelta(minutes=5)
               or stamp(row["end"]) <= now - RETENTION
               or stamp(row["start"]) >= now + timedelta(days=366) for row in rows):
            raise ValueError("Check the source clock and keep travel within the supported past 30 days and next year.")
        return rows

    @staticmethod
    def _identity_text(row):
        return "|".join(_identity(row))

    def import_records(self, payload, now=None):
        """Import already narrowed records without writing a body-derived temporary file."""
        now = now or datetime.now(timezone.utc)
        rows = self._records(payload, now)
        try:
            with self.storage._locked():
                document = self._document()
                previous = document["reservations"]
                retained = [row for row in previous if stamp(row["end"]) > now - RETENTION]
                merged = _resolve([*retained, *rows])
                if len(merged) > MAX_RECORDS:
                    raise ValueError("The private travel store is full. Old travel is removed after 30 days.")
                result = [merged[key] for key in sorted(merged)]
                if result != previous:
                    self._write(result, document["bookings"])
        except PersonalError:
            raise ValueError("The private travel store is unavailable. Check its directory and free space.") from None
        return {"reviewed": len(rows), "stored": len(result), "changed": result != previous}

    def _write(self, rows, bookings):
        retained = {self._identity_text(row) for row in rows}
        bookings = {key: dict(value, identities=[item for item in value["identities"] if item in retained])
                    for key, value in bookings.items()}
        bookings = {key: value for key, value in bookings.items() if value["identities"]}
        document = {"schema": 2 if bookings else 1, "reservations": rows}
        if bookings:
            document["bookings"] = bookings
        if len(json.dumps(document).encode("utf-8")) > MAX_BYTES:
            raise ValueError("The private travel store is full. Old travel is removed after 30 days.")
        self.storage._write(document)

    def import_listing(self, booking_key, payload, now=None):
        """Atomically reconcile one complete, supported listing and its private hash.

        The association never contains a locator, passenger or message identifier.
        A missing segment is superseded only by a newer snapshot of this booking.
        Confirmed evidence newer than that snapshot is retained by normal precedence.
        """
        now = now or datetime.now(timezone.utc)
        rows = self._records(payload, now)
        if (not isinstance(booking_key, str) or not re.fullmatch(r"[a-f0-9]{64}", booking_key)
                or any(row["status"] != "listed" or row["source"] != "delta_email" for row in rows)
                or len({row["updated_at"] for row in rows}) != 1
                or len({_identity(row) for row in rows}) != len(rows)):
            raise ValueError("Use one supported Delta listing snapshot.")
        observed = stamp(rows[0]["updated_at"])
        identities = sorted(self._identity_text(row) for row in rows)
        try:
            with self.storage._locked():
                document = self._document()
                previous, bookings = document["reservations"], dict(document["bookings"])
                retained = [row for row in previous if stamp(row["end"]) > now - RETENTION]
                association = bookings.get(booking_key)
                if association and observed < stamp(association["updated_at"]):
                    return {"reviewed": len(rows), "stored": len(previous), "changed": False}
                if (association and observed == stamp(association["updated_at"])
                        and identities != association["identities"]):
                    raise ValueError("Conflicting Delta listing snapshots require review.")
                other_identities = {identity for key, value in bookings.items() if key != booking_key
                                    for identity in value["identities"]}
                removed = set(association["identities"] if association else ()) - set(identities) - other_identities
                cancellations = [dict(row, status="cancelled", updated_at=rows[0]["updated_at"])
                                 for row in retained if self._identity_text(row) in removed
                                 and row["source"] == "delta_email" and row["status"] == "listed"
                                 and stamp(row["updated_at"]) <= observed and stamp(row["start"]) >= observed]
                established = {_identity(row) for row in retained if row["status"] in {"confirmed", "flown"}}
                listing_rows = [row for row in rows if _identity(row) not in established]
                merged = _resolve([*retained, *listing_rows, *cancellations])
                if len(merged) > MAX_RECORDS:
                    raise ValueError("The private travel store is full. Old travel is removed after 30 days.")
                result = [merged[key] for key in sorted(merged)]
                bookings[booking_key] = {"updated_at": rows[0]["updated_at"], "identities": identities}
                if len(bookings) > MAX_RECORDS:
                    raise ValueError("The private booking association store is full.")
                changed = result != previous or bookings != document["bookings"]
                if changed:
                    self._write(result, bookings)
        except PersonalError:
            raise ValueError("The private travel store is unavailable. Check its directory and free space.") from None
        return {"reviewed": len(rows), "stored": len(result), "changed": changed}
