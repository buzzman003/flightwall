"""Owner choices persist without changing consent, providers or privacy gates."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw
import pytest

from flightwall.card_preferences import CARD_DEFAULTS, card_enabled
from flightwall.config import ConfigStore, DEFAULTS, REMOTE_SETTINGS, validate
from flightwall.display import attach_flight_context, attach_nearby_map
from flightwall.tv_options import (GROUPS, RADAR_SCOPES, RADIUS_PRESETS, draw_options,
                                  nearby_radius, next_value, value_label)


def test_defaults_retain_existing_deck_and_do_not_enable_any_provider():
    assert all(DEFAULTS[key] is True for key in CARD_DEFAULTS)
    assert not DEFAULTS['national_radar_enabled']
    assert not DEFAULTS['nearby_enabled']
    assert DEFAULTS['national_radar_flight_only'] is True
    assert 'national_radar_enabled' not in REMOTE_SETTINGS
    assert set(CARD_DEFAULTS) <= REMOTE_SETTINGS


def test_discrete_range_slider_moves_both_directions_and_stops_at_bounds():
    config = deepcopy(DEFAULTS) | {'nearby_radius_miles': 2.5}
    for expected in RADIUS_PRESETS[1:]:
        config['nearby_radius_miles'] = next_value(config, 'nearby_radius_miles', 1)
        assert config['nearby_radius_miles'] == expected
    assert next_value(config, 'nearby_radius_miles', 1) == 100
    for expected in reversed(RADIUS_PRESETS[:-1]):
        config['nearby_radius_miles'] = next_value(config, 'nearby_radius_miles', -1)
        assert config['nearby_radius_miles'] == expected
    assert next_value(config, 'nearby_radius_miles', -1) == 2.5
    assert next_value({'nearby_radius_miles': 7}, 'nearby_radius_miles', -1) == 5
    assert next_value({'nearby_radius_miles': 7}, 'nearby_radius_miles', 1) == 10
    assert nearby_radius({'nearby_radius_miles': None, 'nearby_radius_nm': 5}) == pytest.approx(5.7539)


def test_scope_and_card_preferences_save_without_touching_provider_flags(tmp_path):
    config = ConfigStore(tmp_path)
    config.update({'kids_filter_enabled': True, 'guest_mode': True}, remote=False)
    initial = config.get()
    changes = {**{key: False for key in CARD_DEFAULTS}, 'national_radar_scope': 'hyperlocal',
               'national_radar_flight_only': False, 'nearby_radius_miles': 25}
    config.update(changes, remote=True)
    persisted = ConfigStore(tmp_path).get()
    assert all(persisted[key] == value for key, value in changes.items())
    for key in initial.keys() - changes.keys():
        assert initial[key] == persisted[key]
    for scope in RADAR_SCOPES:
        assert validate('national_radar_scope', scope) == scope
    for invalid in ('regional', '', [], True):
        with pytest.raises(ValueError):
            validate('national_radar_scope', invalid)


@pytest.mark.parametrize('view,flag', [('nearby', 'nearby_card_visible'),
    ('national_radar', 'radar_card_visible'), ('operating_map', 'flight_cards_visible')])
def test_cached_hires_layers_cannot_paint_a_hidden_card(view, flag):
    state = {'view': view, 'demo': False}
    config = DEFAULTS | {'demo': False, flag: False, 'nearby_enabled': True,
                         'national_radar_enabled': True}
    connection = Mock()
    frame = Image.new('RGB', (512, 256))
    with patch('flightwall.nearby_map.render_map') as nearby, patch('flightwall.operating_map.render_map') as operating:
        attach_nearby_map(frame, state, config, connection, (1920, 1080), datetime.now(timezone.utc))
        attach_flight_context(frame, state, config, connection, (1920, 1080), datetime.now(timezone.utc))
        nearby.assert_not_called()
        operating.assert_not_called()
    connection.local_map_config.assert_not_called()
    connection.local_radar_image.assert_not_called()
    assert 'geographic_patch' not in frame.info


def test_every_options_row_renders_and_uses_a_valid_allowlisted_setting():
    for index, (_title, rows) in enumerate(GROUPS):
        for selected, name in enumerate(rows):
            frame = Image.new('RGB', (512, 256))
            draw_options(ImageDraw.Draw(frame), DEFAULTS, index, selected)
            assert name in REMOTE_SETTINGS
            assert value_label(DEFAULTS, name)
            assert validate(name, next_value(DEFAULTS, name, 1)) is not None
            assert frame.getbbox() is not None


def test_private_views_keep_their_own_gates_and_personal_toggles_are_separate():
    hidden = {key: False for key in CARD_DEFAULTS}
    assert not card_enabled('personal', hidden, {'kind': 'dinner'})
    assert card_enabled('personal', hidden | {'appointment_cards_visible': True}, {'kind': 'appointment'})
    assert card_enabled('lisa_private', hidden)
    assert card_enabled('home', hidden)


def test_keyboard_and_gamepad_navigate_options_and_save_only_chosen_fields(monkeypatch):
    import pygame
    from flightwall.display import run_gui
    from flightwall.render import demo_snapshot
    monkeypatch.setenv('SDL_VIDEODRIVER', 'dummy')
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    saved = []
    class Connection:
        def status(self):
            return {'config': deepcopy(DEFAULTS), 'snapshot': demo_snapshot()}
        def settings(self, values):
            saved.append(values)
    class Screen:
        gl = None
        def __init__(self, *args):
            pass
        def draw(self, frame, config):
            pass
    events = [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_s),
              pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB),
              pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT),
              pygame.event.Event(pygame.JOYBUTTONDOWN, button=0),
              pygame.event.Event(pygame.JOYHATMOTION, value=(1, 0)),
              pygame.event.Event(pygame.JOYHATMOTION, value=(0, -1)),
              pygame.event.Event(pygame.JOYHATMOTION, value=(1, 0))]
    with patch('flightwall.display.ShaderScreen', Screen), patch.object(pygame.event, 'get', return_value=events):
        run_gui(Connection(), windowed=True, max_frames=1)
    assert saved == [{'flight_cards_visible': False}, {'nearby_radius_miles': 100},
                     {'national_radar_scope': 'southeast'}]
