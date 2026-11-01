"""Exercise optional dinner metadata against the exact published 0.4.7 store.

The frozen public source is extracted from flightwall-source-0.4.7.zip. It
models the startup rollback reader rather than a reimplementation of it.
"""
from datetime import datetime, timezone
import hashlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path
from unittest.mock import patch

from flightwall.personal import PersonalStore


NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
FROZEN_SHA256 = "e696cab1de00e8fe74ec648b9cbcce8443f7d8254eb2e03a6260c6ca9aae3cb8"
EXTRAS = {"sides", "ready_at", "ready_until"}


def published_store_module():
    fixture = Path(__file__).parent / "fixtures" / "personal-0.4.7.py.txt"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == FROZEN_SHA256
    loader = SourceFileLoader("_published_flightwall_personal_047", str(fixture))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def assert_core_is_047_readable(path, legacy):
    document = json.loads(path.read_text(encoding="utf-8"))
    core_items = document["items"] + [record["item"] for record in document["requests"].values()]
    assert core_items
    for item in core_items:
        assert not EXTRAS.intersection(item)
        legacy.PersonalStore._check_item(item)


def test_published_047_can_list_create_cancel_after_048_rich_dinners(tmp_path):
    legacy = published_store_module()
    config = {"timezone": "America/Chicago", "dinner_start": "14:30", "dinner_end": "19:00"}
    current = PersonalStore(tmp_path, lambda: dict(config))
    with patch("flightwall.personal._now", return_value=NOW), patch.object(legacy, "_now", return_value=NOW):
        today = current.create({"kind": "dinner", "title": "Fictional tacos", "date": "2026-11-01",
                                "sides": "Fictional rice", "ready_time": "18:45",
                                "request_id": "fictional-rich-today"})["item"]
        tomorrow_payload = {"kind": "dinner", "title": "Fictional teriyaki", "date": "2026-11-02",
                            "sides": "Fictional peas", "ready_window_start": "18:45",
                            "ready_window_end": "19:15", "request_id": "fictional-rich-tomorrow"}
        tomorrow = current.create(tomorrow_payload)["item"]
        details_before = current.details_path.read_bytes()
        assert_core_is_047_readable(current.path, legacy)

        # An actual old-release reader accepts every core row and ledger
        # snapshot; its unaware writes must preserve the independent details.
        old = legacy.PersonalStore(tmp_path, lambda: dict(config))
        listed = old.list_items(NOW)
        assert [item["id"] for item in listed] == [today["id"], tomorrow["id"]]
        assert all(not EXTRAS.intersection(item) for item in listed)
        appointment_payload = {"kind": "appointment", "title": "Fictional haircut", "date": "2026-11-02",
                               "time": "10:00", "request_id": "fictional-old-appointment"}
        appointment = old.create(appointment_payload)["item"]
        assert old.create(appointment_payload)["created"] is False
        assert old.cancel(today["id"])["cancelled"] is True
        assert [item["id"] for item in old.list_items(NOW)] == [appointment["id"], tomorrow["id"]]
        assert current.details_path.read_bytes() == details_before
        assert_core_is_047_readable(current.path, legacy)

        # Upgrading again still binds rich metadata to exactly its original
        # core card and preserves the cancellation written by old recovery.
        reopened = PersonalStore(tmp_path, lambda: dict(config))
        items = {item["id"]: item for item in reopened.list_items(NOW, include_cancelled=True)}
        assert items[today["id"]]["cancelled"] is True
        assert items[today["id"]]["sides"] == today["sides"]
        assert items[tomorrow["id"]] == tomorrow
        assert items[appointment["id"]] == appointment
        retried = reopened.create(tomorrow_payload)
        assert retried["created"] is False and retried["item"] == tomorrow
        assert_core_is_047_readable(current.path, legacy)
