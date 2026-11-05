from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch

import pytest

from flightwall.commute import (CommuteStore, attach_commute_plan, lookup_schedules,
                                plan_commute, report_leg)
from flightwall.config import DEFAULTS
from flightwall.config import ConfigStore
from flightwall.feeds import FlightFeeds, PaidLimitError
from flightwall.trip_weather import preview_airports


NOW = datetime(2026, 11, 5, 17, tzinfo=timezone.utc)
ANCHOR = {"id": "synthetic-report", "number": "DL1946", "origin": "ATL", "destination": "SJU",
          "start": "2026-11-06T18:19:00+00:00", "end": "2026-11-06T21:49:00+00:00",
          "report": "2026-11-06T17:19:00+00:00", "deadhead": True}
SNAPSHOT = {"demo": False, "flights": [ANCHOR], "last_sync": NOW.isoformat(), "error": None}
CONFIG = DEFAULTS | {"demo": False, "presence_enabled": True, "flightaware_enabled": True,
                    "timezone": "America/Chicago", "commute_schedule_idents": '["DL2451","DL3051","DL2048"]'}
HOME = {"chris": {"state": "home", "stale": False, "last_seen": NOW.isoformat()}}


def candidates():
    return [{"number": number, "origin": "VPS", "destination": "ATL", "start": start, "end": end,
             "source_url": "https://tracker.flightview.com/synthetic?date=20261106", "checked_at": NOW.isoformat()}
            for number, start, end in (("DL2451", "2026-11-06T13:00:00+00:00", "2026-11-06T14:18:00+00:00"),
                                        ("DL3051", "2026-11-06T15:20:00+00:00", "2026-11-06T16:35:00+00:00"),
                                        ("DL2048", "2026-11-06T18:00:00+00:00", "2026-11-06T19:15:00+00:00"))]


def provider(ident):
    row = next(row for row in candidates() if row["number"] == ident)
    return {"flights": [{"ident_iata": ident, "scheduled_out": row["start"], "scheduled_in": row["end"],
                        "actual_out": "PRIVATE_ACTUAL_DO_NOT_COPY", "estimated_in": "PRIVATE_ESTIMATE_DO_NOT_COPY",
                        "origin": {"code_iata": "VPS"}, "destination": {"code_iata": "ATL"},
                        "crew": "PRIVATE_PROVIDER_FIELD"}]}


def test_latest_primary_leaves_later_backup_before_exact_report_without_buffer():
    plan = plan_commute(list(reversed(candidates())), ANCHOR, NOW)
    assert plan["status"] == "ready" and plan["viable_count"] == 2
    assert plan["primary"]["number"] == "DL2451" and plan["backup"]["number"] == "DL3051"
    assert plan["report_deadline"] == ANCHOR["report"]
    assert "seat availability unverified" in plan["basis"]
    rows = candidates()
    rows[1]["end"] = ANCHOR["report"]
    assert plan_commute(rows, ANCHOR, NOW)["status"] == "ready"
    rows[1]["end"] = "2026-11-06T17:19:01+00:00"
    assert plan_commute(rows, ANCHOR, NOW)["status"] == "insufficient_schedule"


def test_missing_report_stale_source_and_conflicting_schedule_never_invent_plan():
    assert plan_commute(candidates(), ANCHOR | {"report": None}, NOW)["status"] == "missing_report"
    assert plan_commute(candidates(), ANCHOR, NOW + timedelta(days=1, hours=1))["status"] == "expired"
    for clock in (NOW - timedelta(hours=48), NOW + timedelta(seconds=1)):
        rows = candidates()
        rows[0]["checked_at"] = clock.isoformat()
        assert plan_commute(rows, ANCHOR, NOW)["status"] == "schedule_stale"
    rows = candidates()[:2]
    rows.append(rows[1] | {"end": "2026-11-06T16:36:00+00:00"})
    assert plan_commute(rows, ANCHOR, NOW)["status"] == "ambiguous_schedule"
    with pytest.raises(ValueError):
        plan_commute([candidates()[0] | {"description": "PRIVATE"}], ANCHOR, NOW)


def test_private_plan_invalidates_on_report_change_and_only_adds_preview_windows(tmp_path):
    store = CommuteStore(tmp_path)
    anchor = report_leg(SNAPSHOT, "2026-11-06", NOW)
    store.save(candidates(), anchor, NOW)
    original = deepcopy(SNAPSHOT)
    projected = attach_commute_plan(SNAPSHOT, tmp_path, CONFIG, NOW)
    assert projected["flights"] == SNAPSHOT["flights"] and SNAPSHOT == original
    airports = preview_airports(projected, CONFIG, NOW)
    assert [row["iata"] for row in airports] == ["VPS", "ATL", "SJU"]
    assert [row["commute_role"] for row in airports[0]["windows"]] == ["primary", "backup"]
    assert airports[0]["windows"][0]["scheduled_at"] == candidates()[0]["start"]
    changed = SNAPSHOT | {"flights": [ANCHOR | {"report": "2026-11-06T17:20:00+00:00"}]}
    assert store.status(changed, CONFIG, NOW)["status"] == "report_changed"
    assert not preview_airports(attach_commute_plan(changed, tmp_path, CONFIG, NOW), CONFIG, NOW)[0].get("windows", [{}])[0].get("commute_role")


def test_error_missing_or_expired_roster_blocks_saved_plan_and_automatic_lookup(tmp_path):
    store, feeds = CommuteStore(tmp_path), Mock()
    store.save(candidates(), report_leg(SNAPSHOT, "2026-11-06", NOW), NOW)
    for changed in (SNAPSHOT | {"error": "calendar unavailable"}, SNAPSHOT | {"last_sync": None},
                    SNAPSHOT | {"last_sync": (NOW - timedelta(minutes=31)).isoformat()}):
        assert report_leg(changed, "2026-11-06", NOW, CONFIG) is None
        assert store.status(changed, CONFIG, NOW)["status"] != "ready"
        assert store.poll(changed, CONFIG, feeds, NOW, presence=HOME)["status"] != "ready"
    feeds._flightaware.assert_not_called()
    assert not store.usage.exists()


def test_direct_lookup_uses_one_existing_priced_route_per_ident_and_scheduled_only():
    feeds = Mock()
    feeds._flightaware.side_effect = lambda path, config: provider(path.split("/")[-1].split("?")[0])
    rows = lookup_schedules(feeds, CONFIG, "2026-11-06", ["DL2451", "DL3051"], NOW)
    assert len(rows) == feeds._flightaware.call_count == 2
    assert all("max_pages=1" in call.args[0] and call.args[0].startswith("/flights/DL") for call in feeds._flightaware.call_args_list)
    assert "PRIVATE" not in json.dumps(rows)
    feeds._flightaware.reset_mock()
    with pytest.raises(ValueError):
        lookup_schedules(feeds, CONFIG, "2026-11-06", ["DL2451"] * 9, NOW)
    feeds._flightaware.assert_not_called()


def test_automatic_refresh_reserves_before_network_and_survives_restart(tmp_path):
    store = CommuteStore(tmp_path)
    feeds = Mock()
    def lookup(path, config):
        assert store.usage.is_file()
        assert len(json.loads(store.usage.read_text())["anchors"]) == 1
        return provider(path.split("/")[-1].split("?")[0])
    feeds._flightaware.side_effect = lookup
    assert store.poll(SNAPSHOT, CONFIG, feeds, NOW, presence=HOME)["status"] == "ready"
    assert feeds._flightaware.call_count == 3
    assert CommuteStore(tmp_path).poll(SNAPSHOT, CONFIG, feeds, NOW, presence=HOME)["status"] == "ready"
    assert feeds._flightaware.call_count == 3
    assert store.status(SNAPSHOT, CONFIG | {"commute_schedule_idents": '["DL2451"]'}, NOW)["status"] == "selection_changed"
    assert "PRIVATE" not in store.path.read_text()


def test_empty_ids_or_unknown_presence_make_no_files_and_no_paid_calls(tmp_path):
    store, feeds = CommuteStore(tmp_path), Mock()
    store.poll(SNAPSHOT, CONFIG | {"commute_schedule_idents": "[]"}, feeds, NOW, presence=HOME)
    store.poll(SNAPSHOT, CONFIG, feeds, NOW, presence={})
    assert not list(tmp_path.iterdir())
    feeds._flightaware.assert_not_called()


def test_same_day_verified_manual_plan_avoids_redundant_paid_refresh(tmp_path):
    store, feeds = CommuteStore(tmp_path), Mock()
    store.save(candidates(), report_leg(SNAPSHOT, "2026-11-06", NOW), NOW)
    assert store.poll(SNAPSHOT, CONFIG, feeds, NOW, presence=HOME)["status"] == "ready"
    assert not store.usage.exists()
    feeds._flightaware.assert_not_called()


def test_automatic_config_is_local_only_and_future_attempt_clock_fails_closed(tmp_path):
    from flightwall.config import REMOTE_SETTINGS
    config = ConfigStore(tmp_path)
    config.update({"commute_schedule_idents": ["DL2451", "DL3051"]})
    assert config.get()["commute_schedule_idents"] == '["DL2451","DL3051"]'
    assert "commute_schedule_idents" not in config.public() and "commute_schedule_idents" not in REMOTE_SETTINGS
    with pytest.raises(ValueError):
        config.update({"commute_schedule_idents": '["AA123"]'})
    store, feeds = CommuteStore(tmp_path), Mock()
    store.usage.write_text(json.dumps({"day": "2026-11-06", "anchors": []}))
    assert store.poll(SNAPSHOT, CONFIG, feeds, NOW, presence=HOME)["status"] == "usage_unknown"
    feeds._flightaware.assert_not_called()


def test_schedule_cli_helper_keeps_existing_monthly_budget_and_route_fee(tmp_path):
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    feed = FlightFeeds(tmp_path)
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch("flightwall.feeds.request_json", return_value=provider("DL2451")) as network:
        rows = lookup_schedules(feed, CONFIG, "2026-11-06", ["DL2451"], NOW)
        assert len(rows) == 1 and network.call_count == 1
    usage = json.loads((tmp_path / "flightaware-usage.json").read_text())
    assert usage["count"] == 1 and usage["reserved_milliusd"] == 5
    with patch("flightwall.feeds.utcnow", return_value=NOW), patch("flightwall.feeds.request_json") as network:
        with pytest.raises(PaidLimitError):
            lookup_schedules(feed, CONFIG | {"flightaware_monthly_limit_usd": 0}, "2026-11-06", ["DL3051"], NOW)
        network.assert_not_called()


def test_native_cli_saves_exact_plan_and_fetches_only_normalized_schedules(tmp_path, capsys):
    from flightwall.main import main
    config = ConfigStore(tmp_path)
    config.update({"demo": False, "flightaware_enabled": True})
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(candidates()))
    with patch("flightwall.backend.Runtime.snapshot", return_value=SNAPSHOT), patch("flightwall.main.datetime") as clock:
        clock.now.return_value = NOW
        code = main(["--data-dir", str(tmp_path), "commute-plan", "--date", "2026-11-06", "--candidates", str(path)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["primary"]["number"] == "DL2451"
    with patch("flightwall.feeds.FlightFeeds._flightaware", return_value=provider("DL2451")) as request, patch("flightwall.main.datetime") as clock:
        clock.now.return_value = NOW
        code = main(["--data-dir", str(tmp_path), "commute-schedules", "--date", "2026-11-06", "--idents", "DL2451"])
    rows = json.loads(capsys.readouterr().out)
    assert code == 0 and request.call_count == 1 and rows[0]["number"] == "DL2451"
    assert "PRIVATE" not in repr(rows)


def test_runtime_projection_and_worker_attach_commute_without_changing_roster(tmp_path):
    from flightwall.backend import Runtime
    runtime = Runtime(tmp_path, tmp_path / "install")
    runtime.config.update({"demo": False, "flightaware_enabled": True, "presence_enabled": True, "trip_weather_enabled": True,
                           "timezone": "America/Chicago", "commute_schedule_idents": CONFIG["commute_schedule_idents"]})
    runtime.commute.save(candidates(), report_leg(SNAPSHOT, "2026-11-06", NOW), NOW)
    with patch.object(runtime, "snapshot", return_value=SNAPSHOT):
        status = runtime.display_status(NOW)
    assert status["snapshot"]["flights"] == SNAPSHOT["flights"]
    assert status["snapshot"]["commute_plan"]["primary"]["number"] == "DL2451"
    with patch.object(runtime, "snapshot", return_value=SNAPSHOT), patch.object(runtime.presence, "snapshot", return_value=HOME), \
            patch.object(runtime.commute, "poll") as poll, patch.object(runtime.trip_weather, "poll") as weather, \
            patch.object(runtime.stop_event, "is_set", side_effect=[False, True]), patch.object(runtime.stop_event, "wait"), \
            patch("flightwall.backend.datetime") as clock:
        clock.now.return_value = NOW
        runtime._run_trip_weather()
    assert poll.call_count == 1 and poll.call_args.kwargs["presence"] == HOME
    assert weather.call_args.args[0]["commute_plan"]["status"] == "ready"
    assert weather.call_args.kwargs["preview"] is True
