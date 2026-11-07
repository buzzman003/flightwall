"""Owner-reviewed Delta reservations, separate from all operating schedules."""
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


def _reservation(row):
    if not isinstance(row, dict) or row.keys() != FIELDS:
        raise ValueError("Use only the documented personal travel fields.")
    if any(not isinstance(value, str) or len(value) > 40 for value in row.values()):
        raise ValueError("Personal travel fields must be short text values.")
    if not re.fullmatch(r"DL[1-9][0-9]{0,3}", row["number"]):
        raise ValueError("Use the Delta flight number, such as DL1234.")
    origin, destination = row["origin"], row["destination"]
    if origin == destination or not all(re.fullmatch(r"[A-Z]{3}", code) and airport_zone(code)
                                        for code in (origin, destination)):
        raise ValueError("Use two distinct supported three-letter airport codes.")
    start, end, updated = (stamp(row[field]) for field in ("start", "end", "updated_at"))
    if not timedelta(0) < end - start <= timedelta(days=1):
        raise ValueError("The scheduled arrival must follow departure by no more than one day.")
    if row["status"] not in STATUSES or row["source"] not in {"delta_email", "calendar_copy"}:
        raise ValueError("Use a documented personal travel status and source.")
    if row["source"] == "calendar_copy" and row["status"] not in {"listed", "jumpseat_requested"}:
        raise ValueError("A calendar copy alone cannot confirm, cancel or prove travel.")
    if row["status"] == "flown" and updated < start:
        raise ValueError("Use flown only with a post-departure Delta record of actual travel.")
    return dict(row, start=start.isoformat(), end=end.isoformat(), updated_at=updated.isoformat())


def _identity(row):
    day = stamp(row["start"]).astimezone(airport_zone(row["origin"])).date().isoformat()
    return row["number"], day, row["origin"], row["destination"]


def _precedence(row):
    return row["source"] == "delta_email", stamp(row["updated_at"])


def _resolve(rows):
    by_identity = {}
    for row in rows:
        by_identity.setdefault(_identity(row), []).append(row)
    resolved = {}
    for key, candidates in by_identity.items():
        best = max(candidates, key=_precedence)
        if any(_precedence(row) == _precedence(best) and row != best for row in candidates):
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
            if (row["status"] == "confirmed" and stamp(row["end"]) > now
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

    def _read(self):
        try:
            document = _read_json(self.storage.path)
            if (not isinstance(document, dict) or document.keys() != {"schema", "reservations"}
                    or type(document["schema"]) is not int or document["schema"] != 1
                    or not isinstance(document["reservations"], list)
                    or len(document["reservations"]) > MAX_RECORDS):
                raise ValueError("Unsupported travel store.")
            rows = [_reservation(row) for row in document["reservations"]]
            if len({_identity(row) for row in rows}) != len(rows):
                raise ValueError("Duplicate travel records.")
            return rows
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            raise ValueError("The private travel store could not be read. Its contents were preserved.") from None

    def list_records(self):
        # Atomic replacement means a read observes one entire import or its predecessor.
        return self._read()

    def import_file(self, path, now=None):
        now = now or datetime.now(timezone.utc)
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ValueError()
            payload = _read_json(path)
        except (OSError, ValueError, RecursionError):
            raise ValueError("Choose a regular local personal travel JSON file under 64 KiB.") from None
        if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_RECORDS:
            raise ValueError("Provide a list of 1 to 64 reviewed personal travel records.")
        rows = [_reservation(row) for row in payload]
        if any(stamp(row["updated_at"]) > now + timedelta(minutes=5)
               or stamp(row["end"]) <= now - RETENTION
               or stamp(row["start"]) >= now + timedelta(days=366) for row in rows):
            raise ValueError("Check the source clock and keep travel within the supported past 30 days and next year.")
        try:
            with self.storage._locked():
                previous = self._read()
                retained = [row for row in previous if stamp(row["end"]) > now - RETENTION]
                merged = _resolve([*retained, *rows])
                if len(merged) > MAX_RECORDS:
                    raise ValueError("The private travel store is full. Old travel is removed after 30 days.")
                result = [merged[key] for key in sorted(merged)]
                if result != previous:
                    self.storage._write({"schema": 1, "reservations": result})
        except PersonalError:
            raise ValueError("The private travel store is unavailable. Check its directory and free space.") from None
        return {"reviewed": len(rows), "stored": len(result), "changed": result != previous}
