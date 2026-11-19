"""Validate local photo privacy and high-resolution display with fictional images."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import pytest

from flightwall import display
from flightwall.config import ConfigStore, DEFAULTS
from flightwall.couple_photos import LocalPhotoDeck, VIEW, eligible
from flightwall.render import compose_display_overlays


NOW = datetime(2026, 1, 1, 18, tzinfo=timezone.utc)


def household(chris="home", lisa="away"):
    config = DEFAULTS | {"demo": False, "presence_enabled": True}
    status = {"config": config, "snapshot": {"demo": False, "flights": []},
              "presence": {person: {"state": value, "stale": False, "last_seen": NOW.isoformat()}
                           for person, value in (("chris", chris), ("lisa", lisa))}}
    return status, config


@pytest.fixture
def album(tmp_path):
    (tmp_path / "config.json").write_text('{"couple_photos_enabled": true}', encoding="utf-8")
    directory = tmp_path / "couple-photos"
    directory.mkdir()
    for number, color in ((1, (180, 20, 30)), (2, (30, 180, 40))):
        Image.new("RGB", (1600, 900), color).save(directory / f"{number:02d}.jpg")
    return directory


@pytest.mark.parametrize("chris,lisa,result", [("home", "away", True), ("away", "home", True),
    ("home", "home", False), ("away", "away", False), ("unknown", "home", False), ("away", "unknown", False)])
def test_card_requires_one_fresh_home_and_one_fresh_away(chris, lisa, result):
    status, config = household(chris, lisa)
    assert eligible(display.fresh_household_status(status, config, NOW), config) is result
    assert not eligible(display.fresh_household_status(status, config, NOW, backend_available=False), config)
    assert not eligible(display.fresh_household_status(status, config | {"guest_mode": True}, NOW),
                        config | {"guest_mode": True})
    expiry = NOW + timedelta(minutes=config["presence_ttl_minutes"])
    assert not eligible(display.fresh_household_status(status, config, expiry), config)


def test_local_opt_in_and_rotation_ignore_non_jpegs_and_release_picture_when_hidden(album):
    Image.new("RGB", (30, 20), (0, 0, 200)).save(album / "03.jpg", format="PNG")
    Image.new("RGB", (30, 20), (0, 0, 200)).save(album / "private file.jpg")
    store = ConfigStore(album.parent)
    assert store.get()["couple_photos_enabled"] is True
    assert "couple_photos_enabled" not in store.public()
    with pytest.raises(ValueError, match="local CLI"):
        store.update({"couple_photos_enabled": False}, remote=True)
    deck = LocalPhotoDeck(album.parent)
    assert deck.refresh(0, True, NOW.date())
    first = deck.select(True)
    assert first.width == 1600 and first.getpixel((20, 20))[0] > 160
    assert not first.info and deck.select(True) is first
    assert not deck.refresh(.1, False, NOW.date())
    assert deck.fit((500, 500)) is None
    assert deck.refresh(.2, True, NOW.date())
    second = deck.select(True)
    assert second.getpixel((20, 20))[1] > 160
    deck.select(False)
    assert deck.select(True).getpixel((20, 20))[0] > 160
    (album.parent / "config.json").write_text(json.dumps({"couple_photos_enabled": False}), encoding="utf-8")
    assert not deck.refresh(1.3, True, NOW.date())
    assert deck.select(True) is None


def test_whole_album_rotates_without_a_repeat_and_weekly_order_survives_relaunch(album):
    for number in range(1, 9):
        Image.new("RGB", (32, 18), (number * 25, 10, 20)).save(album / f"{number:02d}.jpg")
    sunday = NOW.date() + timedelta(days=6 - NOW.weekday())
    monday = sunday + timedelta(days=1)

    def cards(deck, tick, day):
        assert deck.refresh(tick, True, day)
        found = []
        for _ in range(8):
            deck.select(False)
            picture = deck.select(True)
            found.append(round(picture.getpixel((0, 0))[0] / 25))
        return found

    deck = LocalPhotoDeck(album.parent)
    previous_week = cards(deck, 0, sunday)
    new_week = cards(LocalPhotoDeck(album.parent), .1, monday)
    assert set(previous_week) == set(new_week) == set(range(1, 9))
    assert previous_week != new_week
    assert cards(deck, .1, monday)[0] != previous_week[-1]
    assert cards(LocalPhotoDeck(album.parent), 5, monday + timedelta(days=2)) == new_week


def test_photo_uses_clear_presentation_pixels_and_stays_between_overlays(album):
    deck = LocalPhotoDeck(album.parent)
    assert deck.refresh(0, True, NOW.date()) and deck.select(True) is not None
    state = {"view": VIEW, "timers": [{"id": "test", "label": "Fixture",
              "starts_at": NOW.isoformat(), "ends_at": (NOW + timedelta(hours=1)).isoformat()}],
             "shopping_list": {"items": [{"name": "Fixture milk"}]}}
    config = {"led_effect": True, "brightness": .5, "dot_gap": .6}
    frame = compose_display_overlays(Image.new("RGB", (512, 256)), state, NOW, 1, 1, presentation=True)
    display.attach_couple_photo(frame, state, config, deck, (1920, 1080), NOW, 1, 1)
    picture, box = display._project_layer(frame, (0, 0, 1920, 960), frame.info["couple_photo"])
    assert picture.width > 512 and abs(picture.width / picture.height - 16 / 9) < .01
    x, y, w, h = box
    assert y > 50 and y + h < 910
    output = display._present_frame(frame, config, (1920, 960))
    point = (x + w // 2, y + h // 2)
    color = tuple(channel // 2 for channel in picture.getpixel((picture.width // 2, picture.height // 2)))
    assert output.getpixel(point) == output.getpixel((point[0] + 1, point[1])) == color


@pytest.mark.parametrize("loss", ["guest", "stale", "outage"])
def test_paused_photo_is_removed_on_every_privacy_transition(monkeypatch, album, loss):
    pygame = pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    status, config = household()
    ticks, now, frames, index = [0.0], [NOW], [], [-1]

    class Connection:
        data_dir = album.parent

        def status(self):
            if loss == "outage" and index[0] >= 2:
                raise OSError("synthetic outage")
            return deepcopy(status)

        def settings(self, _):
            raise OSError("synthetic save failure")

    class Screen:
        gl = None

        def __init__(self, *_):
            self.screen = pygame.Surface((1920, 1080))

        def draw(self, frame, _):
            frames.append("couple_photo" in frame.info)

    def events():
        index[0] += 1
        if index[0] == 1:
            ticks[0] = .5
            return [pygame.event.Event(pygame.KEYDOWN, key=key) for key in (pygame.K_RIGHT, pygame.K_SPACE)]
        if index[0] == 2:
            ticks[0] = 3 if loss == "outage" else .75
            if loss == "stale":
                now[0] += timedelta(minutes=config["presence_ttl_minutes"])
            if loss == "guest":
                return [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_g)]
        return []

    with patch.object(display, "DisplayScreen", Screen), patch.object(display, "datetime") as date, \
            patch.object(display.time, "monotonic", side_effect=lambda: ticks[0]), \
            patch.object(pygame.event, "get", side_effect=events), \
            patch.object(pygame.time, "Clock", return_value=SimpleNamespace(tick=lambda *_: None)):
        date.now.side_effect = lambda *_: now[0]
        display.run_gui(Connection(), windowed=True, max_frames=3, initial_status=status)
    assert frames == [False, True, False]
