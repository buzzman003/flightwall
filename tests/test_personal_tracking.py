"""Lisa's exact personal flight targets never become duty/presence evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from flightwall.config import DEFAULTS
from flightwall.feeds import FlightFeeds
from flightwall.flight_alerts import normalize_operating_status
from flightwall.personal_tracking import (PersonalFlightTracker, fresh_personal_tracking,
    match_personal_flight, personal_tracking_targets, personal_tracking_window)


NOW = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
CONFIG = DEFAULTS | {"demo": False, "guest_mode": False, "flightaware_enabled": True,
                     "personal_travel_cards_visible": True, "lisa_flight_tracking_enabled": True,
                     "nearby_enabled": False, "flightaware_poll_seconds": 120}


def record(**changes):
    return {"traveler": "lisa", "number": "DL123", "origin": "VPS", "destination": "ATL",
            "start": (NOW - timedelta(minutes=30)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat(),
            "updated_at": (NOW - timedelta(days=1)).isoformat(), "source": "delta_email", "status": "listed", **changes}


def flight(**changes):
    return personal_tracking_targets([record(**changes)])[0]


def provider(**changes):
    return {"fa_flight_id": "DAL123-test", "ident": "DAL123", "ident_iata": "DL123",
            "origin": {"code_iata": "VPS"}, "destination": {"code_iata": "ATL"},
            "scheduled_out": record()["start"], "scheduled_in": record()["end"],
            "actual_off": (NOW - timedelta(minutes=10)).isoformat(),
            "actual_in": None, "cancelled": False, "diverted": False, **changes}


def data(*items, **changes):
    return {"flights": list(items or (provider(),)), "links": {"next": None}, "num_pages": 1, **changes}


def position(now=NOW, **changes):
    return {"fa_flight_id": "DAL123-test", "last_position": {
        "latitude": 32.0, "longitude": -85.0, "timestamp": now.isoformat(),
        "altitude": 350, "groundspeed": 420, "heading": 45, **changes}}


def setup(tmp_path, responses=()):
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    request = Mock(side_effect=list(responses))
    return PersonalFlightTracker(request, tmp_path), request


def tracking_row(now=NOW, reservation_status="listed"):
    target = flight(status=reservation_status)
    status = normalize_operating_status(provider(), target, now)
    live = {"latitude": 32.0, "longitude": -85.0, "observed_at": now.isoformat(), "flight_id": target["id"],
            "provider_flight_id": "DAL123-test", "altitude": 35000, "altitude_units": "feet",
            "groundspeed": 420, "heading": 45, "source": status["source"]}
    return {"id": target["id"], "traveler": "lisa", "reservation_status": reservation_status,
            "flight": target, "status": "tracking", "provider": status, "live": live}


def test_targets_accept_only_narrow_lisa_listed_or_confirmed_legs_and_preserve_labels():
    assert personal_tracking_targets([record(traveler="chris")]) == []
    for status in ("cancelled", "flown", "jumpseat_requested"):
        assert personal_tracking_targets([record(status=status)]) == []
    for changes in ({"description": "PRIVATE-BODY"}, {"locator": "PRIVATE"}, {"origin": None},
                    {"start": "2026-11-17T17:30:00"}, {"number": "123"}):
        assert personal_tracking_targets([record(**changes)]) == []
    listed, confirmed = flight(), flight(status="confirmed")
    assert listed["id"] == confirmed["id"]
    assert listed["reservation_status"] == "listed" and confirmed["reservation_status"] == "confirmed"
    assert "updated_at" not in listed and "PRIVATE" not in json.dumps(listed)
    assert personal_tracking_targets([record(), record(status="confirmed")]) == []
    assert flight(start=(NOW - timedelta(minutes=29)).isoformat())["id"] != listed["id"]


def test_provider_requires_exact_number_route_and_start_not_nearest_or_other_day():
    assert match_personal_flight(data(), flight())[0] == "matched"
    for changes in ({"ident": "AA123", "ident_iata": "AA123"}, {"origin": {"code_iata": "PNS"}},
                    {"destination": {"code_iata": "FAY"}}, {"scheduled_out": (NOW - timedelta(minutes=29)).isoformat()},
                    {"scheduled_out": (NOW - timedelta(days=1, minutes=30)).isoformat()},
                    {"fa_flight_id": "../PRIVATE"}, {"ident_iata": {"bad": "value"}}):
        assert match_personal_flight(data(provider(**changes)), flight()) == ("not_found", None)
    assert match_personal_flight(data(provider(), provider(fa_flight_id="second")), flight())[0] == "ambiguous"
    for changes in ({"links": {"next": "https://invalid"}}, {"num_pages": 2}, {"num_pages": True}):
        assert match_personal_flight(data(**changes), flight())[0] == "ambiguous"


@pytest.mark.parametrize("changes", [
    {"lisa_flight_tracking_enabled": False}, {"flightaware_enabled": False}, {"demo": True},
    {"guest_mode": True}, {"personal_travel_cards_visible": False},
])
def test_no_optin_or_hidden_private_display_never_polls_or_publishes(tmp_path, changes):
    tracker, request = setup(tmp_path)
    tracker.poll(CONFIG | changes, [record()], NOW, 0)
    request.assert_not_called()
    assert tracker.snapshot(CONFIG | changes, [record()], NOW) == []
    assert fresh_personal_tracking([tracking_row()], CONFIG | changes, NOW) == []


def test_missing_key_distant_flights_and_phone_states_do_not_trigger_paid_lookups(tmp_path):
    tracker, request = setup(tmp_path)
    for days in (1, 14):
        future = record(start=(NOW + timedelta(days=days)).isoformat(), end=(NOW + timedelta(days=days, hours=2)).isoformat())
        tracker.poll(CONFIG, [future], NOW, 0)
        assert tracker.snapshot(CONFIG, [future], NOW) == []
    request.assert_not_called()
    (tmp_path / "flightaware.key").unlink()
    tracker.poll(CONFIG, [record()], NOW, 0)
    request.assert_not_called()
    assert tracker.snapshot(CONFIG, [record()], NOW)[0]["status"] == "unconfigured"


def test_one_request_per_interval_alternates_exact_status_and_position_with_shared_api_paths(tmp_path):
    tracker, request = setup(tmp_path, [data(provider(crew="PRIVATE-CREW", locator="PRIVATE-LOCATOR")),
                                        position(NOW + timedelta(seconds=120)), data()])
    tracker.poll(CONFIG, [record()], NOW, 0)
    assert request.call_count == 1
    query = urlsplit(request.call_args.args[0])
    assert query.path == "/flights/DL123"
    assert parse_qs(query.query)["max_pages"] == ["1"]
    first = tracker.snapshot(CONFIG, [record()], NOW)[0]
    assert first["status"] == "scheduled" and first["reservation_status"] == "listed"
    assert "PRIVATE" not in json.dumps(first)
    tracker.poll(CONFIG, [record()], NOW + timedelta(seconds=119), 119)
    assert request.call_count == 1
    tracker.poll(CONFIG, [record()], NOW + timedelta(seconds=120), 120)
    assert request.call_count == 2 and request.call_args.args[0] == "/flights/DAL123-test/position"
    current = tracker.snapshot(CONFIG, [record()], NOW + timedelta(seconds=120))[0]
    assert current["status"] == "tracking" and current["live"]["altitude"] == 35000
    assert current["live"]["altitude_units"] == "feet" and current["reservation_status"] == "listed"
    tracker.poll(CONFIG, [record()], NOW + timedelta(seconds=240), 240)
    assert request.call_count == 3 and request.call_args.args[0].startswith("/flights/DL123?")


def test_revised_cancelled_or_removed_reservation_cannot_reuse_old_aircraft(tmp_path):
    tracker, request = setup(tmp_path, [data(), position(NOW + timedelta(seconds=120))])
    tracker.poll(CONFIG, [record()], NOW, 0)
    tracker.poll(CONFIG, [record()], NOW + timedelta(seconds=120), 120)
    assert tracker.snapshot(CONFIG, [record()], NOW + timedelta(seconds=120))[0].get("live")
    changed = record(destination="FAY")
    assert tracker.snapshot(CONFIG, [changed], NOW + timedelta(seconds=120))[0]["status"] == "pending"
    assert tracker.snapshot(CONFIG, [record(status="cancelled")], NOW + timedelta(seconds=120)) == []
    assert tracker.snapshot(CONFIG, [], NOW + timedelta(seconds=120)) == []


@pytest.mark.parametrize("changes", [
    {"timestamp": (NOW - timedelta(seconds=301)).isoformat()},
    {"timestamp": (NOW + timedelta(seconds=1)).isoformat()}, {"latitude": True}, {"latitude": float("nan")},
    {"longitude": 181},
])
def test_invalid_or_stale_position_is_never_live(tmp_path, changes):
    tracker, request = setup(tmp_path, [data(), position(**changes)])
    tracker.poll(CONFIG, [record()], NOW, 0)
    tracker.poll(CONFIG, [record()], NOW, 120)
    result = tracker.snapshot(CONFIG, [record()], NOW)[0]
    assert result["status"] == "scheduled" and "live" not in result


def test_wrong_provider_position_association_is_rejected(tmp_path):
    tracker, request = setup(tmp_path, [data(), position() | {"fa_flight_id": "OTHER"}])
    tracker.poll(CONFIG, [record()], NOW, 0)
    tracker.poll(CONFIG, [record()], NOW, 120)
    assert "live" not in tracker.snapshot(CONFIG, [record()], NOW)[0]


def test_perframe_staleness_offline_identity_and_altitude_units():
    row = tracking_row()
    original = deepcopy(row)
    assert fresh_personal_tracking([row], CONFIG, NOW + timedelta(seconds=300))[0]["live"]
    expired = fresh_personal_tracking([row], CONFIG, NOW + timedelta(seconds=301))[0]
    assert expired["status"] == "stale" and "live" not in expired
    assert fresh_personal_tracking([row], CONFIG, NOW, backend_available=False) == []
    assert fresh_personal_tracking([row | {"id": "different"}], CONFIG, NOW) == []
    wrong = deepcopy(row)
    wrong["provider"]["flight_id"] = "different"
    assert "live" not in fresh_personal_tracking([wrong], CONFIG, NOW)[0]
    wrong = deepcopy(row)
    wrong["live"]["altitude_units"] = "metres"
    assert fresh_personal_tracking([wrong], CONFIG, NOW)[0]["live"]["altitude"] is None
    assert row == original


@pytest.mark.parametrize("phase", ["landed", "at_gate", "blocked", "tracking_ended"])
def test_terminal_provider_phase_never_keeps_live_plane_marker(phase):
    row = tracking_row()
    row["provider"]["phase"] = phase
    assert "live" not in fresh_personal_tracking([row], CONFIG, NOW)[0]


def test_delay_window_is_observed_bounded_and_not_a_presence_or_boarding_inference():
    target = flight()
    end = datetime.fromisoformat(target["end"])
    assert personal_tracking_window(target, end + timedelta(hours=1))
    assert not personal_tracking_window(target, end + timedelta(hours=1, seconds=1))
    delayed_now = end + timedelta(hours=1, minutes=10)
    delayed = normalize_operating_status(provider(estimated_in=(end + timedelta(hours=5)).isoformat()), target, delayed_now)
    assert personal_tracking_window(target, delayed_now, delayed)
    assert not personal_tracking_window(target, end + timedelta(hours=2, seconds=1), delayed)
    assert personal_tracking_window(target, delayed_now + timedelta(seconds=301), delayed)
    future_observation = dict(delayed, fetched_at=(delayed_now + timedelta(seconds=1)).isoformat())
    assert not personal_tracking_window(target, delayed_now, future_observation)
    row = tracking_row()
    assert "presence" not in row and row["reservation_status"] == "listed"


def test_provider_failure_drops_live_and_backs_off_with_fixed_reason(tmp_path):
    tracker, request = setup(tmp_path, [data(), OSError("PRIVATE-KEY-URL")])
    tracker.poll(CONFIG, [record()], NOW, 0)
    tracker.poll(CONFIG, [record()], NOW + timedelta(seconds=120), 120)
    result = tracker.snapshot(CONFIG, [record()], NOW + timedelta(seconds=120))[0]
    assert result["status"] == "stale" and result["reason"] == "provider_unavailable" and "live" not in result
    assert "PRIVATE" not in json.dumps(result)
    tracker.poll(CONFIG, [record()], NOW + timedelta(seconds=240), 240)
    assert request.call_count == 2


def test_unmatched_or_ambiguous_leg_never_buys_position(tmp_path):
    tracker, request = setup(tmp_path, [data(provider(destination={"code_iata": "FAY"}))])
    tracker.poll(CONFIG, [record()], NOW, 0)
    assert tracker.snapshot(CONFIG, [record()], NOW)[0]["status"] == "not_found"
    tracker.poll(CONFIG, [record()], NOW + timedelta(seconds=120), 120)
    assert request.call_count == 1


@pytest.mark.parametrize(("number", "source"), [("AA310", "american_email"),
                                               ("B6310", "airline_email"),
                                               ("6E310", "airline_email")])
def test_marketed_booking_tracks_exact_provider_flight_or_codeshare(tmp_path, number, source):
    from flightwall.feeds import _paid_path

    trip = record(number=number, source=source, status="confirmed", origin="CLT", destination="STL")
    target, = personal_tracking_targets([trip])
    marketed = provider(ident="RPA4310", ident_iata="YX4310", codeshares_iata=[number],
                        origin={"code_iata": "CLT"}, destination={"code_iata": "STL"},
                        fa_flight_id="RPA4310-test")
    for bad in (dict(marketed, codeshares_iata=number), dict(marketed, scheduled_out=trip["end"]),
                dict(marketed, origin={"code_iata": "VPS"})):
        assert match_personal_flight(data(bad), target) == ("not_found", None)
    assert match_personal_flight(data(marketed, dict(marketed, fa_flight_id="other")), target)[0] == "ambiguous"

    later = NOW + timedelta(seconds=120)
    tracker, lookup = setup(tmp_path, [data(marketed), position(later) | {"fa_flight_id": "RPA4310-test"}])
    tracker.poll(CONFIG, [trip], NOW, 0)
    query = urlsplit(_paid_path(lookup.call_args.args[0])[0])
    assert query.path == "/flights/" + number
    assert parse_qs(query.query)["start"] == ["2026-11-17T16:30:00Z"]
    tracker.poll(CONFIG, [trip], later, 120)
    assert _paid_path(lookup.call_args.args[0])[0] == "/flights/RPA4310-test/position"
    observed, = tracker.snapshot(CONFIG, [trip], later)
    assert observed["status"] == "tracking" and observed["ident"] == number
    assert observed["reservation_status"] == "confirmed" and observed["traveler"] == "lisa"
    assert observed["live"]["provider_flight_id"] == "RPA4310-test"
    assert tracker.snapshot(CONFIG | {"guest_mode": True}, [trip], later) == []


def test_three_targets_pair_status_with_position_before_round_robin_so_fixes_are_fresh(tmp_path):
    records = [record(number=f"DL{123 + index}",
                      start=(NOW - timedelta(minutes=30 - index)).isoformat(),
                      end=(NOW + timedelta(minutes=60 + index)).isoformat()) for index in range(3)]
    tracker, request = setup(tmp_path)
    current = NOW
    def response(path, config):
        if path.endswith("/position"):
            return position(current) | {"fa_flight_id": path.split("/")[2]}
        number = urlsplit(path).path.split("/")[2]
        row = next(item for item in records if item["number"] == number)
        return data(provider(ident=number, ident_iata=number, fa_flight_id=number + "-test",
                             scheduled_out=row["start"], scheduled_in=row["end"]))
    request.side_effect = response
    for tick in (0, 120, 240, 360, 480, 600):
        current = NOW + timedelta(seconds=tick)
        tracker.poll(CONFIG, records, current, tick)
        count = request.call_count
        tracker.poll(CONFIG, records, current + timedelta(seconds=2), tick + 2)
        assert request.call_count == count
        if tick % 240 == 120:
            target_number = records[tick // 240]["number"]
            result = next(row for row in tracker.snapshot(CONFIG, records, current) if row["ident"] == target_number)
            assert result["status"] == "tracking" and result["live"]
            assert result["provider"]["status"] == "fresh"
    assert [urlsplit(call.args[0]).path for call in request.call_args_list] == [
        "/flights/DL123", "/flights/DL123-test/position", "/flights/DL124", "/flights/DL124-test/position",
        "/flights/DL125", "/flights/DL125-test/position"]


def test_previously_observed_delay_can_be_refreshed_after_freshness_expires_but_not_past_hard_window(tmp_path):
    target = flight()
    end = datetime.fromisoformat(target["end"])
    delayed = provider(estimated_in=(end + timedelta(minutes=90)).isoformat())
    tracker, request = setup(tmp_path, [data(delayed), data(delayed), position(end + timedelta(minutes=72))])
    tracker.poll(CONFIG, [record()], NOW, 0)
    later = end + timedelta(minutes=70)
    stale = tracker.snapshot(CONFIG, [record()], later)[0]
    assert stale["status"] == "stale" and "live" not in stale
    tracker.poll(CONFIG, [record()], later, 7800)
    assert request.call_count == 2 and request.call_args.args[0].startswith("/flights/DL123?")
    tracker.poll(CONFIG, [record()], end + timedelta(minutes=72), 7920)
    assert request.call_count == 3
    assert tracker.snapshot(CONFIG, [record()], end + timedelta(minutes=72))[0]["live"]
    tracker.poll(CONFIG, [record()], end + timedelta(hours=2, seconds=1), 11000)
    assert request.call_count == 3
    assert tracker.snapshot(CONFIG, [record()], end + timedelta(hours=2, seconds=1)) == []


def test_feed_operating_lookup_precedes_lisa_and_existing_shared_cap_blocks_network(tmp_path):
    (tmp_path / "flightaware.key").write_text("synthetic-key")
    feed = FlightFeeds(tmp_path)
    order = []
    with (patch.object(feed, "_poll_live", side_effect=lambda *a: order.append("operating")),
          patch.object(feed.personal_tracking, "poll", side_effect=lambda *a, **kw: order.append("lisa")),
          patch.object(feed, "_poll_watches", side_effect=lambda *a: order.append("watches"))):
        feed.poll(CONFIG, None, personal_flights=[record()])
    assert order == ["operating", "lisa", "watches"]
    config = CONFIG | {"flightaware_monthly_limit_usd": 0}
    with (patch("flightwall.feeds.utcnow", return_value=NOW), patch("flightwall.feeds.request_json") as network):
        feed.personal_tracking.poll(config, [record()], NOW, 0)
        network.assert_not_called()
    result = feed.personal_tracking.snapshot(config, [record()], NOW)[0]
    assert result["reason"] == "monthly_limit" and result["status"] == "unavailable"


GATE = CONFIG | {"flightaware_household_gate_tracking_enabled": True, "flightaware_quiet_hours_enabled": True,
                 "timezone": "America/Chicago", "presence_enabled": True, "airline": "DL"}
NIGHT = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)  # 9 p.m. Central


def night_home(directory, who="chris", when=NIGHT):
    from flightwall.presence import PresenceStore
    presence = PresenceStore(directory, lambda: GATE)
    presence.rotate(who)
    presence.update(who, {"state": "home", "observed_at": when.isoformat()}, when)
    return presence


@pytest.mark.parametrize("person", ["chris", "lisa"])
def test_night_gate_tracking_requires_one_fresh_home_and_preserves_all_limits(tmp_path, person):
    from flightwall.feeds import PaidLimitError, PaidRequestsPaused
    feed = FlightFeeds(tmp_path)
    path = "/flights/DL123?max_pages=1"
    with patch("flightwall.feeds.utcnow", return_value=NIGHT):
        for when in (NIGHT - timedelta(days=1), NIGHT):
            night_home(tmp_path, person, when)
            if when < NIGHT:
                with pytest.raises(PaidRequestsPaused):
                    feed._allow_paid_request(GATE, path, purpose="household")
        for config, purpose in ((GATE, "operating"), (GATE, "nearby"),
                                (GATE | {"presence_enabled": False}, "household"),
                                (GATE | {"flightaware_household_gate_tracking_enabled": False}, "household")):
            with pytest.raises(PaidRequestsPaused):
                feed._allow_paid_request(config, path, purpose=purpose)
        usage_file = tmp_path / "flightaware-usage.json"
        assert not usage_file.exists()
        with pytest.raises(PaidLimitError, match="monthly app cost limit"):
            feed._allow_paid_request(GATE | {"flightaware_monthly_limit_usd": 0}, path, purpose="household")
        feed._allow_paid_request(GATE, path, purpose="household")
        assert json.loads(usage_file.read_text())["reserved_milliusd"] == 5
        with pytest.raises(PaidLimitError, match="daily request limit"):
            feed._allow_paid_request(GATE | {"flightaware_daily_limit": 1}, path, purpose="household")


def test_overnight_personal_touchdown_keeps_polling_until_exact_gate_then_stops(tmp_path):
    from flightwall.personal_tracking_view import _arrival_clock
    feed = FlightFeeds(tmp_path)
    (tmp_path / "flightaware.key").write_text("fixture")
    night_home(tmp_path)
    start, end = NIGHT - timedelta(hours=1), NIGHT + timedelta(minutes=5)
    trip = record(number="AA123", origin="CLT", destination="VPS", source="american_email", status="confirmed",
                  start=start.isoformat(), end=end.isoformat())
    gate = NIGHT + timedelta(minutes=2)
    landed = provider(ident="AAL123", ident_iata="AA123", fa_flight_id="AA123-fixture", actual_off=start.isoformat(),
                      origin={"code_iata": "CLT"}, destination={"code_iata": "VPS"}, scheduled_out=start.isoformat(),
                      scheduled_in=end.isoformat(), actual_on=NIGHT.isoformat(), estimated_in=gate.isoformat())
    with patch("flightwall.feeds.request_json", side_effect=[data(landed), data(landed | {"actual_in": gate.isoformat()})]) as network:
        for tick in (0, 60, 120, 240, 360):
            when = NIGHT + timedelta(seconds=tick)
            with patch("flightwall.feeds.utcnow", return_value=when), patch("flightwall.feeds.time.monotonic", return_value=tick):
                feed.poll(GATE, None, personal_flights=[trip])
                row, = feed.personal_tracking.snapshot(GATE, [trip], when)
                assert _arrival_clock(row, when) == (("LANDED", NIGHT) if tick < 120 else ("AT GATE", gate))
            assert network.call_count == (1 if tick < 120 else 2)
        assert all(urlsplit(call.args[0]).path.endswith("/flights/AA123") for call in network.call_args_list)
        assert json.loads((tmp_path / "flightaware-usage.json").read_text())["reserved_milliusd"] == 10


def test_presence_change_between_operating_status_and_position_closes_second_charge(tmp_path):
    feed = FlightFeeds(tmp_path)
    (tmp_path / "flightaware.key").write_text("fixture")
    presence = night_home(tmp_path, when=NIGHT - timedelta(minutes=1))
    start = (NIGHT - timedelta(minutes=30)).isoformat()
    trip = flight(start=start, end=(NIGHT + timedelta(minutes=40)).isoformat())
    def depart(*args):
        presence.update("chris", {"state": "away", "observed_at": NIGHT.isoformat()}, NIGHT)
        return data(provider(scheduled_out=start, actual_off=start))
    with patch("flightwall.feeds.utcnow", return_value=NIGHT), patch("flightwall.feeds.request_json", side_effect=depart) as network:
        feed.poll(GATE, trip)
        assert network.call_count == 1
        assert feed.operating_status_for_config(GATE, trip, NIGHT)["status"] == "paused_quiet_hours"
        assert json.loads((tmp_path / "flightaware-usage.json").read_text())["reserved_milliusd"] == 5


def test_chris_personal_flights_respect_lisa_consent_and_skip_duplicate_duty_leg(tmp_path):
    tracker, network = setup(tmp_path, [data()])
    mine = record(traveler="chris")
    other = record()
    settings = GATE | {"lisa_flight_tracking_enabled": False}
    tracker.poll(settings, [mine, other], NOW, 0, operating=flight())
    network.assert_not_called()
    assert tracker.snapshot(settings, [mine, other], NOW, operating=flight()) == []
    tracker.poll(settings, [mine, other], NOW, 120)
    network.assert_called_once()
    row, = tracker.snapshot(settings, [mine, other], NOW)
    assert row["traveler"] == "chris" and row["provider"]["phase"] == "airborne"
    assert row["id"] != flight()["id"]
    assert tracker.snapshot(settings | {"guest_mode": True}, [mine, other], NOW) == []


def test_delayed_observed_reservation_follows_beyond_old_window_without_reviving_unmatched_flight():
    from flightwall.flight_alerts import gate_follow_window, within_tracking_window
    target = flight()
    later = NOW + timedelta(hours=9)
    estimate = (later + timedelta(minutes=30)).isoformat()
    observed = normalize_operating_status(provider(estimated_in=estimate), target, NOW)
    assert not personal_tracking_window(target, later, observed)
    assert personal_tracking_window(target, later, observed, follow_gate=True)
    assert within_tracking_window(target, later, observed)
    assert not gate_follow_window(target, later, observed | {"flight_id": "other"})
    assert not gate_follow_window(target, later + timedelta(days=1), observed)


def test_two_household_bookings_share_each_paid_request_and_keep_distinct_traveler_labels(tmp_path):
    legs = [record(), record(traveler="chris")]
    later = NOW + timedelta(seconds=120)
    tracker, request = setup(tmp_path, [data(), position(later)])
    tracker.poll(GATE, legs, NOW, 0)
    tracker.poll(GATE, legs, later, 120)
    assert request.call_count == 2
    assert request.call_args.args[0].endswith("/position")
    both = tracker.snapshot(GATE, legs, later)
    assert {row["traveler"] for row in both} == {"chris", "lisa"}
    assert len({row["id"] for row in both}) == 2
    for row in both:
        assert row["provider"]["flight_id"] == row["live"]["flight_id"] == row["id"]
    mine, = tracker.snapshot(GATE | {"lisa_flight_tracking_enabled": False}, legs, later)
    assert mine["traveler"] == "chris"


def test_gate_follow_keeps_original_flight_through_provider_timetable_revisions():
    from flightwall.flight_alerts import match_operating_flight
    target = flight()
    shifted = provider(scheduled_out=(datetime.fromisoformat(target["start"]) + timedelta(minutes=7)).isoformat())
    other = provider(fa_flight_id="another-aircraft")
    assert match_operating_flight(data(shifted), target, GATE) == ("matched", shifted)
    assert match_operating_flight(data(other, shifted), target, GATE)[0] == "ambiguous"
    assert match_operating_flight(data(other, shifted), target, GATE,
                                  provider_id=shifted["fa_flight_id"]) == ("matched", shifted)
    assert match_personal_flight(data(shifted), target)[0] == "not_found"
    assert match_personal_flight(data(other, shifted), target, provider_id=shifted["fa_flight_id"]) == ("matched", shifted)


def test_lisa_on_operating_flight_shares_its_fix_and_gate_clock_with_no_extra_requests(tmp_path):
    from flightwall.personal_tracking_view import _arrival_clock
    feed = FlightFeeds(tmp_path)
    (tmp_path / "flightaware.key").write_text("fixture")
    operating = flight() | {"id": "roster-test", "number": "123"}
    gate = NOW + timedelta(minutes=2)
    config = GATE | {"flightaware_quiet_hours_enabled": False}
    with patch("flightwall.feeds.request_json", side_effect=[
            data(), position(), data(provider(actual_on=NOW.isoformat(), actual_in=gate.isoformat()))]) as network:
        for tick in (0, 120, 240):
            when = NOW + timedelta(seconds=tick)
            with patch("flightwall.feeds.utcnow", return_value=when), patch("flightwall.feeds.time.monotonic", return_value=tick):
                feed.poll(config, operating, personal_flights=[record(), record(traveler="chris")])
                row, = feed.personal_tracking.snapshot(config, [record(), record(traveler="chris")], when, operating=operating)
                assert row["traveler"] == "lisa" and row["provider"]["number"] == "DL123"
                if tick == 0:
                    assert row["live"]["flight_id"] == row["id"] and network.call_count == 2
                else:
                    assert _arrival_clock(row, when) == ("AT GATE", gate) and network.call_count == 3
