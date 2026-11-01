"""Optional menu metadata never poisons the old recovery release's core store."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import stat
import threading
from unittest.mock import patch

import pytest

from flightwall.personal import PersonalError, PersonalStore

NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
DETAILS = {"sides", "ready_at", "ready_until"}
PAYLOAD = {"kind": "dinner", "title": "Fictional pasta", "sides": "Fictional peas", "ready_time": "18:45",
           "request_id": "fictional-rich-retry"}


@pytest.fixture
def store(tmp_path):
    config = {"timezone": "America/Chicago", "dinner_start": "14:30", "dinner_end": "19:00"}
    with patch("flightwall.personal._now", return_value=NOW):
        yield PersonalStore(tmp_path, lambda: dict(config))


def core(store):
    return json.loads(store.path.read_text())


def test_core_and_request_snapshots_stay_old_format_while_details_bind_opaque_id(store):
    item = store.create(PAYLOAD)["item"]
    state = core(store)
    assert not DETAILS.intersection(state["items"][0])
    assert all(not DETAILS.intersection(record["item"]) for record in state["requests"].values())
    document = json.loads(store.details_path.read_text())
    assert document["schema_version"] == 1 and set(document["items"]) == {item["id"]}
    entry = document["items"][item["id"]]
    assert entry["core_sha256"] == store._core_fingerprint(state["items"][0])
    assert entry["sides"] == item["sides"] and entry["ready_at"] == item["ready_at"]
    assert "fictional-rich-retry" not in store.details_path.read_text()
    assert store.create(PAYLOAD)["item"] == item
    if os.name == "posix":
        assert stat.S_IMODE(store.details_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("damage", ["missing", "broken", "array", "deep", "unknown-field", "wrong-hash"])
def test_missing_or_corrupt_sidecar_hides_details_but_base_menu_remains_readable(store, damage):
    item = store.create(PAYLOAD)["item"]
    state_before = store.path.read_bytes()
    document = json.loads(store.details_path.read_text())
    if damage == "missing":
        store.details_path.unlink()
    elif damage == "broken":
        store.details_path.write_text("not json")
    elif damage == "array":
        store.details_path.write_text("[]")
    elif damage == "deep":
        store.details_path.write_text("[" * 2000 + "]" * 2000)
    else:
        entry = document["items"][item["id"]]
        if damage == "unknown-field":
            entry["description"] = "PRIVATE-RAW-DATA"
        else:
            entry["core_sha256"] = "0" * 64
        store.details_path.write_text(json.dumps(document))
    result = store.list_items(NOW)
    assert len(result) == 1 and result[0]["title"] == "Fictional pasta"
    assert not DETAILS.intersection(result[0]) and "PRIVATE" not in json.dumps(result)
    assert store.path.read_bytes() == state_before


def test_core_change_cannot_inherit_details_but_cancellation_does_not_rebind_them(store):
    item = store.create(PAYLOAD)["item"]
    cancelled = store.cancel(item["id"])["item"]
    assert cancelled["cancelled"] and cancelled["sides"] == item["sides"]
    assert store.visible(NOW) == []
    state = core(store)
    state["items"][0].update(cancelled=False, title="Fictional changed core")
    state["items"][0].pop("cancelled_at", None)
    store.path.write_text(json.dumps(state))
    changed = store.list_items(NOW)[0]
    assert changed["title"] == "Fictional changed core" and not DETAILS.intersection(changed)


def test_metadata_write_failure_cannot_replace_the_existing_core_menu(store):
    original = store.create({"kind": "dinner", "title": "Fictional original"})["item"]
    before = store.path.read_bytes()
    real_write = store._write_document
    def fail_details(path, document):
        if path == store.details_path:
            raise PersonalError("Fictional storage failure")
        return real_write(path, document)
    with patch.object(store, "_write_document", side_effect=fail_details), pytest.raises(PersonalError):
        store.create(PAYLOAD)
    assert store.path.read_bytes() == before and store.list_items(NOW) == [original]


def test_core_failure_leaves_only_an_invisible_orphan_then_retry_prunes_it(store):
    original = store.create({"kind": "dinner", "title": "Fictional original"})["item"]
    before = store.path.read_bytes()
    with patch.object(store, "_write", side_effect=PersonalError("Fictional core failure")), pytest.raises(PersonalError):
        store.create(PAYLOAD)
    orphan = next(iter(json.loads(store.details_path.read_text())["items"]))
    assert store.path.read_bytes() == before and store.list_items(NOW) == [original]
    saved = store.create(PAYLOAD)["item"]
    entries = json.loads(store.details_path.read_text())["items"]
    assert set(entries) == {saved["id"]} and orphan != saved["id"]
    assert store.create(PAYLOAD)["item"] == saved


def test_corrupt_sidecar_blocks_rich_overwrite_but_not_plain_core_operations(store):
    original = store.create({"kind": "dinner", "title": "Fictional original"})["item"]
    store.details_path.write_text("PRIVATE broken details")
    before = store.path.read_bytes()
    with pytest.raises(PersonalError) as error:
        store.create(PAYLOAD)
    assert "PRIVATE" not in str(error.value) and store.path.read_bytes() == before
    plain = store.create({"kind": "dinner", "title": "Fictional plain replacement"})["item"]
    assert store.list_items(NOW) == [plain]
    assert store.details_path.read_text() == "PRIVATE broken details"
    assert plain["id"] != original["id"]


def test_detail_byte_limit_fails_before_replacing_core_menu(store):
    original = store.create({"kind": "dinner", "title": "Fictional original"})["item"]
    before = store.path.read_bytes()
    with patch("flightwall.personal.MAX_DETAILS_BYTES", 100), pytest.raises(PersonalError, match="history is full"):
        store.create(PAYLOAD)
    assert store.path.read_bytes() == before and store.list_items(NOW) == [original]
    assert not store.details_path.exists()


def test_concurrent_rich_retry_is_one_core_item_and_one_bound_detail_entry(store):
    barrier = threading.Barrier(4)
    def save(_):
        other = PersonalStore(store.data_dir, store.config_getter)
        barrier.wait(timeout=5)
        return other.create(PAYLOAD)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(save, range(4)))
    assert sum(result["created"] for result in results) == 1
    assert len({result["item"]["id"] for result in results}) == 1
    assert all(result["item"]["sides"] == "Fictional peas" for result in results)
    assert len(core(store)["items"]) == len(json.loads(store.details_path.read_text())["items"]) == 1
