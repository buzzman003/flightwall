"""Actual regional geometry and per-frame aircraft checks, no live providers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from flightwall import nearby_map
from flightwall import render


NOW = datetime(2026, 10, 31, 20, tzinfo=timezone.utc)
# Public regional example, deliberately not read from household settings.
CONFIG = {"demo": False, "nearby_enabled": True, "home_latitude": 30.3935,
          "home_longitude": -86.4958, "nearby_radius_miles": 5}


def aircraft(**changes):
    return {"hex": "abc123", "latitude": 30.40, "longitude": -86.49,
            "observed_at": NOW.isoformat(), "track": 60, **changes}


class NearbyGeographyTests(unittest.TestCase):
    def setUp(self):
        nearby_map._load_asset.cache_clear()
        nearby_map._base_map.cache_clear()

    def test_bundled_public_geometry_has_actual_required_layers(self):
        asset = nearby_map._load_asset()
        self.assertIsNotNone(asset)
        _, features = asset
        for kind in ("land", "water", "road", "runway"):
            self.assertTrue(any(feature[0] == kind for feature in features), kind)
        self.assertTrue(any(feature[3] is True for feature in features))
        metadata = json.loads(nearby_map.ASSET_PATH.read_text(encoding="utf-8"))
        self.assertEqual(metadata["license"], "ODbL-1.0")
        self.assertEqual(metadata["region"], "Fort Walton Beach / Destin, Florida")
        self.assertTrue(metadata["source_timestamp"])
        self.assertNotIn("home_latitude", metadata)
        self.assertNotIn("home_longitude", metadata)

    def test_real_map_pixels_are_independent_of_logical_led_grid(self):
        labels = []
        original = render._text
        def text(draw, xy, value, *args, **kwargs):
            labels.append(str(value))
            return original(draw, xy, value, *args, **kwargs)
        with patch.object(render, "_text", side_effect=text):
            image = nearby_map.render_map(CONFIG, (1110, 555), now=NOW)
        self.assertEqual(image.size, (1110, 555))
        self.assertEqual(image.mode, "RGB")
        self.assertIn("US 98", labels)
        self.assertIn("(C) OPENSTREETMAP CONTRIBUTORS / ODbL", labels)
        self.assertNotIn("DESTIN AIRPORT", labels)
        self.assertNotIn("DTS", labels)
        low = nearby_map.render_map(CONFIG, (296, 148), now=NOW)
        self.assertNotEqual(image.tobytes(), low.resize(image.size, Image.Resampling.NEAREST).tobytes())
        # Known public geography: Gulf and bay are water while Destin's
        # peninsula contains land/roads/runway geometry. No flat radar texture.
        project, _, _, _, _ = nearby_map._projection(30.3935, -86.4958, 5, image.size)
        gulf = tuple(round(v) for v in project((-86.47, 30.35)))
        bay = tuple(round(v) for v in project((-86.49, 30.44)))
        land = tuple(round(v) for v in project((-86.47, 30.397)))
        self.assertEqual(image.getpixel(gulf), nearby_map.WATER)
        self.assertEqual(image.getpixel(bay), nearby_map.WATER)
        self.assertNotEqual(image.getpixel(land), nearby_map.WATER)

    def test_off_demo_unknown_or_unbounded_inputs_fall_back(self):
        for changes in ({"nearby_enabled": False},
                        {"demo": True}, {"home_latitude": None}, {"home_longitude": float("nan")},
                        {"nearby_radius_miles": True}, {"nearby_radius_miles": 0}, {"nearby_radius_miles": 251},
                        {"home_latitude": 90}):
            with self.subTest(changes=changes):
                self.assertIsNone(nearby_map.render_map(CONFIG | changes, (1110, 555), now=NOW))
        self.assertIsNone(nearby_map.render_map({}, (1110, 555), now=NOW))
        for size in ((64, 48), (5000, 2500), (True, 400), (1000, float("nan")), None):
            with self.subTest(size=size):
                self.assertIsNone(nearby_map.render_map(CONFIG, size, now=NOW))

    def test_same_center_cached_background_does_not_cache_aircraft_or_time(self):
        empty = nearby_map.render_map(CONFIG, (1110, 555), now=NOW)
        row = aircraft()
        fresh = nearby_map.render_map(CONFIG, (1110, 555), [row], NOW)
        self.assertNotEqual(empty.tobytes(), fresh.tobytes())
        self.assertEqual(nearby_map._base_map.cache_info().misses, 1)
        self.assertGreaterEqual(nearby_map._base_map.cache_info().hits, 1)
        # Exactly 120 seconds is visible; afterward it disappears even if no
        # backend poll updated the cached aircraft list.
        boundary = nearby_map.render_map(CONFIG, (1110, 555), [row], NOW + timedelta(seconds=120))
        expired = nearby_map.render_map(CONFIG, (1110, 555), [row], NOW + timedelta(seconds=120, microseconds=1))
        self.assertEqual(boundary.tobytes(), fresh.tobytes())
        self.assertEqual(expired.tobytes(), empty.tobytes())
        self.assertEqual(row, aircraft())

    def test_invalid_future_far_or_duplicate_points_are_not_drawn(self):
        empty = nearby_map.render_map(CONFIG, (1110, 555), now=NOW)
        for row in (aircraft(observed_at=(NOW + timedelta(seconds=1)).isoformat()),
                    aircraft(observed_at=None), aircraft(latitude=float("inf")),
                    aircraft(latitude=31), aircraft(hex="not hex")):
            with self.subTest(row=row):
                image = nearby_map.render_map(CONFIG, (1110, 555), [row], NOW)
                self.assertEqual(image.tobytes(), empty.tobytes())
        row = aircraft()
        expected = nearby_map.render_map(CONFIG, (1110, 555), [row], NOW)
        duplicate = aircraft(observed_at=(NOW - timedelta(seconds=1)).isoformat(), latitude=30.42)
        actual = nearby_map.render_map(CONFIG, (1110, 555), [duplicate, row, row], NOW)
        self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_legacy_nautical_radius_keeps_its_units(self):
        parameters = nearby_map._parameters(CONFIG | {"nearby_radius_miles": None, "nearby_radius_nm": 5}, (1110, 555))
        self.assertAlmostEqual(parameters[2], 5 * 1.15078)

    def test_owner_radius_presets_use_actual_bundled_geography_and_exact_filter(self):
        # A received point about seven miles north appears at 10mi, never 5mi.
        row = aircraft(latitude=30.495, longitude=-86.4958)
        for radius in (2.5, 5, 10, 25, 50, 100):
            with self.subTest(radius=radius):
                config = CONFIG | {"nearby_radius_miles": radius}
                empty = nearby_map.render_map(config, (1110, 555), now=NOW)
                actual = nearby_map.render_map(config, (1110, 555), [row], NOW)
                self.assertIsInstance(empty, Image.Image)
                self.assertEqual(empty.size, (1110, 555))
                self.assertEqual(actual.tobytes() == empty.tobytes(), radius <= 5)
        # The wider basemap has its own declared source and no online tiles.
        labels = []
        nearby_map._base_map.cache_clear()
        nearby_map._wide_base_map.cache_clear()
        with patch.object(render, "_text", side_effect=lambda draw, xy, text, *a, **kw: labels.append(text)):
            nearby_map.render_map(CONFIG | {"nearby_radius_miles": 100}, (1110, 555), now=NOW)
        self.assertIn("NATURAL EARTH / LIMITED DETAIL", labels)

    def test_corrupt_missing_or_unlicensed_asset_is_not_silently_displayed(self):
        valid = json.loads(nearby_map.ASSET_PATH.read_text(encoding="utf-8"))
        corrupt = deepcopy(valid)
        corrupt["features"][0]["geometry"]["coordinates"][0][0] = [9999, 9999]
        for content in (None, "{}", "invalid", json.dumps(valid | {"license": "unknown"}), json.dumps(corrupt)):
            path = Mock()
            if content is None:
                path.stat.side_effect = FileNotFoundError
            else:
                path.stat.return_value.st_size = len(content)
                path.read_text.return_value = content
            with self.subTest(content=None if content is None else content[:20]), patch.object(nearby_map, "ASSET_PATH", path):
                nearby_map._load_asset.cache_clear()
                nearby_map._base_map.cache_clear()
                self.assertIsNone(nearby_map.render_map(CONFIG, (1110, 555), now=NOW))


@unittest.skipUnless(importlib.util.find_spec("shapely"), "Shapely is a maintenance/build-only dependency")
class CoastlineExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent.parent / "scripts" / "extract-nearby-map.py"
        spec = importlib.util.spec_from_file_location("extract_nearby_map", path)
        cls.extractor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.extractor)

    def test_directed_coastline_and_real_closed_water_ring(self):
        from shapely.geometry import Point, box
        region = box(0, 0, 10, 10)
        # Eastbound coast: north/left side is land, south/right is sea.
        coast = {"tags": {"natural": "coastline"}, "geometry": [{"lon": -1, "lat": 5}, {"lon": 11, "lat": 5}]}
        land = self.extractor.coastal_land([coast], region)
        self.assertTrue(land.contains(Point(5, 8)))
        self.assertFalse(land.contains(Point(5, 2)))
        broken = deepcopy(coast)
        broken["geometry"][-1] = {"lon": 8, "lat": 5}
        with self.assertRaisesRegex(ValueError, "interior endpoint"):
            self.extractor.coastal_land([broken], region)

    def test_incomplete_water_relations_do_not_fill_open_geometry(self):
        relation = {"members": [{"type": "way", "role": "outer", "geometry": [
            {"lon": 1, "lat": 1}, {"lon": 2, "lat": 1}, {"lon": 2, "lat": 2}]}]}
        self.assertIsNone(self.extractor.relation_water(relation))
        relation["members"][0]["geometry"].append({"lon": 1, "lat": 1})
        self.assertIsNotNone(self.extractor.relation_water(relation))
