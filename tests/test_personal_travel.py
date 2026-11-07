"""Verify the private reservation import through the owner and TV entry points."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

from flightwall.backend import Runtime
from flightwall.display import entry_render_state
from flightwall.household_policy import build_rotation
from flightwall.main import main
from flightwall.personal_travel import PersonalTravel
from flightwall.render import render_frame


def record(now, **changes):
    departure = now.replace(microsecond=0) + timedelta(days=2)
    return dict(
        number="DL1234", origin="ATL", destination="MEM",
        start=departure.isoformat(), end=(departure + timedelta(hours=2)).isoformat(),
        status="confirmed", source="delta_email",
        updated_at=(now - timedelta(days=1)).isoformat(),
    ) | changes


def save(path, records):
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_cli_reservation_joins_tv_rotation_without_becoming_operating_or_maintenance_data(tmp_path, capsys):
    now = datetime.now(timezone.utc)
    data = tmp_path / "data"
    confirmed = record(now)
    filename = save(tmp_path / "reviewed.json", [
        confirmed,
        record(now, number="DL1235", status="listed", source="calendar_copy"),
        record(now, number="DL1236", status="jumpseat_requested"),
    ])
    command = ["--data-dir", str(data)]
    assert main([*command, "travel-import", "--reservations", str(filename)]) == 0
    assert json.loads(capsys.readouterr().out) == {"reviewed": 3, "stored": 3, "changed": True}
    assert main([*command, "travel-list"]) == 0
    assert len(json.loads(capsys.readouterr().out)["reservations"]) == 3

    runtime = Runtime(data, tmp_path / "install")
    assert runtime.display_status(now)["personal_travel"] == []
    runtime.config.update({"demo": False})
    config = runtime.config.get()
    status = runtime.display_status(now)
    assert status["personal_travel"] == [confirmed]
    assert status["snapshot"]["flights"] == []
    assert status["operating_flight"] is None
    assert status["on_duty"] is False
    assert "personal_travel" not in runtime.status(now)

    slot, = [entry for entry in build_rotation(status, config, now) if entry["view"] == "personal_travel"]
    state = entry_render_state(status, config, slot, now)
    with patch("flightwall.render._text") as text:
        render_frame(state, config)
    assert {"PERSONAL DELTA TRAVEL", "DL1234 / ATL > MEM", "CONFIRMED"}.issubset(
        {call.args[2] for call in text.call_args_list})
    for context, settings in ((status, config | {"guest_mode": True}),
                              (status | {"backend_available": False}, config)):
        assert not any(entry["view"] == "personal_travel" for entry in build_rotation(context, settings, now))
        retained_state = entry_render_state(context, settings, slot, now)
        with patch("flightwall.render._text") as text:
            render_frame(retained_state, settings)
        assert not any("DL1234" in call.args[2] for call in text.call_args_list)
    at_end = datetime.fromisoformat(confirmed["end"])
    assert not any(entry["view"] == "personal_travel" for entry in build_rotation(status, config, at_end))


def test_latest_delta_revision_supersedes_listing_and_stale_replays(tmp_path):
    now = datetime(2026, 11, 7, 17, tzinfo=timezone.utc)
    store = PersonalTravel(tmp_path / "data", lambda: {})
    file = tmp_path / "reviewed.json"
    first = record(now)
    copy = dict(first, status="listed", source="calendar_copy", updated_at=now.isoformat())
    assert store.import_file(save(file, [copy, first]), now)["stored"] == 1
    assert store.list_records() == [first]
    revised = dict(first, updated_at=(now - timedelta(hours=1)).isoformat(),
                   start=(now + timedelta(days=2, hours=1)).isoformat(),
                   end=(now + timedelta(days=2, hours=3)).isoformat())
    assert store.import_file(save(file, [revised, copy, first]), now)["changed"]
    assert store.list_records() == [revised]
    cancellation = dict(revised, status="cancelled", updated_at=now.isoformat())
    assert store.import_file(save(file, [cancellation]), now)["changed"]
    assert not store.import_file(save(file, [first, copy, revised]), now)["changed"]
    assert PersonalTravel(tmp_path / "data", lambda: {}).list_records() == [cancellation]
    with pytest.raises(ValueError, match="Conflicting travel records"):
        store.import_file(save(file, [dict(cancellation, status="confirmed"), record(now, number="DL4321")]), now)
    assert store.list_records() == [cancellation]


def test_import_rejects_sensitive_fields_and_calendar_as_confirmation(tmp_path):
    now = datetime(2026, 11, 7, 17, tzinfo=timezone.utc)
    store = PersonalTravel(tmp_path / "data", lambda: {})
    for invalid in (record(now, record_locator="SAMPLE"),
                    record(now, source="calendar_copy"),
                    record(now, start="2026-11-10T13:00:00")):
        with pytest.raises(ValueError):
            store.import_file(save(tmp_path / "reviewed.json", [record(now), invalid]), now)
        assert store.list_records() == []
