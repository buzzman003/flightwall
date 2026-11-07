"""Privacy, local-date boundaries, and fail-unknown Kids calendar parsing."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from flightwall.calendar_client import CalendarClient, CalendarError, SCOPES
from flightwall.config import private_json
from flightwall.household_calendar import HouseholdCalendars, kids_snapshot, parse_kids_events, parse_labelled_layovers


UTC = timezone.utc
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def event(summary="Mom", start="2026-10-22T12:00:00Z", end="2026-10-22T20:00:00Z", **extra):
    return {"summary": summary, "start": {"dateTime": start},
            "end": {"dateTime": end}, **extra}


def all_day(summary="Mom", start="2026-10-22", end="2026-10-23", zone=None, **extra):
    result = {"summary": summary, "start": {"date": start}, "end": {"date": end}, **extra}
    if zone is not None:
        result["start"]["timeZone"] = zone
        result["end"]["timeZone"] = zone
    return result


class KidsParserTests(unittest.TestCase):
    def test_only_exact_parent_summaries_supply_the_configured_meaning(self):
        ignored = ["Mom and Dad", "Mom: pickup", "Appointment w/Mom", "Dad's birthday",
                   "Dinner", "Mom\u200b", None, 23, ["Mom"]]
        parsed = parse_kids_events([event(title) for title in ["Mom", "Dad", *ignored]])
        self.assertEqual({(item["parent"], item["state"]) for item in parsed},
                         {("Mom", "home"), ("Dad", "away")})
        self.assertEqual(len(parsed), 2)

    def test_real_house_title_aliases_allow_trim_and_case_but_never_substrings(self):
        for title, parent in [(" Mom ", "Mom"), ("mom's house", "Mom"), ("MOM\u2019S HOUSE", "Mom"),
                              (" dad ", "Dad"), ("Dad's House", "Dad"), ("Dad\u2019s House", "Dad")]:
            with self.subTest(title=title):
                self.assertEqual(parse_kids_events([event(title)])[0]["parent"], parent)
        self.assertEqual(parse_kids_events([event("Dentist w/Mom"), event("Dinner at Dad's House"), event("Mom's House party")]), [])

    def test_parser_retains_only_safe_intervals_and_does_not_mutate_events(self):
        private = "PRIVATE-KIDS-DETAILS"
        events = [event(id=private + "-event-id", description=private + "-description",
                        location=private + "-location", organizer={"email": private + "@invalid"},
                        attendees=[{"email": private + "-attendee@invalid"}],
                        recurringEventId=private + "-series-id")]
        original = deepcopy(events)
        result = parse_kids_events(events)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]), {"start", "end", "parent", "state"})
        self.assertNotIn(private, json.dumps(result))
        self.assertEqual(events, original)
        self.assertIsNotNone(datetime.fromisoformat(result[0]["start"]).utcoffset())
        self.assertIsNotNone(datetime.fromisoformat(result[0]["end"]).utcoffset())

    def test_cancelled_and_unrelated_events_are_ignored_even_with_parent_description(self):
        self.assertEqual(parse_kids_events([
            event("Mom", status="cancelled"),
            event("Dad", status="cancelled"),
            event("Vacation", description="Mom"),
            {"description": "Dad", "start": {"dateTime": "2026-10-22T12:00:00Z"},
             "end": {"dateTime": "2026-10-22T20:00:00Z"}},
        ]), [])

    def test_latest_cancellation_suppresses_older_recurring_occurrence_in_any_order(self):
        occurrence = event(id="private-recurring-occurrence", recurringEventId="private-series",
                           updated="2026-10-20T12:00:00Z")
        cancellation = {"id": "private-recurring-occurrence", "status": "cancelled",
                        "updated": "2026-10-21T12:00:00Z"}
        for events in ([occurrence, cancellation], [cancellation, occurrence]):
            with self.subTest(cancellation_first=events[0] is cancellation):
                self.assertEqual(parse_kids_events(events), [])

    def test_latest_edit_supersedes_old_parent_and_older_cancellation(self):
        cancelled = {"id": "one-occurrence", "status": "cancelled",
                     "updated": "2026-10-19T12:00:00Z"}
        old = event("Mom", id="one-occurrence", updated="2026-10-20T12:00:00Z")
        latest = event("Dad", id="one-occurrence", updated="2026-10-21T12:00:00Z")
        for events in ([cancelled, old, latest], [latest, old, cancelled]):
            parsed = parse_kids_events(events)
            self.assertEqual(len(parsed), 1)
            self.assertEqual((parsed[0]["parent"], parsed[0]["state"]), ("Dad", "away"))

    def test_latest_cancellation_compares_updated_instants_across_offsets(self):
        old = event(id="same-occurrence", updated="2026-10-21T11:59:00Z")
        cancelled = {"id": "same-occurrence", "status": "cancelled",
                     "updated": "2026-10-21T08:00:00-04:00"}  # Noon UTC.
        self.assertEqual(parse_kids_events([cancelled, old]), [])

    def test_cancelling_one_recurrence_does_not_remove_another_occurrence(self):
        parsed = parse_kids_events([
            event(id="series_day_one", recurringEventId="series",
                  updated="2026-10-20T12:00:00Z"),
            {"id": "series_day_one", "status": "cancelled", "updated": "2026-10-21T12:00:00Z"},
            event("Dad", "2026-10-23T12:00:00Z", "2026-10-23T20:00:00Z",
                  id="series_day_two", recurringEventId="series"),
        ])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["parent"], "Dad")
        self.assertEqual(dt(parsed[0]["start"]), dt("2026-10-23T12:00:00Z"))

    def test_all_day_boundaries_use_calendar_timezone_and_end_is_next_midnight(self):
        parsed = parse_kids_events([all_day()], default_timezone="America/New_York")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(dt(parsed[0]["start"]), dt("2026-10-22T04:00:00Z"))
        self.assertEqual(dt(parsed[0]["end"]), dt("2026-10-23T04:00:00Z"))

    def test_all_day_endpoint_timezones_override_default(self):
        item = all_day(zone="America/Los_Angeles")
        parsed = parse_kids_events([item], default_timezone="UTC")
        self.assertEqual(dt(parsed[0]["start"]), dt("2026-10-22T07:00:00Z"))
        self.assertEqual(dt(parsed[0]["end"]), dt("2026-10-23T07:00:00Z"))

    def test_each_all_day_endpoint_uses_its_own_explicit_timezone(self):
        item = all_day()
        item["start"]["timeZone"] = "America/New_York"
        item["end"]["timeZone"] = "America/Los_Angeles"
        parsed = parse_kids_events([item], default_timezone="UTC")
        self.assertEqual(dt(parsed[0]["start"]), dt("2026-10-22T04:00:00Z"))
        self.assertEqual(dt(parsed[0]["end"]), dt("2026-10-23T07:00:00Z"))

    def test_all_day_duration_tracks_both_dst_transitions(self):
        for start, end, expected_hours in [("2026-03-08", "2026-03-09", 23),
                                            ("2026-11-01", "2026-11-02", 25)]:
            with self.subTest(day=start):
                parsed = parse_kids_events([all_day(start=start, end=end)],
                                           default_timezone="America/New_York")
                self.assertEqual(len(parsed), 1)
                self.assertEqual(dt(parsed[0]["end"]) - dt(parsed[0]["start"]),
                                 timedelta(hours=expected_hours))

    def test_explicit_offsets_allow_fall_back_and_overnight_intervals(self):
        parsed = parse_kids_events([
            event("Mom", "2026-11-01T01:30:00-04:00", "2026-11-01T01:20:00-05:00"),
            event("Dad", "2026-11-02T23:30:00-05:00", "2026-11-03T01:30:00-05:00"),
        ])
        self.assertEqual(len(parsed), 2)
        intervals = {item["parent"]: item for item in parsed}
        self.assertEqual(dt(intervals["Mom"]["end"]) - dt(intervals["Mom"]["start"]),
                         timedelta(minutes=50))
        self.assertEqual(dt(intervals["Dad"]["end"]) - dt(intervals["Dad"]["start"]),
                         timedelta(hours=2))

    def test_invalid_boundaries_and_unzoned_timed_values_are_not_guessed(self):
        invalid = [
            None, "Mom", {},
            event(start="not-a-date"), event(end="not-a-date"),
            event(start="2026-10-22T20:00:00Z"),
            event(start="2026-10-22T21:00:00Z"),
            event(start="2026-10-22T12:00:00", end="2026-10-22T20:00:00"),
            all_day(start="2026-02-30", end="2026-03-01"),
            all_day(start="2026-10-23", end="2026-10-23"),
            all_day(zone="Invalid/Private-Timezone"),
            {"summary": "Mom", "start": {}, "end": {}},
            {"summary": "Dad", "start": None, "end": []},
        ]
        self.assertEqual(parse_kids_events(invalid), [])

    def test_ambiguous_and_nonexistent_local_timed_values_are_not_guessed(self):
        for start, end in [("2026-11-01T01:30:00", "2026-11-01T03:00:00"),
                           ("2026-03-08T02:30:00", "2026-03-08T04:00:00")]:
            with self.subTest(start=start):
                item = {"summary": "Mom",
                        "start": {"dateTime": start, "timeZone": "America/New_York"},
                        "end": {"dateTime": end, "timeZone": "America/New_York"}}
                self.assertEqual(parse_kids_events([item]), [])


class KidsSnapshotTests(unittest.TestCase):
    def snapshot(self, events=None, last_sync=None, now=NOW, **kwargs):
        intervals = parse_kids_events([event()] if events is None else events)
        return kids_snapshot(intervals, (now.isoformat() if last_sync is None else last_sync),
                             now=now, **kwargs)

    def assert_unknown(self, snapshot):
        self.assertEqual(snapshot["state"], "unknown")
        self.assertIsNone(snapshot["parent"])
        self.assertIsNone(snapshot["until"])

    def test_fresh_active_parent_has_state_exclusive_end_sync_and_configured_source(self):
        last_sync = "2026-10-22T15:59:00Z"
        for parent, state in [("Mom", "home"), ("Dad", "away")]:
            with self.subTest(parent=parent):
                snapshot = self.snapshot([event(parent)], last_sync=last_sync, source="Kids")
                self.assertEqual(snapshot["state"], state)
                self.assertEqual(snapshot["parent"], parent)
                self.assertEqual(dt(snapshot["until"]), dt("2026-10-22T20:00:00Z"))
                self.assertEqual(dt(snapshot["last_sync"]), dt(last_sync))
                self.assertEqual(snapshot["source"], "Kids")
                self.assertFalse(snapshot["stale"])
                self.assertIsNone(snapshot["error"])

    def test_active_interval_includes_exact_start_and_excludes_exact_end(self):
        for clock, expected in [("2026-10-22T11:59:59.999999Z", "unknown"),
                                ("2026-10-22T12:00:00Z", "home"),
                                ("2026-10-22T19:59:59.999999Z", "home"),
                                ("2026-10-22T20:00:00Z", "unknown")]:
            with self.subTest(now=clock):
                snapshot = self.snapshot(now=dt(clock))
                self.assertEqual(snapshot["state"], expected)
                if expected == "unknown":
                    self.assert_unknown(snapshot)

    def test_all_day_end_is_exclusive_across_dst(self):
        intervals = parse_kids_events([all_day(start="2026-03-08", end="2026-03-09")],
                                     default_timezone="America/New_York")
        before = dt("2026-03-09T03:59:59.999999Z")
        at_end = dt("2026-03-09T04:00:00Z")
        self.assertEqual(kids_snapshot(intervals, before.isoformat(), now=before)["state"], "home")
        self.assert_unknown(kids_snapshot(intervals, at_end.isoformat(), now=at_end))

    def test_conflicting_active_parents_are_unknown_regardless_of_order(self):
        for events in ([event("Mom"), event("Dad")], [event("Dad"), event("Mom")]):
            snapshot = self.snapshot(events)
            self.assert_unknown(snapshot)
            self.assertFalse(snapshot["stale"])

    def test_nonoverlapping_parent_handoff_changes_at_exact_boundary(self):
        events = [event("Mom", end="2026-10-22T16:00:00Z"),
                  event("Dad", start="2026-10-22T16:00:00Z")]
        before = self.snapshot(events, now=NOW - timedelta(microseconds=1))
        at_boundary = self.snapshot(events)
        self.assertEqual((before["parent"], before["state"]), ("Mom", "home"))
        self.assertEqual((at_boundary["parent"], at_boundary["state"]), ("Dad", "away"))

    def test_missing_current_interval_stays_unknown_despite_future_parent_event(self):
        for events in ([], [event(start="2026-10-22T17:00:00Z")],
                       [event(end="2026-10-22T15:00:00Z")]):
            with self.subTest(events=events):
                snapshot = self.snapshot(events)
                self.assert_unknown(snapshot)
                self.assertFalse(snapshot["stale"])

    def test_duplicate_same_parent_does_not_create_a_conflict(self):
        snapshot = self.snapshot([event("Mom"), event("Mom", start="2026-10-22T15:00:00Z")])
        self.assertEqual((snapshot["parent"], snapshot["state"]), ("Mom", "home"))
        self.assertFalse(snapshot["stale"])

    def test_last_sync_older_than_thirty_minutes_overrides_active_interval(self):
        fresh = self.snapshot(last_sync=(NOW - timedelta(minutes=30)).isoformat())
        self.assertEqual(fresh["state"], "home")
        self.assertFalse(fresh["stale"])
        stale = self.snapshot(last_sync=(NOW - timedelta(minutes=30, microseconds=1)).isoformat())
        self.assert_unknown(stale)
        self.assertTrue(stale["stale"])
        self.assertIsInstance(stale["error"], str)

    def test_explicit_stale_after_controls_the_cache_freshness_boundary(self):
        sync = (NOW - timedelta(seconds=61)).isoformat()
        self.assert_unknown(self.snapshot(last_sync=sync, stale_after=60))
        self.assertEqual(self.snapshot(last_sync=sync, stale_after=120)["state"], "home")

    def test_more_than_five_minutes_future_sync_fails_unknown(self):
        allowed = self.snapshot(last_sync=(NOW + timedelta(minutes=5)).isoformat())
        self.assertEqual(allowed["state"], "home")
        self.assertFalse(allowed["stale"])
        invalid = self.snapshot(last_sync=(NOW + timedelta(minutes=5, microseconds=1)).isoformat())
        self.assert_unknown(invalid)
        self.assertTrue(invalid["stale"])

    def test_absent_invalid_or_unzoned_sync_fails_unknown_without_echoing_input(self):
        intervals = parse_kids_events([event()])
        for invalid in (None, "", "PRIVATE-SYNC-SECRET", "2026-10-22T15:59:00", [], {}):
            with self.subTest(last_sync=invalid):
                snapshot = kids_snapshot(intervals, invalid, now=NOW, source="Kids")
                self.assert_unknown(snapshot)
                self.assertTrue(snapshot["stale"])
                self.assertNotIn("PRIVATE-SYNC-SECRET", json.dumps(snapshot))

    def test_snapshot_does_not_mutate_parsed_intervals_or_reveal_raw_event_details(self):
        private = "PRIVATE-KIDS-EVENT"
        intervals = parse_kids_events([event(id=private, description=private)])
        before = deepcopy(intervals)
        snapshot = kids_snapshot(intervals, NOW.isoformat(), now=NOW, source="Kids")
        self.assertEqual(intervals, before)
        self.assertNotIn(private, json.dumps(snapshot))


class HouseholdManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {"calendar_id": "flight-main", "calendar_name": "FC View",
                       "secondary_calendar_id": "flight-secondary", "secondary_calendar_name": "Buzz Flight Brief",
                       "family_calendar_id": "family-shared", "family_calendar_name": "Household",
                       "family_calendar_profile": "chris", "kids_filter_enabled": True,
                       "kids_stale_minutes": 30, "timezone": "America/Chicago"}
        self.now = NOW
        self.clock = patch("flightwall.household_calendar._now", side_effect=lambda: self.now)
        self.clock.start()
        self.credentials("chris")
        self.manager = HouseholdCalendars(self.root, lambda: dict(self.config))
        self.calls = []

    def tearDown(self):
        self.clock.stop()
        self.directory.cleanup()

    def credentials(self, profile):
        directory = self.root / "accounts" / profile
        private_json(directory / "google-client.json", {"client_id": profile + ".apps.googleusercontent.com", "client_secret": "synthetic"})
        private_json(directory / "google-token.json", {"client_id": profile + ".apps.googleusercontent.com",
                     "access_token": profile + "-access", "refresh_token": profile + "-refresh", "expires_at": 4102444800,
                     "scopes": list(SCOPES)})

    def api(self, client, path, parameters):
        profile = client.data_dir.name
        self.calls.append((profile, path, deepcopy(parameters)))
        if path == "/users/me/calendarList":
            return {"items": [{"id": profile + "-calendar", "summary": profile + " calendar", "timeZone": "America/Chicago"}]}
        if "family" in path:
            return {"timeZone": "America/Chicago", "items": [all_day("Mom's House"),
                    all_day("Private doctor w/Mom", description="PRIVATE-FAMILY-DESCRIPTION", id="PRIVATE-EVENT-ID")]}
        if "flight-secondary" in path:
            return {"timeZone": "UTC", "items": [event("DL1946: ATL-MEM"),
                    event("99: ATL-LAX", "2026-10-23T12:00:00Z", "2026-10-23T17:00:00Z")]}
        return {"timeZone": "UTC", "items": [event("1946: ATL-MEM"),
                event("Report: 1000L", "2026-10-22T11:00:00Z", "2026-10-22T22:00:00Z")]}

    def patched_api(self):
        return patch.object(CalendarClient, "_api", autospec=True, side_effect=self.api)

    def test_shared_family_calendar_works_with_chris_only_and_flights_deduplicate(self):
        with self.patched_api():
            result = self.manager.sync()
        self.assertEqual(len(result["flight_snapshot"]["flights"]), 2)
        self.assertIn("report", result["flight_snapshot"]["flights"][0])
        self.assertEqual(result["kids"]["state"], "home")
        self.assertEqual(result["kids"]["parent"], "Mom")
        self.assertTrue(result["connected_profiles"]["chris"]["verified"])
        self.assertFalse(result["connected_profiles"]["lisa"]["verified"])
        self.assertTrue(all(profile == "chris" for profile, _path, _params in self.calls))
        raw = self.manager.path.read_text()
        self.assertNotIn("PRIVATE-", raw)
        self.assertNotIn("doctor", raw)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.manager.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((self.root / "accounts").stat().st_mode), 0o700)

    def test_family_agenda_caches_only_approved_todays_activities(self):
        private_json(self.manager.agenda_path, {"children": ["Rory"], "activities": {"Soccer": ["futsal"]}})

        def agenda_api(client, path, parameters):
            result = self.api(client, path, parameters)
            if "family" in path:
                result["items"].extend([
                    event("Rory’s Jiu-Jitsu at PRIVATE-VENUE", "2026-10-22T14:00:00", "2026-10-22T15:00:00",
                          description="PRIVATE-DESCRIPTION", id="PRIVATE-ID"),
                    event("Futsal for Rory", "2026-10-22T21:00:00Z", "2026-10-22T22:00:00Z"),
                    event("Adult dance", "2026-10-22T18:00:00Z", "2026-10-22T19:00:00Z"),
                    event("Rory appointment"), all_day("Rory Dance", zone="America/New_York"),
                    all_day("Soccer Game", start="2026-10-23", end="2026-10-24", zone="America/New_York"),
                    event("Soccer Game", "2026-10-22T15:00:00Z", "2026-10-22T16:00:00Z"),
                    event("Soccer", "2026-10-22T15:00:00Z", "2026-10-22T16:00:00Z"),
                    event("Rory's Viola Lesson", "2026-10-22T16:00:00Z", "2026-10-22T17:00:00Z"),
                    event("Violin Class", "2026-10-22T17:00:00Z", "2026-10-22T18:00:00Z"),
                    event("Violin Class with PRIVATE-ADULT", "2026-10-22T18:00:00Z", "2026-10-22T19:00:00Z"),
                    event("Rory Gymnastics", id="cancelled", updated="2026-10-21T12:00:00Z"),
                    {"id": "cancelled", "status": "cancelled", "updated": "2026-10-21T13:00:00Z"},
                ])
            return result

        with patch.object(CalendarClient, "_api", autospec=True, side_effect=agenda_api):
            outlook = self.manager.sync()["custody_outlook"]
        self.assertEqual(outlook["activities"], [
            {"title": "Rory Dance", "start": "2026-10-22T05:00:00Z", "end": "2026-10-23T05:00:00Z", "all_day": True},
            {"title": "Soccer Game", "start": "2026-10-22T15:00:00Z", "end": "2026-10-22T16:00:00Z"},
            {"title": "Rory Viola", "start": "2026-10-22T16:00:00Z", "end": "2026-10-22T17:00:00Z"},
            {"title": "Violin", "start": "2026-10-22T17:00:00Z", "end": "2026-10-22T18:00:00Z"},
            {"title": "Rory Jiu-jitsu", "start": "2026-10-22T19:00:00Z", "end": "2026-10-22T20:00:00Z"},
            {"title": "Rory Soccer", "start": "2026-10-22T21:00:00Z", "end": "2026-10-22T22:00:00Z"},
        ])
        self.assertNotIn("PRIVATE-", self.manager.path.read_text())
        self.assertTrue(all("description" not in parameters.get("fields", "") for _, path, parameters in self.calls if "family" in path))
        self.config["guest_mode"] = True
        self.assertNotIn("activities", self.manager.status()["custody_outlook"])
        self.config["guest_mode"] = False
        private_json(self.manager.agenda_path, {"children": ["Nora"]})
        self.assertNotIn("activities", self.manager.status()["custody_outlook"])

    def test_family_agenda_resolves_private_initials_and_configured_activity_conflicts(self):
        preferences = {
            "children": ["Rory", "Nora", "Jules"],
            "child_aliases": {"R": "Rory", "N": "Nora", "J": "Jules", "A": "Unlisted"},
            "activity_defaults": {"Jiu-jitsu": "Rory"},
            "activity_precedence": ["Soccer", "Jiu-jitsu"],
            "same_day_activity_overrides": {"Jiu-jitsu": "Soccer"},
        }
        private_json(self.manager.agenda_path, preferences)

        def agenda_api(client, path, parameters):
            result = self.api(client, path, parameters)
            if "family" in path:
                result["items"].extend([
                    event("Jiu-jitsu", "2026-10-22T17:00:00Z", "2026-10-22T18:00:00Z"),
                    event("R: Soccer Practice", "2026-10-22T18:00:00Z", "2026-10-22T19:00:00Z"),
                    event("Nora Jiu-jitsu", "2026-10-22T18:15:00Z", "2026-10-22T19:30:00Z"),
                    event("Rory Jiu-jitsu", "2026-10-22T18:30:00Z", "2026-10-22T19:30:00Z"),
                    event("Rory Jiu-jitsu", "2026-10-22T19:00:00Z", "2026-10-22T20:00:00Z"),
                    event("Violin Class - N", "2026-10-22T20:00:00Z", "2026-10-22T21:00:00Z"),
                    event("J Dance Class", "2026-10-22T21:00:00Z", "2026-10-22T22:00:00Z"),
                    event("A Gymnastics", "2026-10-22T22:00:00Z", "2026-10-22T23:00:00Z"),
                    event("Gymnastics near R park", "2026-10-22T22:00:00Z", "2026-10-22T23:00:00Z"),
                ])
            return result

        with patch.object(CalendarClient, "_api", autospec=True, side_effect=agenda_api):
            activities = self.manager.sync()["custody_outlook"]["activities"]
            self.assertEqual([row["title"] for row in activities],
                             ["Rory Soccer Practice", "Nora Jiu-jitsu", "Rory Jiu-jitsu", "Nora Violin", "Jules Dance"])
            self.assertEqual(activities[2]["start"], "2026-10-22T19:00:00Z")
            private_json(self.manager.agenda_path, {**preferences, "same_day_activity_overrides": {}})
            self.assertNotIn("activities", self.manager.status()["custody_outlook"])
            activities = self.manager.sync()["custody_outlook"]["activities"]
            self.assertEqual(activities[0], {
                "title": "Rory Jiu-jitsu", "start": "2026-10-22T17:00:00Z", "end": "2026-10-22T18:00:00Z",
            })

    def test_credentials_are_not_reported_connected_before_actual_api_success(self):
        profile = self.manager.status()["connected_profiles"]["chris"]
        self.assertTrue(profile["credentials_present"])
        self.assertFalse(profile["authorized"])
        self.assertFalse(profile["connected"])
        with self.patched_api():
            self.manager.list_calendars("chris")
        self.assertTrue(self.manager.status()["connected_profiles"]["chris"]["connected"])

    def test_each_profile_lists_its_actual_independent_account(self):
        self.credentials("lisa")
        with self.patched_api():
            chris = self.manager.list_calendars("chris")
            lisa = self.manager.list_calendars("lisa")
        self.assertEqual(chris[0]["id"], "chris-calendar")
        self.assertEqual(lisa[0]["id"], "lisa-calendar")
        self.assertNotEqual(self.manager.client_for("chris").token_path, self.manager.client_for("lisa").token_path)
        self.config["family_calendar_profile"] = "lisa"
        with self.patched_api():
            self.assertEqual(self.manager.sync()["kids"]["state"], "home")
        self.assertTrue(any(profile == "lisa" and "family" in path for profile, path, _params in self.calls))

    def test_legacy_chris_credentials_migrate_without_overwriting_accounts_or_lisa(self):
        data = self.root / "legacy"
        private_json(data / "google-client.json", {"installed": {"client_id": "legacy-client"}})
        private_json(data / "google-token.json", {"access_token": "legacy-token"})
        manager = HouseholdCalendars(data, lambda: dict(self.config))
        migrated = manager.client_for("chris")
        self.assertEqual(json.loads(migrated.token_path.read_text())["access_token"], "legacy-token")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(migrated.token_path.stat().st_mode), 0o600)
        private_json(data / "google-token.json", {"access_token": "changed-legacy-token"})
        manager.client_for("chris")
        self.assertEqual(json.loads(migrated.token_path.read_text())["access_token"], "legacy-token")
        self.assertFalse(manager.client_for("lisa").token_path.exists())
        self.assertTrue((data / "google-token.json").exists())

    def test_sync_failure_and_staleness_do_not_claim_kids_home(self):
        with self.patched_api():
            previous = self.manager.sync()
        self.assertEqual(previous["kids"]["state"], "home")
        with patch.object(CalendarClient, "_api", side_effect=CalendarError("Google Calendar network unavailable; using cached schedule.")):
            failed = self.manager.sync()
        self.assertEqual(failed["kids"]["state"], "unknown")
        self.assertIsNone(failed["kids"]["parent"])
        self.assertEqual(failed["flight_snapshot"]["flights"], previous["flight_snapshot"]["flights"])
        self.assertFalse(failed["connected_profiles"]["chris"]["connected"])
        self.now += timedelta(minutes=31)
        self.assertTrue(self.manager.status()["kids"]["stale"])

    def test_role_switch_discards_prior_family_and_secondary_cache_immediately(self):
        with self.patched_api():
            self.manager.sync()
        self.config["family_calendar_id"] = "different-family"
        self.config["secondary_calendar_id"] = "different-secondary"
        result = self.manager.status()
        self.assertEqual(result["kids"]["state"], "unknown")
        self.assertEqual(len(result["flight_snapshot"]["flights"]), 1)

    def test_preview_uses_real_calendar_timezone_and_no_unrelated_content(self):
        with self.patched_api():
            result = self.manager.preview_kids("chris", "family-shared")
        self.assertEqual(result["matching_events"], 1)
        self.assertEqual(result["timezone"], "America/Chicago")
        self.assertEqual(dt(result["intervals"][0]["start"]), dt("2026-10-22T05:00:00Z"))
        self.assertNotIn("PRIVATE", json.dumps(result))
        for _profile, path, parameters in self.calls:
            if "/events" in path:
                self.assertNotIn("description", parameters["fields"])

    def test_oauth_completion_is_not_verified_until_calendar_api_succeeds(self):
        with patch.object(CalendarClient, "authorize", return_value={"authorized": True}):
            with patch.object(CalendarClient, "list_calendars", side_effect=CalendarError("Google Calendar access denied.")):
                with self.assertRaises(CalendarError):
                    self.manager.authorize("chris", "synthetic-client.json", open_browser=False)
        self.assertFalse(self.manager.status()["connected_profiles"]["chris"]["verified"])

    def test_missing_calendar_timezone_does_not_guess_all_day_utc(self):
        def missing_zone(client, path, parameters):
            result = self.api(client, path, parameters)
            if "family" in path:
                result.pop("timeZone", None)
            return result
        with patch.object(CalendarClient, "_api", autospec=True, side_effect=missing_zone):
            result = self.manager.sync()
        self.assertEqual(result["kids"]["state"], "unknown")

    def test_malformed_cache_does_not_claim_kids_state_or_connection(self):
        private_json(self.manager.path, {"schema_version": 1, "profiles": {"chris": "broken"},
                     "flight_roles": {"primary": {"binding": "invalid", "snapshot": []}},
                     "family": {"binding": "invalid", "intervals": None}})
        result = self.manager.status()
        self.assertEqual(result["kids"]["state"], "unknown")
        self.assertFalse(result["connected_profiles"]["chris"]["connected"])

    def test_hotel_descriptions_are_requested_only_for_opted_in_flight_roles(self):
        self.config["hotel_details_enabled"] = True
        def with_hotel(client, path, parameters):
            result = self.api(client, path, parameters)
            if "flight-main" in path:
                result["items"].append(event("Layover: LAX", description="Hotel: Airport Hilton\nCity: Los Angeles\nCrew: PRIVATE-CREW"))
            return result
        with patch.object(CalendarClient, "_api", autospec=True, side_effect=with_hotel):
            result = self.manager.sync()
        self.assertEqual(result["flight_snapshot"]["layovers"][0]["hotel"], "Airport Hilton")
        for _profile, path, parameters in self.calls:
            if "/events" in path:
                self.assertEqual("description" in parameters["fields"], "family" not in path)
        self.assertNotIn("PRIVATE-CREW", self.manager.path.read_text())
        self.config["hotel_details_enabled"] = False
        self.assertEqual(self.manager.status()["flight_snapshot"]["layovers"], [])

    def brief_api(self, client, path, parameters):
        if "flight-main" in path:
            self.calls.append((client.data_dir.name, path, deepcopy(parameters)))
            return {"timeZone": "America/Chicago", "items": [
                event("DL18: ATL-VPS", "2026-10-22T20:00:00Z", "2026-10-22T22:00:00Z"),
                event("DL19: VPS-ATL", "2026-10-23T14:00:00Z", "2026-10-23T16:00:00Z"),
                event("Report: 0800L", "2026-10-23T13:00:00Z", "2026-10-23T18:00:00Z")]}
        if "flight-secondary" in path:
            self.calls.append((client.data_dir.name, path, deepcopy(parameters)))
            return {"timeZone": "America/Chicago", "items": [event(
                "🏠 Buzz Flight Brief — VPS Home Overnight (Oct 22–23)", "2026-10-22T22:00:00Z", "2026-10-23T13:00:00Z",
                description="LEAVE HOME FOR VPS — requested aircraft-arrival formula: RECOMMENDED\n6:30 AM\n"
                            "IMPORTANT REPORT-TIME CONFLICT: To meet scheduled report on time, leave by 6:20 AM.\n"
                            "Crew: PRIVATE-CREW\nHotel: PRIVATE-HOTEL\nPhone: 555-123-4567")]}
        return self.api(client, path, parameters)

    def test_optional_leave_time_notes_match_exact_report_and_disable_immediately(self):
        self.config["brief_leave_times_enabled"] = True
        self.config["brief_leave_policy"] = "report_safe"
        with patch.object(CalendarClient, "_api", autospec=True, side_effect=self.brief_api):
            self.manager.sync()
        flight = self.manager.status()["flight_snapshot"]["flights"][1]
        self.assertEqual(flight["leave_at"], "2026-10-23T11:20:00+00:00")
        self.assertTrue(flight["leave_conflict"])
        self.assertEqual(flight["report"], "2026-10-23T13:00:00+00:00")
        self.assertNotIn("PRIVATE-", self.manager.path.read_text())
        self.assertNotIn("description", self.manager.path.read_text())
        self.assertNotIn("555", self.manager.path.read_text())
        for _profile, path, parameters in self.calls:
            if "/events" in path:
                self.assertEqual("description" in parameters["fields"], "flight-secondary" in path)
        self.config["brief_leave_policy"] = "conflict"
        flight = self.manager.status()["flight_snapshot"]["flights"][1]
        self.assertNotIn("leave_at", flight)
        self.assertTrue(flight["leave_conflict"])
        self.config["brief_leave_policy"] = "recommended"
        self.assertEqual(self.manager.status()["flight_snapshot"]["flights"][1]["leave_at"], "2026-10-23T11:30:00+00:00")
        self.config["brief_leave_times_enabled"] = False
        flight = self.manager.status()["flight_snapshot"]["flights"][1]
        self.assertNotIn("leave_at", flight)
        self.assertNotIn("leave_conflict", flight)
        self.config["brief_leave_times_enabled"] = True
        self.config["secondary_calendar_id"] = "different-secondary"
        self.assertNotIn("leave_at", self.manager.status()["flight_snapshot"]["flights"][1])

    def test_invalid_leave_cache_field_does_not_discard_the_flight_schedule(self):
        self.config["brief_leave_times_enabled"] = True
        with patch.object(CalendarClient, "_api", autospec=True, side_effect=self.brief_api):
            self.manager.sync()
        stored = json.loads(self.manager.path.read_text())
        for leave in (None, 6, "2026-10-23T11:30:00", "2026-10-23T13:01:00Z", "2026-10-22T22:00:00Z"):
            with self.subTest(leave=leave):
                stored["flight_roles"]["secondary"]["snapshot"]["brief_leaves"][0]["recommended_at"] = leave
                private_json(self.manager.path, stored)
                snapshot = self.manager.status()["flight_snapshot"]
                self.assertEqual(len(snapshot["flights"]), 2)
                self.assertNotIn("leave_at", snapshot["flights"][1])


class LabelledHotelTests(unittest.TestCase):
    def test_only_explicit_timed_hotel_events_supply_safe_hotel_metadata(self):
        source = [event("Layover: LAX", description="Hotel: Airport Hilton\nCity: Los Angeles\nCrew: PRIVATE-CREW\nPhone: 555-123-4567"),
                  event("Report: 0900L", description="Hotel: PRIVATE-REPORT-HOTEL"),
                  all_day("Hotel: MEM", description="Hotel: PRIVATE-ALLDAY-HOTEL"),
                  event("Appointment", description="Hotel: PRIVATE-UNRELATED-HOTEL")]
        before = deepcopy(source)
        parsed = parse_labelled_layovers(source, "FC View")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0], {"airport": "LAX", "start": "2026-10-22T12:00:00Z", "end": "2026-10-22T20:00:00Z",
                                     "hotel": "Airport Hilton", "city": "Los Angeles", "source_label": "FC View"})
        self.assertNotIn("PRIVATE", json.dumps(parsed))
        self.assertNotIn("555", json.dumps(parsed))
        self.assertEqual(source, before)

    def test_contact_or_crew_values_are_not_hotel_names(self):
        for value in ["Hilton 555-123-4567", "hotel@example.invalid", "https://hotel.example.invalid", "Crew Captain Jones", "<b>Hilton</b>"]:
            self.assertEqual(parse_labelled_layovers([event("Hotel: LAX", description="Hotel: " + value)]), [])

    def test_explicit_airport_label_and_latest_cancellation(self):
        valid = event("Hotel", id="hotel-event", updated="2026-10-20T00:00:00Z",
                      description="Airport: MEM\nLayover Hotel: Riverside Inn")
        self.assertEqual(parse_labelled_layovers([valid])[0]["airport"], "MEM")
        cancelled = {"id": "hotel-event", "status": "cancelled", "updated": "2026-10-21T00:00:00Z"}
        self.assertEqual(parse_labelled_layovers([valid, cancelled]), [])


if __name__ == "__main__":
    unittest.main()
