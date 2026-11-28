from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

from flightwall.backend import Runtime
from flightwall.config import DEFAULTS
from flightwall.home_briefing import HomeBriefing, SEED_FILE, visible_sections


NOW = datetime(2026, 11, 28, 5, 45, tzinfo=timezone.utc)
ZONE = "America/Chicago"
CFG = DEFAULTS | {"demo": False, "presence_enabled": True, "home_briefing_enabled": True,
                  "guest_ops_enabled": True, "timezone": ZONE}


def four_days(today="2026-11-27"):
    day = datetime.fromisoformat(today).date()
    return [{"date": (day + timedelta(days=i)).isoformat(), "arrivals": i, "departures": 0}
            for i in range(4)]


def test_seed_projects_four_days_with_no_private_fields_and_expires(tmp_path):
    rows = four_days()
    rows[0]["sensitive"] = "synthetic private data"
    (tmp_path / SEED_FILE).write_text(json.dumps({"synced": NOW.isoformat(), "rows": rows}), encoding="utf-8")
    source = HomeBriefing(tmp_path, lambda: CFG)
    actual = source.snapshot(NOW, CFG)
    assert actual["guest_ops"]["rows"] == four_days()
    assert not source.snapshot(NOW + timedelta(minutes=15), CFG)
    assert not source.snapshot(NOW, CFG | {"guest_ops_enabled": False})
    operations = {"rows": four_days("2026-11-28"), "synced": NOW.isoformat()}
    assert not visible_sections({"guest_ops": operations}, NOW + timedelta(hours=2), ZONE)


def test_provider_results_never_survive_account_change_or_use_private_identifiers(tmp_path):
    source = HomeBriefing(tmp_path, lambda: CFG)
    key = tmp_path / "hospitable.key"
    key.write_text("test-credential-one", encoding="utf-8")
    with patch("flightwall.home_briefing.PUBLIC_SECTIONS", {}), \
            patch("flightwall.home_briefing.fetch_operations", return_value=four_days()) as fetch:
        source.refresh(NOW)
        source.refresh(NOW + timedelta(minutes=1))
        assert fetch.call_count == 1
        assert source.snapshot(NOW, CFG)["guest_ops"]["rows"] == four_days()
        key.write_text("test-credential-two", encoding="utf-8")
        assert source.snapshot(NOW, CFG) == {}
        source.refresh(NOW + timedelta(minutes=1))
        assert fetch.call_count == 2
    assert "binding" not in json.dumps(source.snapshot(NOW + timedelta(minutes=1), CFG))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hospitable.key"]


def test_maintenance_status_excludes_home_briefing(tmp_path):
    runtime = Runtime(tmp_path, tmp_path / "install")
    sample = runtime.display_status(NOW) | {"home_briefing": {"guest_ops": {"rows": four_days()}}}
    with patch.object(runtime, "display_status", return_value=sample):
        assert "home_briefing" not in runtime.status(NOW)
