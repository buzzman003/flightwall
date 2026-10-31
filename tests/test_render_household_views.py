"""Bounded timer overlays and derived custody outlooks use fictional inputs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from PIL import Image

from flightwall import render
from flightwall.display import entry_render_state, fresh_household_status


NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)


def labels_for(state):
    labels = []
    original = render._text

    def capture(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(draw, xy, value, *args, **kwargs)

    with patch.object(render, "_text", side_effect=capture):
        image = render.render_frame(state, {"timezone": "America/Chicago"})
    return labels, image


class TimerRenderTests(unittest.TestCase):
    def timer(self, seconds, label="PASTA"):
        return {"id": label, "label": label, "ends_at": (NOW + timedelta(seconds=seconds)).isoformat(),
                "remaining_seconds": 999, "status": "running"}

    def test_countdown_uses_deadline_and_expiry_is_steady(self):
        state = {"now": NOW.isoformat(), "timers": [self.timer(61)]}
        before = deepcopy(state)
        self.assertIn("01:01", labels_for(state)[0])
        state["now"] = (NOW + timedelta(seconds=60, microseconds=900000)).isoformat()
        self.assertIn("00:01", labels_for(state)[0])
        state["now"] = (NOW + timedelta(seconds=61)).isoformat()
        labels, image = labels_for(state)
        self.assertIn("DONE", labels)
        later = state | {"now": (NOW + timedelta(seconds=120)).isoformat()}
        self.assertEqual(image.crop((0, 0, 512, 36)).tobytes(), labels_for(later)[1].crop((0, 0, 512, 36)).tobytes())
        self.assertEqual(state["timers"], before["timers"])

    def test_five_labels_are_centered_and_countdowns_fit(self):
        state = {"now": NOW.isoformat(), "timers": [self.timer(3600 + i, "TIMER " + str(i)) for i in range(6)]}
        labels, image = labels_for(state)
        for index in range(5):
            self.assertIn("TIMER " + str(index), labels)
        self.assertNotIn("TIMER 5", labels)
        for seconds in (1, 59, 60, 3599, 3600, 35999, 36000, 86400, 604800):
            self.assertLessEqual(render._width(render._timer_countdown(seconds), 2), 88)
        self.assertEqual(image.getpixel((11, 1)), render.LINE)
        self.assertEqual(image.getpixel((500, 1)), render.LINE)

    def test_cached_finished_timer_disappears_after_five_minutes(self):
        timer = self.timer(0)
        before = deepcopy(timer)
        self.assertEqual(len(render.visible_timers({"timers": [timer]}, NOW + timedelta(seconds=299))), 1)
        self.assertEqual(render.visible_timers({"timers": [timer]}, NOW + timedelta(seconds=300)), [])
        self.assertEqual(render.visible_timers({"timers": [timer]}, NOW + timedelta(hours=1)), [])
        self.assertEqual(timer, before)

    def test_extreme_valid_deadline_does_not_overflow_timer_rendering(self):
        timer = self.timer(60) | {"ends_at": "9999-12-31T23:59:59+00:00"}
        self.assertEqual(len(render.visible_timers({"timers": [timer]}, NOW)), 1)
        self.assertEqual(labels_for({"now": NOW.isoformat(), "timers": [timer]})[1].size, (512, 256))

    def test_invalid_and_cancelled_rows_do_not_reserve_space(self):
        state = {"timers": [None, {}, self.timer(60) | {"status": "cancelled"}, self.timer(60) | {"ends_at": "2026-10-31T18:01:00"}]}
        source = Image.new("RGB", (512, 256), (40, 80, 160))
        self.assertIs(render.compose_timer_strip(source, state, NOW), source)

    def test_inset_keeps_all_four_corners_and_the_two_to_one_aspect(self):
        source = Image.new("RGB", (512, 256), (40, 80, 160))
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
        for box, color in zip(((0, 0, 8, 8), (504, 0, 512, 8), (0, 248, 8, 256), (504, 248, 512, 256)), colors):
            source.paste(color, box)
        state = {"timers": [self.timer(60)]}
        for progress in (.25, .5, 1):
            inset = round(render.TIMER_STRIP_HEIGHT * progress)
            height, width = 256 - inset, (256 - inset) * 2
            x = (512 - width) // 2
            image = render.compose_timer_strip(source, state, NOW, progress)
            self.assertEqual(image.crop((x, inset, x + width, 256)).tobytes(), source.resize((width, height), Image.Resampling.LANCZOS).tobytes())
            for point, color in zip(((x, inset), (x + width - 1, inset), (x, 255), (x + width - 1, 255)), colors):
                self.assertEqual(image.getpixel(point), color)
        self.assertEqual(render.compose_timer_strip(source, state, NOW, 0).tobytes(), source.tobytes())

    def test_timers_do_not_bypass_private_card_gate(self):
        state = {"now": NOW.isoformat(), "view": "message", "timers": [self.timer(60)],
                 "selected_card": {"id": "private", "profile": "lisa", "title": "PRIVATE FIXTURE", "observed_at": NOW.isoformat()},
                 "household_context": {"private_lisa": False}}
        labels, image = labels_for(state)
        self.assertIn("NO CARD TO DISPLAY", labels)
        self.assertNotIn("PRIVATE FIXTURE", labels)
        self.assertIn("PASTA", labels)


class CustodyRenderTests(unittest.TestCase):
    def setUp(self):
        self.outlook = {"stale": False, "last_sync": NOW.isoformat(), "timezone": "America/Chicago", "days": [
            {"date": "2026-10-31", "state": "home"},
            {"date": "2026-11-01", "state": "away"},
            {"date": "2026-11-02", "state": "mixed", "segments": [
                {"state": "away", "start": "2026-11-02T06:00:00Z"},
                {"state": "home", "start": "2026-11-02T21:00:00Z"}]},
        ]}
        self.state = {"now": NOW.isoformat(), "view": "custody_outlook", "custody_outlook": self.outlook}

    def test_ten_days_show_houses_changes_and_unknown_without_inference(self):
        before = deepcopy(self.state)
        labels, image = labels_for(self.state)
        self.assertIn("MOM'S HOUSE", labels)
        self.assertIn("DAD'S HOUSE", labels)
        self.assertIn("CHANGE DAY", labels)
        self.assertIn("00:00 DAD", labels)
        self.assertIn("15:00 MOM", labels)
        self.assertEqual(labels.count("UNKNOWN"), 7)
        self.assertIn("11/09", labels)  # October 31 through November 9, inclusive.
        self.assertEqual(self.state, before)
        self.assertEqual(image.size, (512, 256))

    def test_stale_outlook_hides_previous_houses_and_change_times(self):
        self.outlook["stale"] = True
        labels, image = labels_for(self.state)
        self.assertEqual(labels.count("UNKNOWN"), 10)
        self.assertNotIn("MOM'S HOUSE", labels)
        self.assertNotIn("DAD'S HOUSE", labels)
        self.assertNotIn("15:00 MOM", labels)

    def test_household_clock_labels_repeated_dst_hour_correctly(self):
        before, image = labels_for(self.state | {"now": "2026-11-01T06:30:00Z"})
        after, image = labels_for(self.state | {"now": "2026-11-01T07:30:00Z"})
        self.assertIn("01:30 CDT", before)
        self.assertIn("01:30 CST", after)

    def test_display_rechecks_outlook_freshness_and_carries_timers(self):
        status = {"snapshot": {}, "custody_outlook": self.outlook,
                  "timers": [{"id": "tea", "label": "TEA", "ends_at": (NOW + timedelta(minutes=2)).isoformat()}]}
        config = {"kids_stale_minutes": 30}
        state = entry_render_state(status, config, {"view": "custody_outlook"}, NOW)
        self.assertFalse(state["custody_outlook"]["stale"])
        self.assertEqual(state["timers"], status["timers"])
        self.assertFalse(state["demo"])
        for refreshed in (fresh_household_status(status, config, NOW + timedelta(minutes=31)),
                          fresh_household_status(status, config, NOW, backend_available=False)):
            self.assertTrue(refreshed["custody_outlook"]["stale"])
        self.assertFalse(self.outlook["stale"])


class ShoppingTickerTests(unittest.TestCase):
    def setUp(self):
        self.shopping = {"items": [{"id": "milk", "name": "Milk"}, {"id": "eggs", "name": "Eggs"}]}
        self.state = {"shopping_list": self.shopping}

    def test_empty_hidden_or_disabled_list_has_no_ticker_or_content_inset(self):
        source = Image.new("RGB", (512, 256), (40, 80, 160))
        self.assertEqual(render.shopping_ticker_names(self.state), ["MILK", "EGGS"])
        for shopping in ({}, {"items": []}, self.shopping | {"enabled": False}, self.shopping | {"visible": False}):
            state = {"shopping_list": shopping}
            self.assertEqual(render.shopping_ticker_names(state), [])
            self.assertIs(render.compose_display_overlays(source, state, NOW), source)

    def test_names_are_bounded_plain_single_line_text(self):
        self.shopping["items"] = [{"name": "  Milk\n and\t eggs "}, {"name": {}}, {"name": " "}, {"name": "x" * 200}]
        names = render.shopping_ticker_names(self.state)
        self.assertEqual(names, ["MILK AND EGGS", "X" * 80])
        self.shopping["items"] = [{"name": "Milk"}] * 200
        self.assertEqual(len(render.shopping_ticker_names(self.state)), 100)

    def test_scroll_moves_only_item_window_left_and_wraps_seamlessly(self):
        names = render.shopping_ticker_names(self.state)
        first = render._shopping_ticker(names, 0)
        second = render._shopping_ticker(names, 6)
        self.assertEqual(first.crop((0, 0, 75, 18)).tobytes(), second.crop((0, 0, 75, 18)).tobytes())
        self.assertEqual(first.crop((81, 4, 502, 17)).tobytes(), second.crop((75, 4, 496, 17)).tobytes())
        cycle_width = len("   /   ".join(names) + "   /   ") * 6
        self.assertEqual(first.tobytes(), render._shopping_ticker(names, cycle_width).tobytes())
        self.assertEqual(first.tobytes(), render._shopping_ticker(names, float("nan")).tobytes())

    def test_timer_and_ticker_share_one_aspect_preserving_content_inset(self):
        source = Image.new("RGB", (512, 256), (40, 80, 160))
        state = self.state | {"timers": [{"label": "TEA", "ends_at": (NOW + timedelta(minutes=2)).isoformat()}]}
        image = render.compose_display_overlays(source, state, NOW)
        self.assertEqual(image.crop((54, 36, 458, 238)).tobytes(), source.resize((404, 202), Image.Resampling.LANCZOS).tobytes())
        self.assertEqual(image.crop((0, 238, 512, 256)).tobytes(), render._shopping_ticker(["MILK", "EGGS"], 0).tobytes())
        no_timer = render.compose_display_overlays(source, self.state, NOW)
        self.assertEqual(no_timer.crop((18, 0, 494, 238)).tobytes(), source.resize((476, 238), Image.Resampling.LANCZOS).tobytes())

    def test_ticker_is_persistent_across_views_and_does_not_bypass_privacy(self):
        base = render.demo_snapshot(NOW) | self.state
        expected = render._shopping_ticker(["MILK", "EGGS"], 0).tobytes()
        for view in ("details", "map", "schedule", "personal", "setup", "custody_outlook", "local_visit", "message"):
            card = {"profile": "lisa", "title": "PRIVATE FIXTURE"} if view == "message" else None
            state = base | {"view": view, "selected_card": card, "household_context": {}}
            labels, image = labels_for(state)
            self.assertEqual(image.crop((0, 238, 512, 256)).tobytes(), expected)
            self.assertNotIn("PRIVATE FIXTURE", labels)
        display_state = entry_render_state({"snapshot": {}, "shopping_list": self.shopping}, {}, {"view": "details"}, NOW)
        self.assertEqual(display_state["shopping_list"], self.shopping)


class LocalVisitRenderTests(unittest.TestCase):
    def setUp(self):
        self.card = {"id": "fictional-vps", "airport": "VPS", "source": "Calendar schedule",
                     "arrival": "2026-10-31T20:05:00Z", "departure": "2026-11-01T16:00:00Z",
                     "display_start": "2026-10-31T00:00:00Z", "display_end": "2026-11-01T16:00:00Z",
                     "duration_minutes": 1195, "timezone": "America/Chicago"}
        self.state = {"now": NOW.isoformat(), "view": "local_visit", "selected_card": self.card}

    def test_arrival_and_next_departure_keep_their_own_dst_offsets(self):
        labels, image = labels_for(self.state)
        self.assertIn("SCHEDULED ARRIVAL", labels)
        self.assertIn("3:05 PM", labels)
        self.assertIn("10:00 AM", labels)
        self.assertIn("Sat 31 Oct / CDT", labels)
        self.assertIn("Sun 01 Nov / CST", labels)
        self.assertIn("PLANNED STAY / 19H 55M", labels)
        self.assertIn("CALENDAR SCHEDULE / NOT LIVE TRACKING", labels)
        self.assertEqual(image.size, (512, 256))

    def test_missing_outbound_is_explicit_and_does_not_infer_location(self):
        self.card.pop("departure")
        self.card.pop("duration_minutes")
        for airport in ("VPS", "PNS", "ECP"):
            self.card["airport"] = airport
            labels, image = labels_for(self.state)
            self.assertIn(airport, labels)
            self.assertIn("UNKNOWN", labels)
            self.assertIn("NO MATCHING OUTBOUND", labels)
            self.assertIn("SCHEDULE ONLY / LOCATION NOT CONFIRMED", labels)
            self.assertFalse(any("PLANNED STAY" in label for label in labels))

    def test_visit_visibility_is_end_exclusive_even_for_a_retained_card(self):
        for now in ("2026-10-30T23:59:59Z", "2026-11-01T16:00:00Z"):
            labels, image = labels_for(self.state | {"now": now})
            self.assertIn("NO LOCAL VISIT TO DISPLAY", labels)
            self.assertNotIn("VPS", labels)
        state = entry_render_state({"snapshot": {}}, {}, {"view": "local_visit", "item": self.card}, NOW)
        self.assertEqual(state["selected_card"], self.card)
        self.assertFalse(state["demo"])


class PublicFlightWatchRenderTests(unittest.TestCase):
    def setUp(self):
        self.card = {"id": "public-request", "ident": "DAL123", "date": "2026-10-31", "status": "tracking",
                     "flight": {"id": "public-leg", "number": "DAL123", "origin": "ATL", "destination": "MEM",
                                "start": "2026-10-31T17:00:00Z", "end": "2026-10-31T19:00:00Z"},
                     "live": {"flight_id": "public-leg", "latitude": 34, "longitude": -86,
                              "source": "Fixture provider", "observed_at": NOW.isoformat()}}
        self.state = render.demo_snapshot(NOW) | {"view": "flight_watch", "selected_card": self.card}

    def test_resolved_request_uses_its_own_route_and_fresh_position(self):
        self.state["selected_flight"] = self.state["flights"][2]
        before = deepcopy(self.state)
        labels, image = labels_for(self.state)
        self.assertIn("PUBLIC REQUEST / DAL123", labels)
        self.assertIn("ATL > MEM", labels)
        self.assertIn("LIVE POSITION / Fixture provider", labels)
        self.assertIn("PUBLIC REQUEST / 2026-10-31", labels)
        self.assertEqual(self.state, before)
        self.assertEqual(image.size, (512, 256))

    def test_old_future_or_wrong_flight_positions_never_appear_live(self):
        variants = [self.card | {"status": "scheduled"}, self.card | {"status": "stale"}]
        for live in (self.card["live"] | {"observed_at": (NOW - timedelta(seconds=301)).isoformat()},
                     self.card["live"] | {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
                     self.card["live"] | {"flight_id": "another-flight"},
                     self.card["live"] | {"observed_at": None},
                     self.card["live"] | {"latitude": 100}):
            variants.append(self.card | {"live": live})
        for card in variants:
            labels, image = labels_for(self.state | {"selected_card": card})
            self.assertIn("PLANNED ROUTE / NOT LIVE TRACKING", labels)
            self.assertFalse(any(label.startswith("LIVE POSITION") for label in labels))

    def test_unresolved_statuses_show_request_without_raw_provider_errors(self):
        for status, title in (("unconfigured", "FLIGHT LOOKUP NOT CONFIGURED"), ("pending", "LOOKUP PENDING"),
                              ("ambiguous", "MORE THAN ONE FLIGHT MATCH"), ("not_found", "NO MATCH FOUND"),
                              ("unavailable", "FLIGHT DATA UNAVAILABLE"), ("error", "FLIGHT LOOKUP UNAVAILABLE")):
            card = self.card | {"status": status, "error": "PRIVATE PROVIDER DIAGNOSTIC"}
            labels, image = labels_for(self.state | {"selected_card": card})
            self.assertIn("DAL123", labels)
            self.assertIn("REQUESTED DATE / 2026-10-31", labels)
            self.assertIn(title, labels)
            self.assertNotIn("PRIVATE PROVIDER DIAGNOSTIC", labels)
            self.assertFalse(any(label.startswith("LIVE POSITION") for label in labels))

    def test_display_propagates_public_requests_and_drops_live_on_backend_outage(self):
        status = {"snapshot": {}, "flight_watches": [self.card]}
        state = entry_render_state(status, {}, {"view": "flight_watch", "item": self.card}, NOW)
        self.assertFalse(state["demo"])
        self.assertEqual(state["selected_card"], self.card)
        self.assertEqual(state["flight_watches"], [self.card])
        offline = fresh_household_status(status, {}, NOW, backend_available=False)
        self.assertEqual(offline["flight_watches"][0]["status"], "stale")
        self.assertIsNone(offline["flight_watches"][0]["live"])
        self.assertIsNotNone(self.card["live"])


class TripAndHomeRenderTests(unittest.TestCase):
    def setUp(self):
        legs = []
        for index, (origin, destination) in enumerate((("ATL", "MEM"), ("MEM", "ATL"), ("ATL", "VPS"), ("VPS", "ATL"), ("ATL", "LAX"))):
            start = NOW + timedelta(hours=(2, 5, 8, 20, 25)[index])
            legs.append({"id": "trip-" + str(index), "number": "DEMO " + str(index + 1),
                         "origin": origin, "destination": destination, "start": start.isoformat(),
                         "end": (start + timedelta(hours=2)).isoformat(), "leg_number": (index % 3) + 1,
                         "deadhead": index == 1})
        self.preview = {"start_date": "2026-10-31", "end_date": "2026-11-01", "timezone": "America/Chicago",
                        "days": [{"date": "2026-10-31", "legs": legs[:3]}, {"date": "2026-11-01", "legs": legs[3:]}],
                        "flights": legs, "local_visits": [{"airport": "VPS", "arrival": legs[2]["end"], "duration_minutes": 600}]}
        self.state = {"now": NOW.isoformat(), "trip_preview": self.preview, "trip_page": 0}

    def test_summary_pages_show_every_leg_without_duplicate_flattening(self):
        before = deepcopy(self.state)
        for page, expected in ((0, self.preview["flights"][:3]), (1, self.preview["flights"][3:])):
            labels, image = labels_for(self.state | {"view": "trip_summary", "trip_page": page})
            self.assertIn(f"PAGE {page + 1}/2", labels)
            for leg in expected:
                self.assertIn(f"{leg['origin']} > {leg['destination']}", labels)
            self.assertIn("31 Oct - 01 Nov", labels)
            self.assertEqual(image.size, (512, 256))
        labels, _ = labels_for(self.state | {"view": "trip_summary"})
        self.assertIn("DEADHEAD", labels)
        self.assertIn("VPS VISIT", labels)
        self.assertTrue(any("10H00M STAY" in label for label in labels))
        self.assertEqual(self.state, before)

    def test_map_keeps_all_routes_and_uses_day_colors_with_paged_legend(self):
        page0, image0 = labels_for(self.state | {"view": "trip_map"})
        page1, image1 = labels_for(self.state | {"view": "trip_map", "trip_page": 1})
        self.assertIn("ALL ROUTES / DATE COLORS", page0)
        self.assertIn("LEGEND 1-3 OF 5", page0)
        self.assertIn("LEGEND 4-5 OF 5", page1)
        self.assertIn("Sun 11/01", page1)
        self.assertIn("ATL > LAX", page1)
        self.assertIn("PLANNED TRIP / NOT LIVE TRACKING", page1)
        self.assertFalse(any(label.startswith("LIVE POSITION") for label in page1))
        self.assertNotEqual(image0.crop((15, 59, 325, 202)).tobytes(), image1.crop((15, 59, 325, 202)).tobytes())
        _, rows, _ = render._trip_legs(self.state, {})
        self.assertEqual(rows[0]["_trip_color"], rows[2]["_trip_color"])
        self.assertNotEqual(rows[2]["_trip_color"], rows[3]["_trip_color"])

    def test_trip_map_keeps_airport_codes_and_number_plates_off_the_geography(self):
        calls = []
        original = render._text
        def capture(draw, xy, value, *args, **kwargs):
            calls.append((xy, str(value)))
            return original(draw, xy, value, *args, **kwargs)
        with patch.object(render, "_text", side_effect=capture):
            image = render.render_frame(self.state | {"view": "trip_map"}, {"timezone": "America/Chicago"})
        map_labels = [value for (x, y), value in calls if 15 <= x <= 324 and 59 <= y <= 201]
        self.assertFalse(any(value.isdigit() for value in map_labels))
        self.assertFalse(any(value in {"ATL", "MEM", "VPS", "LAX"} for value in map_labels))
        self.assertIn("ATL > MEM", [value for (x, y), value in calls if x >= 339])
        self.assertEqual(image.size, (512, 256))

    def test_many_legs_are_honestly_paged_and_unknown_airport_is_explicit(self):
        many = [dict(self.preview["flights"][0], id=str(i), leg_number=i + 1) for i in range(13)]
        many[-1]["destination"] = "???"
        preview = self.preview | {"days": [{"date": "2026-10-31", "legs": many}], "flights": many}
        labels, image = labels_for(self.state | {"view": "trip_map", "trip_preview": preview, "trip_page": 4})
        self.assertIn("PAGE 5/5", labels)
        self.assertIn("PAGE ROUTES", labels)
        self.assertIn("LEGEND 13-13 OF 13", labels)
        self.assertIn("MAP COORDS UNAVAILABLE", labels)
        self.assertIn("ATL > ???", labels)

    def test_trip_state_uses_recomputed_entry_preview_and_does_not_mutate_operating_selection(self):
        status = {"snapshot": {}, "trip_preview": {}, "selected_flight": {"id": "operating"}}
        entry = {"view": "trip_map", "trip_page": 1, "trip_preview": self.preview}
        state = entry_render_state(status, {}, entry, NOW)
        self.assertEqual(state["trip_preview"], self.preview)
        self.assertEqual(state["trip_page"], 1)
        self.assertEqual(state["selected_flight"], {"id": "operating"})
        self.assertFalse(state["demo"])

    def test_home_is_local_and_respects_personal_expiry_and_custody_gate(self):
        items = [{"id": "dinner", "kind": "dinner", "title": "FICTIONAL PASTA", "start": NOW.isoformat(),
                  "display_start": (NOW - timedelta(hours=1)).isoformat(), "display_end": (NOW + timedelta(hours=1)).isoformat()},
                 {"id": "appt", "kind": "appointment", "title": "FICTIONAL HAIRCUT", "start": (NOW + timedelta(minutes=30)).isoformat(),
                  "display_start": (NOW - timedelta(hours=1)).isoformat(), "display_end": (NOW + timedelta(hours=1)).isoformat()}]
        state = self.state | {"view": "home", "personal_items": items, "household_context": {}}
        labels, image = labels_for(state)
        self.assertIn("13:00", labels)
        self.assertIn("31 Oct 2026", labels)
        self.assertIn("FICTIONAL HAIRCUT", labels)
        with patch.object(render, "_text", wraps=render._text) as text:
            render.render_frame(state, {"timezone": "America/Chicago", "kids_filter_enabled": True})
            captured = [str(call.args[2]) for call in text.call_args_list]
        self.assertNotIn("FICTIONAL PASTA", captured)
        self.assertIn("FICTIONAL HAIRCUT", captured)
        labels, _ = labels_for(state | {"now": (NOW + timedelta(hours=1)).isoformat()})
        self.assertIn("YOUR HOME BOARD", labels)
        self.assertNotIn("FICTIONAL HAIRCUT", labels)


if __name__ == "__main__":
    unittest.main()
