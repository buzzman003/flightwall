"""Offline aircraft thumbnail loading and unshaded TV presentation."""
import csv
from datetime import datetime, timedelta, timezone

from PIL import Image
import pytest

from flightwall import aircraft_icons
from flightwall.display import _present_frame, attach_nearby_photos
from flightwall.render import display_content_bounds


NOW = datetime(2026, 11, 18, 18, tzinfo=timezone.utc)
CONFIG = {"nearby_enabled": True, "nearby_radius_miles": 5}
SCREEN = (1536, 768)


def test_bundled_catalog_photos_have_credits_and_can_be_loaded():
    catalog = aircraft_icons.PHOTO_PATH
    with (catalog / "ATTRIBUTION.tsv").open(encoding="utf-8", newline="") as source:
        credits = list(csv.DictReader(source, delimiter="\t"))
    codes = {credit["type"] for credit in credits}
    assert codes == {path.stem for path in catalog.glob("*.jpg")}
    assert len(codes) == len(credits)
    assert {"C172", "B407", "BE9L", "D328", "F900"} <= codes
    for credit in credits:
        assert all(credit[field] for field in ("title", "creator", "source", "license", "changes"))
        assert credit["source"].startswith("https://")
        assert credit["license"].startswith("https://")
        assert aircraft_icons.representative_photo(credit["type"]) is not None


@pytest.fixture
def photo_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(aircraft_icons, "PHOTO_PATH", tmp_path)
    aircraft_icons._representative_photo.cache_clear()
    aircraft_icons.photo_panel.cache_clear()
    yield tmp_path
    aircraft_icons._representative_photo.cache_clear()
    aircraft_icons.photo_panel.cache_clear()


def _write_jpeg(folder, code, size=(76, 60), color=(120, 200, 40)):
    Image.new("RGB", size, color).save(folder / (code + ".jpg"))


def _aircraft(index, code, east, age=0):
    return {"hex": f"abc{index:03x}", "aircraft_type": code, "east_miles": east,
            "north_miles": 0, "latitude": 30.4, "longitude": -86.5,
            "observed_at": (NOW - timedelta(seconds=age)).isoformat()}


def test_catalog_accepts_only_bounded_matching_jpegs(photo_folder):
    for code in ("C172", "B407", "BE9L", "D328", "F900", "C146"):
        _write_jpeg(photo_folder, code)
        photo = aircraft_icons.representative_photo(code)
        assert photo is aircraft_icons.representative_photo(code)
        assert photo.mode == "RGB" and photo.size == (76, 60) and not photo.info
    (photo_folder / "OVER.jpg").write_bytes(b"x" * (aircraft_icons.MAX_PHOTO_BYTES + 1))
    Image.new("RGB", (76, 60)).save(photo_folder / "PNG.jpg", format="PNG")
    _write_jpeg(photo_folder, "WIDE", (641, 1))
    for code in ("OVER", "PNG", "WIDE", "NICE", "../C172", "c172", b"C172", "ABC12"):
        assert aircraft_icons.representative_photo(code) is None
    assert aircraft_icons.thumbnail("B350").mode == "RGBA"


def test_inner_photos_use_the_same_strip_inset_and_keep_crisp_board(photo_folder):
    _write_jpeg(photo_folder, "C172")
    state = {"view": "nearby", "nearby_status": "ready", "nearby": [_aircraft(1, "C172", 1)],
             "timers": [{"id": "tea", "label": "Tea", "ends_at": (NOW + timedelta(minutes=5)).isoformat()}],
             "shopping_list": {"items": [{"id": "milk", "name": "milk"}]}}
    frame = Image.new("RGB", (512, 256), (255, 255, 255))
    attach_nearby_photos(frame, state, CONFIG, SCREEN, NOW, .5, .2)
    left, top, width, height = display_content_bounds(state, NOW, .5, .2)
    expected_box = (left + 454 * width / 512, top + 80 * height / 256,
                    38 * width / 512, 30 * height / 256)
    [(box, photo)] = frame.info["aircraft_patches"]
    assert box == expected_box
    attach_nearby_photos(frame, state, CONFIG, SCREEN, NOW, .5, .2)
    assert frame.info["aircraft_patches"][0][1] is photo
    output = _present_frame(frame, {"led_effect": True, "dot_gap": .5, "brightness": .5}, SCREEN)
    x = round((box[0] + box[2] / 2) * 3)
    y = round((box[1] + box[3] / 2) * 3)
    dimmed = tuple(value // 2 for value in photo.getpixel((photo.width // 2, photo.height // 2)))
    assert all(output.getpixel((x + i, y)) == dimmed for i in range(4))
    assert set(output.crop((0, 0, 9, 9)).getdata()) == {(0, 0, 0), (127, 127, 127)}


def test_outer_photos_are_ranked_clipped_to_rows_and_expire(photo_folder):
    for index in range(12):
        _write_jpeg(photo_folder, f"X{index:03d}", color=(10 * index, 70, 80))
    rows = [_aircraft(i, f"X{i:03d}", 6 + i) for i in reversed(range(12))]
    state = {"view": "nearby", "nearby_status": "ready", "nearby": [], "nearby_outer": rows}
    frame = Image.new("RGB", (512, 256))
    attach_nearby_photos(frame, state, CONFIG, SCREEN, NOW)
    patches = frame.info["aircraft_patches"]
    assert len(patches) == 10
    assert [round(photo.getpixel((photo.width // 2, photo.height // 2))[0] / 10)
            for _, photo in patches] == list(range(10))
    assert [box for box, _ in patches] == [(472, 86 + 13 * i, 23, 12) for i in range(10)]
    attach_nearby_photos(frame, state, CONFIG, SCREEN, NOW + timedelta(seconds=121))
    assert "aircraft_patches" not in frame.info


@pytest.mark.parametrize("overrides", [
    {"view": "weather"}, {"demo": True}, {"backend_available": False}, {"nearby_status": "stale"},
])
def test_outer_photos_do_not_render_without_a_current_traffic_view(photo_folder, overrides):
    _write_jpeg(photo_folder, "F900")
    state = {"view": "nearby", "nearby_status": "ready", "nearby": [],
             "nearby_outer": [_aircraft(1, "F900", 7)], **overrides}
    frame = Image.new("RGB", (512, 256))
    attach_nearby_photos(frame, state, CONFIG, SCREEN, NOW)
    assert "aircraft_patches" not in frame.info
