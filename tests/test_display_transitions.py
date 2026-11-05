"""Actual GUI navigation/reveals use fictional cards and immediate privacy gates."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import pytest

from flightwall import display
from flightwall.config import DEFAULTS
from flightwall.render import compose_display_overlays


NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)
RED, GREEN, BLUE, PRIVATE = (177, 13, 23), (27, 101, 211), (38, 170, 69), (201, 7, 197)
SLOTS = [{"key": view, "view": view, "duration_seconds": 15} for view in ("home", "map", "schedule")]


def status_for(**config):
    return {"config": DEFAULTS | {"demo": False, "timezone": "America/Chicago", **config},
            "snapshot": {"flights": [], "demo": False, "last_sync": NOW.isoformat()},
            "presence": {}, "kids": {}, "personal_items": []}


def household_status(*, shared=False, expires=.15):
    status = status_for(presence_enabled=True, lisa_private_enabled=True, inspiration_enabled=True)
    status["presence"] = {person: {"state": state, "stale": False, "last_seen": NOW.isoformat()}
                          for person, state in (("chris", "away"), ("lisa", "home"))}
    status["kids"] = {"state": "away", "stale": False, "last_sync": NOW.isoformat(),
                      "until": (NOW + timedelta(hours=2)).isoformat()}
    status["inspiration"] = [{"id": "fictional-private", "owner": "lisa", "title": "Fictional private artwork",
                              "display_start": (NOW - timedelta(hours=1)).isoformat(),
                              "display_end": (NOW + timedelta(seconds=expires)).isoformat()}]
    if shared:
        status["personal_items"] = [{"id": "shared", "kind": "appointment", "title": "Fictional shared card",
                                     "start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat(),
                                     "display_start": (NOW - timedelta(hours=1)).isoformat(),
                                     "display_end": (NOW + timedelta(hours=1)).isoformat()}]
    return status


def gui(monkeypatch, events, *, times=None, status=None, slots=SLOTS, fail_fetch=False, wall_start=NOW, gl=False):
    pygame = pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    data = deepcopy(status or status_for())
    ticks, wall, index = [0.0], [wall_start], [-1]
    states, frames, caches, rates, patches, settings, transitions = [], [], [], [], [], [], []

    class Connection:
        count = 0
        def status(self):
            self.count += 1
            if fail_fetch and self.count > 1:
                raise OSError("fictional backend outage")
            return deepcopy(data)
        def settings(self, value):
            settings.append(value)
            data["config"].update(value)

    class Transition(display.CardTransition):
        def __init__(self):
            super().__init__(chooser=lambda effects: effects[0])
            transitions.append(self)

    class Screen:
        def __init__(self, *args):
            self.gl = gl
            self.screen = pygame.Surface((1280, 720))
        def draw(self, frame, config):
            frames.append(frame.copy())
            animation = transitions[0]
            caches.append((animation.active, animation._last is None, animation._previous is None))

    def get_events():
        index[0] += 1
        ticks[0] = times[index[0]] if times else index[0] * .1
        wall[0] = wall_start + timedelta(seconds=ticks[0])
        return events[index[0]]

    def render(state, config):
        states.append(deepcopy(state))
        color = {"home": RED, "map": GREEN, "schedule": BLUE, "personal": GREEN,
                 "inspiration": PRIVATE, "nearby": GREEN, "weather": GREEN}.get(state["view"], BLUE)
        return Image.new("RGB", (512, 256), color)

    with patch.object(display, "datetime") as date, patch.object(display, "DisplayScreen", Screen), \
            patch.object(display, "CardTransition", Transition), patch.object(display, "render_frame", side_effect=render), \
            patch.object(display.time, "monotonic", side_effect=lambda: ticks[0]), \
            patch.object(pygame.time, "Clock", return_value=SimpleNamespace(tick=lambda rate: rates.append(rate))), \
            patch.object(pygame.event, "get", side_effect=get_events), \
            patch.object(pygame.joystick, "get_count", return_value=0), \
            patch.object(display, "attach_nearby_map", side_effect=lambda *args: patches.append((index[0], args[1]["view"]))):
        date.now.side_effect = lambda *_: wall[0]
        if slots is None:
            display.run_gui(Connection(), windowed=True, max_frames=len(events))
        else:
            with patch.object(display, "build_rotation", return_value=slots):
                display.run_gui(Connection(), windowed=True, max_frames=len(events))
    return SimpleNamespace(states=states, frames=frames, caches=caches, rates=rates, patches=patches,
                           settings=settings, transition=transitions[0])


def key(pygame, value):
    return pygame.event.Event(pygame.KEYDOWN, key=value)


def test_keyboard_dpad_and_analog_navigation_wrap_and_settings_keep_their_controls(monkeypatch):
    pygame = pytest.importorskip("pygame")
    axis = lambda value: pygame.event.Event(pygame.JOYAXISMOTION, axis=0, value=value, instance_id=7)
    hat = lambda value: pygame.event.Event(pygame.JOYHATMOTION, value=value)
    result = gui(monkeypatch, [
        [key(pygame, pygame.K_LEFT)], [key(pygame, pygame.K_RIGHT)], [key(pygame, pygame.K_TAB)],
        [key(pygame, pygame.K_s)], [key(pygame, pygame.K_RIGHT)], [hat((-1, 0))], [axis(.9)],
        [key(pygame, pygame.K_s), axis(.95)], [axis(.2)], [axis(-.7)], [axis(-.95)],
        [axis(-.4)], [axis(-.3)], [axis(-.6)], [hat((1, 0))],
    ])
    assert [state["deck_index"] for state in result.states] == [2, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 2, 0]
    assert result.settings == [{"brightness": .75}, {"brightness": .7}]


def test_stick_hysteresis_is_independent_for_each_controller():
    stick = display.AnalogNavigation()
    assert stick.direction(1, .6) == 1
    assert stick.direction(1, .9) == stick.direction(1, -.9) == 0
    assert stick.direction(2, -.6) == -1
    assert stick.direction(1, .31) == 0
    assert stick.direction(1, .3) == 0
    assert stick.direction(1, -.6) == -1
    stick.remove(1)
    assert stick.direction(1, .6) == 1
    assert stick.direction(3, float("nan")) == 0


@pytest.mark.parametrize(("gl", "active_rate"), ((False, 12), (True, 30)))
def test_gui_effect_runs_at_transition_rate_and_settles_at_exact_new_card(monkeypatch, gl, active_rate):
    pygame = pytest.importorskip("pygame")
    result = gui(monkeypatch, [[], [key(pygame, pygame.K_RIGHT)], [], []], times=[0, .1, .2, .4], gl=gl)
    assert result.frames[1].getpixel((256, 128)) == RED  # Exact start of the reveal.
    assert result.caches[1][0] and result.caches[2][0]
    assert active_rate in result.rates[1:]
    assert result.frames[3].getpixel((256, 128)) == GREEN
    assert not result.caches[3][0]


def test_same_key_content_change_and_guard_changes_drop_cached_pixels():
    animation = display.CardTransition(chooser=lambda effects: "wipe_left")
    old, current = Image.new("RGB", (512, 256), RED), Image.new("RGB", (512, 256), GREEN)
    animation.render(old, "a", b"guard", 0, view="home")
    animation.render(current, "b", b"guard", .1, view="personal")
    assert animation.active
    assert animation.render(current, "b", b"replacement", .15, view="personal").tobytes() == current.tobytes()
    assert not animation.active
    for view in ("message", "inspiration", "health", "layover", "weather", "nearby", "flight_watch"):
        animation.render(old, "a", b"guard", 0, view="home")
        animation.render(current, "b", b"guard", .1, view="personal")
        animation.render(current, "private", b"guard", .15, view=view)
        assert not animation.active and animation._last is None and animation._previous is None


def test_signature_detects_same_key_content_presence_config_and_live_cutoff_but_not_timer_tick():
    status = household_status(shared=True, expires=30)
    entries = [{"key": "same", "view": "personal", "item": status["personal_items"][0]}]
    baseline = display.transition_signature(status, status["config"], entries, True, NOW)
    changed = deepcopy(entries); changed[0]["item"]["title"] = "Changed shared card"
    assert display.transition_signature(status, status["config"], changed, True, NOW) != baseline
    changed_status = deepcopy(status); changed_status["presence"]["lisa"]["state"] = "away"
    assert display.transition_signature(changed_status, status["config"], entries, True, NOW) != baseline
    assert display.transition_signature(status, status["config"] | {"guest_mode": True}, entries, True, NOW) != baseline
    assert display.transition_signature(status, status["config"], entries, False, NOW) != baseline
    ticking = status | {"timers": [{"remaining_seconds": 5}], "shopping_list": {"items": [{"name": "Milk"}]}}
    assert display.transition_signature(ticking, status["config"], entries, True, NOW) == baseline
    live = status | {"live": {"observed_at": (NOW - timedelta(seconds=300)).isoformat()}}
    assert display.transition_signature(live, status["config"], entries, True, NOW) != display.transition_signature(
        live, status["config"], entries, True, NOW + timedelta(microseconds=1))


def test_private_expiry_in_actual_gui_never_animates_the_old_private_card(monkeypatch):
    pygame = pytest.importorskip("pygame")
    result = gui(monkeypatch, [[key(pygame, pygame.K_2)], [], []], times=[0, .1, .2],
                 status=household_status(expires=.15), slots=None)
    assert result.states[0]["view"] == "inspiration"
    assert result.frames[0].getpixel((256, 128)) == PRIVATE
    assert result.caches[0] == (False, True, True)
    assert result.states[2]["view"] == "home"
    assert result.frames[2].getpixel((256, 128)) == RED
    assert PRIVATE not in set(result.frames[2].getdata())


@pytest.mark.parametrize("outage", (False, True))
def test_guest_or_backend_loss_mid_reveal_immediately_forgets_all_old_pixels(monkeypatch, outage):
    pygame = pytest.importorskip("pygame")
    status = household_status(shared=True, expires=30)
    events = [[], [key(pygame, pygame.K_RIGHT)], [] if outage else [key(pygame, pygame.K_g)]]
    result = gui(monkeypatch, events, times=[1.8, 1.9, 2.01], status=status, slots=None, fail_fetch=outage)
    assert result.caches[1][0]
    assert result.caches[2] == (False, True, True)
    assert result.frames[2].getpixel((256, 128)) == GREEN
    assert RED not in set(result.frames[2].crop((0, 0, 512, 230)).getdata())


def test_dinner_five_pm_cutoff_cancels_an_active_reveal_exactly(monkeypatch):
    pygame = pytest.importorskip("pygame")
    boundary = datetime(2026, 10, 31, 22, tzinfo=timezone.utc)  # 5 p.m. CDT.
    result = gui(monkeypatch, [[], [key(pygame, pygame.K_RIGHT)], []], times=[0, .1, .2],
                 wall_start=boundary - timedelta(seconds=.15))
    assert result.caches[1][0]
    assert not result.caches[2][0]
    assert result.frames[2].getpixel((256, 128)) == GREEN


def test_timer_and_ticker_are_live_layers_outside_the_card_reveal(monkeypatch):
    pygame = pytest.importorskip("pygame")
    status = status_for()
    status["timers"] = [{"id": "fictional-timer", "label": "TEA", "ends_at": (NOW + timedelta(seconds=1.15)).isoformat()}]
    status["shopping_list"] = {"items": [{"name": "Fictional milk and bread"}]}
    result = gui(monkeypatch, [[], [key(pygame, pygame.K_RIGHT)], []], times=[0, .1, .2], status=status)
    assert result.caches[1][0] and result.caches[2][0]
    for index, elapsed in enumerate((0, .1, .2)):
        expected = compose_display_overlays(Image.new("RGB", (512, 256)),
                    {"timers": status["timers"], "shopping_list": status["shopping_list"], "shopping_ticker_offset": elapsed * 12},
                    NOW + timedelta(seconds=elapsed))
        assert result.frames[index].crop((0, 0, 512, 36)).tobytes() == expected.crop((0, 0, 512, 36)).tobytes()
        assert result.frames[index].crop((0, 238, 512, 256)).tobytes() == expected.crop((0, 238, 512, 256)).tobytes()
    assert result.frames[0].crop((0, 0, 512, 36)).tobytes() != result.frames[2].crop((0, 0, 512, 36)).tobytes()


def test_detailed_map_is_attached_only_after_a_card_effect_ends(monkeypatch):
    pygame = pytest.importorskip("pygame")
    slots = [{"key": view, "view": view, "duration_seconds": 15} for view in ("home", "map")]
    result = gui(monkeypatch, [[], [key(pygame, pygame.K_RIGHT)], [], []], times=[0, .1, .2, .4], slots=slots)
    assert (1, "map") not in result.patches and (2, "map") not in result.patches
    assert (3, "map") in result.patches
