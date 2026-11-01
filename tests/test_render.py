"""Rendering contracts: meaningful time, map, privacy and live-data checks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from flightwall import render


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.state = render.demo_snapshot("2026-10-21T14:20:00+00:00")
        self.config = {"timezone": "America/New_York", "show_deadheads": True}

    def test_all_views_are_native_rgb_and_deterministic(self):
        for view in ("details", "map", "schedule", "nearby", "personal"):
            with self.subTest(view=view):
                self.state["view"] = view
                a = render.render_frame(self.state, self.config)
                b = render.render_frame(self.state, self.config)
                self.assertEqual(a.mode, "RGB")
                self.assertEqual(a.size, (512, 256))
                self.assertEqual(a.tobytes(), b.tobytes())
                self.assertGreater(len(a.getcolors(maxcolors=1000)), 4)

    def test_demo_is_visible_on_every_view(self):
        for view in ("details", "map", "schedule", "nearby", "personal"):
            self.state["view"] = view
            image = render.render_frame(self.state, self.config)
            # DEMO is an outlined amber badge; header coordinates are fixed.
            self.assertEqual(image.getpixel((348, 9)), render.AMBER)
            self.assertEqual(image.getpixel((380, 22)), render.AMBER)

    def test_airport_coordinates_are_in_the_correct_hemisphere(self):
        atl, lax, mem = (render._airport(code) for code in ("ATL", "LAX", "MEM"))
        self.assertAlmostEqual(atl["lat"], 33.64, delta=0.02)
        self.assertAlmostEqual(atl["lon"], -84.43, delta=0.02)
        self.assertLess(lax["lon"], mem["lon"])
        self.assertGreater(mem["lat"], atl["lat"])
        for airport in render._asset("airports.json").values():
            self.assertTrue(-90 <= airport["lat"] <= 90)
            self.assertTrue(-180 <= airport["lon"] <= 180)

    def test_regional_and_long_routes_have_different_maps(self):
        self.state["view"] = "map"
        regional = render.render_frame(self.state, self.config)
        self.state["selected_flight"] = self.state["flights"][2]
        national = render.render_frame(self.state, self.config)
        self.assertNotEqual(regional.crop((15, 57, 497, 202)).tobytes(), national.crop((15, 57, 497, 202)).tobytes())
        # Both maps contain real land and administrative outlines.
        for image in (regional, national):
            colors = {color: count for count, color in image.crop((15, 57, 497, 202)).getcolors(maxcolors=1000)}
            self.assertGreater(colors.get((29, 60, 68), 0), 100)

    def test_great_circle_follows_spherical_geometry(self):
        route = render.great_circle((-84.4281, 33.6367), (-118.408, 33.9425))
        self.assertAlmostEqual(route[0][0], -84.4281, places=4)
        self.assertAlmostEqual(route[-1][0], -118.408, places=4)
        self.assertGreater(max(lat for lon, lat in route), 34.5)
        dateline = render.great_circle((170, 35), (-170, 35))
        self.assertTrue(all(abs(lon) >= 170 - 1e-6 for lon, lat in dateline))

    def test_calendar_cannot_become_a_live_position(self):
        self.state["view"] = "map"
        ordinary = render.render_frame(self.state, self.config).tobytes()
        for live in (True, {"latitude": 34, "longitude": -85}, {"source": "provider", "latitude": 150, "longitude": -85}):
            self.state["live"] = live
            self.assertEqual(render.render_frame(self.state, self.config).tobytes(), ordinary)
        self.state["live"] = {"source": "Test provider", "latitude": 34, "longitude": -86}
        self.assertNotEqual(render.render_frame(self.state, self.config).tobytes(), ordinary)

    def test_times_and_header_clock_use_the_configured_zone(self):
        labels = []
        original = render._text

        def capture(draw, xy, value, *args, **kwargs):
            labels.append(str(value))
            return original(draw, xy, value, *args, **kwargs)

        with patch.object(render, "_text", side_effect=capture):
            render.render_frame(self.state, self.config)
        self.assertIn("10:20 AM EDT", labels)
        self.assertNotIn("2:20 PM UTC", labels)
        self.assertIn("DEPART / EDT", labels)
        self.assertIn("12:20 PM", labels)
        self.assertIn("REPORT 11:25 AM EDT", labels)

    def test_completed_legs_and_hidden_deadheads_are_not_selected(self):
        now = render._dt(self.state["now"])
        old = deepcopy(self.state["flights"][0])
        old["start"] = (now - timedelta(hours=3)).isoformat()
        old["end"] = (now - timedelta(hours=2)).isoformat()
        old["id"] = "old"
        self.state["flights"].insert(0, old)
        flights = render._flights(self.state, {"show_deadheads": False}, now)
        self.assertEqual(len(flights), 3)
        self.assertEqual(render._selected(self.state, flights)["id"], "demo-0")

    def test_empty_invalid_and_unknown_airport_states_remain_usable(self):
        for view in ("details", "map", "schedule", "nearby", "personal"):
            image = render.render_frame({"now": self.state["now"], "view": view}, {"timezone": "No/SuchZone"})
            self.assertEqual(image.size, (512, 256))
        self.state["view"] = "map"
        self.state["selected_flight"] = {"origin": "???", "destination": "LAX"}
        self.assertEqual(render.render_frame(self.state, self.config).size, (512, 256))

    def test_render_does_not_mutate_calendar_snapshot(self):
        before = deepcopy(self.state)
        render.render_frame(self.state, self.config, size=(1024, 512))
        self.assertEqual(self.state, before)

    def test_nearby_uses_real_feed_flight_and_source_fields(self):
        self.state["view"] = "nearby"
        self.state["demo"] = False
        self.config.update(nearby_enabled=True, nearby_radius_miles=5)
        self.state["nearby"] = [{"flight": "DAL123", "source": "ADSB.lol", "altitude": 32000,
                                  "latitude": 33.6, "longitude": -84.4, "hex": "abc123",
                                  "east_miles": .5, "north_miles": .2,
                                  "observed_at": self.state["now"]}]
        labels = []
        original = render._text

        def capture(draw, xy, value, *args, **kwargs):
            labels.append(str(value))
            return original(draw, xy, value, *args, **kwargs)

        with patch.object(render, "_text", side_effect=capture):
            render.render_frame(self.state, self.config)
        self.assertTrue(any("DAL123" in label for label in labels))
        self.assertIn("NEARBY AIRCRAFT / ADSB.LOL", labels)
        self.assertTrue(any(label.startswith("ROUTE N/A / ") for label in labels))


class PersonalRenderTests(unittest.TestCase):
    def setUp(self):
        self.config = {"timezone": "America/Chicago", "demo": False}
        self.appointment = {
            "id": "fictional-appointment", "kind": "appointment", "title": "Riley's appointment",
            "start": "2026-10-22T15:25:00+00:00", "end": "2026-10-22T16:25:00+00:00",
            "display_start": "2026-10-22T04:00:00+00:00", "display_end": "2026-10-22T16:25:00+00:00",
            "timezone": "America/New_York", "source": "Alexa",
        }
        self.dinner = {
            "id": "fictional-dinner", "kind": "dinner", "title": "Teriyaki chicken",
            "start": "2026-10-22T18:30:00+00:00", "end": "2026-10-22T23:00:00+00:00",
            "display_start": "2026-10-22T18:30:00+00:00", "display_end": "2026-10-22T23:00:00+00:00",
            "timezone": "America/New_York", "source": "Alexa",
        }
        self.state = {"now": "2026-10-22T14:15:00+00:00", "view": "personal", "demo": True,
                      "personal_items": [self.appointment], "selected_personal_item": self.appointment}

    def labels(self, state=None):
        labels = []
        original = render._text

        def capture(draw, xy, value, *args, **kwargs):
            labels.append(str(value))
            return original(draw, xy, value, *args, **kwargs)

        with patch.object(render, "_text", side_effect=capture):
            render.render_frame(state or self.state, self.config)
        return labels

    def test_appointment_displays_in_household_timezone_with_explicit_am_pm(self):
        labels = self.labels()
        self.assertIn("RILEY'S APPOINTMENT", labels)
        self.assertIn("10:25 AM", labels)
        self.assertIn("11:25 AM", labels)
        self.assertIn("START / CDT", labels)
        self.assertIn("ENDS / CDT", labels)
        self.assertNotIn("START / EDT", labels)
        self.assertIn("TODAY / Thu 22 Oct", labels)
        self.assertIn("STARTS IN 1H 10M", labels)
        self.assertIn("VIA ALEXA", labels)
        self.assertIn("SAMPLE CARD / FICTIONAL EXAMPLE", labels)
        self.assertNotIn("CONNECT YOUR FLIGHT CALENDAR", labels)

    def test_appointment_clears_at_end_but_is_visible_from_local_midnight(self):
        midnight = render._dt("2026-10-22T04:00:00+00:00")
        self.assertEqual(render._selected_personal(self.state, midnight), self.appointment)
        self.assertIsNone(render._selected_personal(self.state, midnight - timedelta(seconds=1)))
        ending = render._dt(self.appointment["end"])
        self.assertIsNone(render._selected_personal(self.state, ending))
        self.state["now"] = "2026-10-22T16:25:00+00:00"
        self.assertIn("YOUR HOME BOARD IS CLEAR", self.labels())

    def test_appointment_status_changes_when_it_starts(self):
        self.state["now"] = "2026-10-22T15:25:00+00:00"
        self.assertIn("APPOINTMENT IN PROGRESS", self.labels())

    def test_dinner_shows_meal_and_clearing_time_not_a_serving_time(self):
        self.state.update(now="2026-10-22T20:00:00+00:00", personal_items=[self.dinner], selected_personal_item=self.dinner)
        labels = self.labels()
        self.assertIn("TONIGHT'S DINNER", labels)
        self.assertIn("TERIYAKI CHICKEN", labels)
        self.assertIn("CLEARS AT 6:00 PM CDT", labels)
        self.assertNotIn("START / CDT", labels)
        self.assertIsNone(render._selected_personal(self.state, render._dt(self.dinner["display_start"]) - timedelta(seconds=1)))
        self.assertEqual(render._selected_personal(self.state, render._dt(self.dinner["display_start"])), self.dinner)
        self.assertIsNone(render._selected_personal(self.state, render._dt(self.dinner["display_end"])))

    def test_selection_respects_app_choice_and_ignores_invalid_entries(self):
        second = {**self.appointment, "id": "fictional-second", "title": "Demo second appointment"}
        self.state["personal_items"] = [None, {"kind": "flight"}, self.appointment, second]
        self.state["selected_personal_item"] = second
        now = render._dt(self.state["now"])
        self.assertEqual(render._selected_personal(self.state, now), second)
        self.state["selected_personal_item"] = second["id"]
        self.assertEqual(render._selected_personal(self.state, now), second)
        self.state.pop("selected_personal_item")
        self.assertEqual(render._selected_personal(self.state, now), self.appointment)

    def test_titles_wrap_within_card_and_do_not_expose_unused_fields(self):
        self.appointment["title"] = "Fictional appointment with a very long example title " + "X" * 80
        self.appointment["description"] = "DO NOT SHOW PRIVATE METADATA"
        calls = []
        original = render._text

        def capture(draw, xy, value, color=render.WHITE, scale=1, **kwargs):
            calls.append((xy, str(value), scale, kwargs))
            return original(draw, xy, value, color, scale, **kwargs)

        with patch.object(render, "_text", side_effect=capture):
            image = render.render_frame(self.state, self.config)
        title_lines = [(xy, value, scale) for xy, value, scale, kw in calls if 63 <= xy[1] <= 134 and xy[0] == 25]
        self.assertGreater(len(title_lines), 1)
        for xy, value, scale in title_lines:
            self.assertLessEqual(render._width(value, scale), 464)
            self.assertLessEqual(xy[1] + 7 * scale, 135)
        self.assertFalse(any("PRIVATE METADATA" in value for xy, value, scale, kw in calls))
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (512, 256))

    def test_personal_render_does_not_mutate_inputs_and_is_deterministic(self):
        before = deepcopy(self.state)
        image = render.render_frame(self.state, self.config)
        self.assertEqual(image.tobytes(), render.render_frame(self.state, self.config).tobytes())
        self.assertEqual(self.state, before)
        self.assertEqual(image.getpixel((348, 9)), render.AMBER)

    def test_personal_without_flight_calendar_has_its_own_footer(self):
        self.state["demo"] = False
        labels = self.labels()
        self.assertIn("PERSONAL BOARD / Alexa", labels)
        self.assertNotIn("CONNECT YOUR FLIGHT CALENDAR", labels)


class HouseholdViewTests(unittest.TestCase):
    def setUp(self):
        self.now = "2026-10-22T18:00:00+00:00"
        self.context = {"chris_home": False, "chris_away": True, "lisa_home": True,
                        "lisa_away": False, "kids_home": False, "kids_away": True,
                        "private_lisa": True, "flight_priority": False, "trip_away": True}
        self.config = {"timezone": "America/New_York", "lisa_private_enabled": True, "guest_mode": False,
                       "health_enabled": True, "inspiration_enabled": True, "messages_enabled": True}
        self.card = {"id": "fictional-card", "profile": "lisa", "title": "Fictional example title",
                     "body": "Example card content for rendering tests only.", "source": "Example Publisher",
                     "source_url": "https://example.org/original-source", "published_at": "2026-10-21T12:00:00+00:00",
                     "observed_at": self.now, "category": "personal"}
        self.state = {"now": self.now, "demo": True, "selected_card": self.card, "household_context": self.context}

    def labels(self, state=None, config=None):
        labels = []
        original = render._text

        def capture(draw, xy, value, *args, **kwargs):
            labels.append(str(value))
            return original(draw, xy, value, *args, **kwargs)

        with patch.object(render, "_text", side_effect=capture):
            image = render.render_frame(state or self.state, config or self.config)
        return labels, image

    def test_new_views_remain_native_deterministic_and_visibly_demo(self):
        self.card.update(start="2026-10-22T17:00:00+00:00", end="2026-10-22T20:00:00+00:00", airport="MEM")
        for view in ("message", "inspiration", "health", "layover", "setup"):
            self.state["view"] = view
            a = render.render_frame(self.state, self.config)
            b = render.render_frame(self.state, self.config)
            self.assertEqual(a.mode, "RGB")
            self.assertEqual(a.size, (512, 256))
            self.assertEqual(a.tobytes(), b.tobytes())
            self.assertEqual(a.getpixel((348, 9)), render.AMBER)

    def test_unknown_or_changed_privacy_hides_retained_lisa_cards(self):
        for view in ("message", "inspiration", "health"):
            for context in (None, {}, self.context | {"private_lisa": False}, self.context | {"kids_away": False}):
                state = self.state | {"view": view, "household_context": context}
                labels, image = self.labels(state)
                self.assertIn("NO CARD TO DISPLAY", labels)
                self.assertFalse(any("FICTIONAL EXAMPLE TITLE" in value.upper() for value in labels))
            labels, image = self.labels(self.state | {"view": view}, self.config | {"guest_mode": True})
            self.assertIn("NO CARD TO DISPLAY", labels)

    def test_health_displays_actual_source_identity_date_and_domain(self):
        labels, image = self.labels(self.state | {"view": "health"})
        self.assertIn("SOURCE / Example Publisher", labels)
        self.assertIn("21 Oct 2026", labels)
        self.assertIn("example.org", labels)
        self.card.pop("observed_at")
        labels, image = self.labels(self.state | {"view": "health"})
        self.assertIn("NO CARD TO DISPLAY", labels)

    def test_inspiration_never_opens_a_path_supplied_in_state(self):
        self.card["profile"] = "public"
        state = self.state | {"view": "inspiration", "household_context": {}, "artwork_image": "/etc/passwd"}
        with patch("PIL.Image.open", side_effect=AssertionError("Do not open paths")):
            image = render.render_frame(state, self.config)
        self.assertEqual(image.size, (512, 256))
        self.assertGreater(len(image.crop((15, 63, 159, 202)).getcolors(maxcolors=1000)), 8)

    def test_layover_hotel_requires_user_provision_or_explicit_optin(self):
        self.card.update(airport="MEM", start="2026-10-22T17:00:00+00:00", end="2026-10-22T20:00:00+00:00",
                         hotel={"name": "FICTIONAL DEMO HOTEL", "address": "FICTIONAL ADDRESS"})
        state = self.state | {"view": "layover"}
        labels, image = self.labels(state)
        self.assertNotIn("FICTIONAL DEMO HOTEL", labels)
        self.card["hotel_user_provided"] = True
        labels, image = self.labels(state)
        self.assertIn("FICTIONAL DEMO HOTEL", labels)
        self.assertIn("FICTIONAL ADDRESS", labels)
        self.assertIn("HOTEL / PROVIDED BY YOU", labels)

    def test_chris_message_requires_home_and_marks_reply_needed(self):
        self.card.update(profile="chris", category="rental", needs_reply=True, reply_label="REPLY REQUESTED")
        state = self.state | {"view": "message"}
        self.assertIn("NO CARD TO DISPLAY", self.labels(state)[0])
        state["household_context"] = self.context | {"chris_home": True, "chris_away": False, "private_lisa": False}
        labels, image = self.labels(state)
        self.assertIn("MESSAGE FOR CHRIS", labels)
        self.assertIn("REPLY REQUESTED", labels)

    def test_postflight_schedule_does_not_claim_a_past_leg_is_next(self):
        state = render.demo_snapshot(self.now)
        flight = state["flights"][0]
        flight["start"], flight["end"] = "2026-10-22T14:00:00+00:00", "2026-10-22T17:30:00+00:00"
        state["selected_flight"] = flight
        labels, image = self.labels(state)
        self.assertIn("MY RECENT LEG / SCHEDULE", labels)
        self.assertIn("SCHEDULED LEG ENDED", labels)
        self.assertNotIn("MY NEXT LEG / SCHEDULE", labels)

    def test_retained_dinner_is_hidden_if_kids_presence_becomes_unknown(self):
        item = {"kind": "dinner", "title": "Fictional dinner", "start": "2026-10-22T17:00:00+00:00",
                "end": "2026-10-22T20:00:00+00:00", "display_start": "2026-10-22T17:00:00+00:00", "display_end": "2026-10-22T20:00:00+00:00"}
        state = self.state | {"view": "personal", "selected_personal_item": item, "household_context": {}}
        config = self.config | {"kids_filter_enabled": True}
        self.assertIn("YOUR HOME BOARD IS CLEAR", self.labels(state, config)[0])
        state["household_context"] = {"kids_home": True}
        self.assertIn("FICTIONAL DINNER", self.labels(state, config)[0])


if __name__ == "__main__":
    unittest.main()
