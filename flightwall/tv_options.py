"""Keyboard/controller TV options, with bounded discrete map-range controls."""
import json
from .card_preferences import CARD_LABELS, TIMING_LABELS

RADIUS_PRESETS = (2.5, 5, 10, 25, 50, 100)
RADAR_SCOPES = ('national', 'southeast', 'local', 'hyperlocal')
GROUPS = (
    ('DISPLAY', ('brightness', 'dot_gap', 'display_scale_mode', 'rotation_seconds',
                 'led_effect', 'sleep_enabled', 'shopping_ticker_visible')),
    ('CARDS', tuple(CARD_LABELS)),
    ('MAPS', ('nearby_radius_miles', 'national_radar_scope', 'national_radar_flight_only')),
    ('TIMING', tuple('timing:' + name for name in TIMING_LABELS)),
)
LABELS = {
    'brightness': 'Brightness', 'dot_gap': 'Dot spacing',
    'display_scale_mode': 'Display fit', 'rotation_seconds': 'Card seconds',
    'led_effect': 'LED effect', 'sleep_enabled': 'Sleep schedule',
    'shopping_ticker_visible': 'Shopping ticker', 'nearby_radius_miles': 'Aircraft radius',
    'national_radar_scope': 'Radar area', 'national_radar_flight_only': 'Radar schedule',
    **CARD_LABELS,
    **{'timing:' + name: label for name, label in TIMING_LABELS.items()},
}


def nearby_radius(config):
    value = config.get('nearby_radius_miles')
    if value is None:
        value = config.get('nearby_radius_nm', 50) * 1.15078
    return value


def _choice(value, values, direction):
    if value in values:
        return values[max(0, min(len(values) - 1, values.index(value) + direction))]
    if isinstance(value, (int, float)):
        eligible = [v for v in values if v > value] if direction > 0 else [v for v in values if v < value]
        return (min(eligible) if direction > 0 else max(eligible)) if eligible else values[-1 if direction > 0 else 0]
    return values[0]


def next_value(config, name, direction):
    direction = 1 if direction >= 0 else -1
    if name.startswith('timing:') and name[7:] in TIMING_LABELS:
        values = json.loads(config.get('card_seconds', '{}'))
        key = name[7:]
        value = _choice(values.get(key, 0), (0, 5, 10, 15, 20, 30, 45, 60, 90, 120), direction)
        if value:
            values[key] = value
        else:
            values.pop(key, None)
        return json.dumps(values, sort_keys=True, separators=(',', ':'))
    if name in CARD_LABELS or name in {'led_effect', 'sleep_enabled', 'shopping_ticker_visible', 'national_radar_flight_only'}:
        return not config.get(name, True)
    if name == 'nearby_radius_miles':
        return _choice(nearby_radius(config), RADIUS_PRESETS, direction)
    if name == 'national_radar_scope':
        return _choice(config.get(name, 'national'), RADAR_SCOPES, direction)
    if name == 'display_scale_mode':
        return 'integer' if config.get(name, 'fit') == 'fit' else 'fit'
    step, low, high = {'brightness': (.05, .05, 1), 'dot_gap': (.05, 0, .65),
                       'rotation_seconds': (1, 3, 300)}[name]
    return max(low, min(high, round(config.get(name, low) + direction * step, 2)))


def value_label(config, name):
    value = config.get(name, True)
    if name.startswith('timing:'):
        seconds = json.loads(config.get('card_seconds', '{}')).get(name[7:])
        return f'{seconds} SEC' if seconds else 'GLOBAL'
    if name in CARD_LABELS:
        return 'SHOW' if value is True else 'HIDE'
    if name == 'nearby_radius_miles':
        return f'{nearby_radius(config):g} MI'
    if name == 'national_radar_scope':
        return {'national': 'NATIONAL', 'southeast': 'SOUTHEAST', 'local': 'LOCAL / 200 MI',
                'hyperlocal': 'FWB / DESTIN'}.get(config.get(name, 'national'), 'NATIONAL')
    if name == 'national_radar_flight_only':
        return 'FLIGHT DAYS' if config.get(name, True) else 'EVERY DAY'
    if isinstance(value, bool):
        return 'ON' if value else 'OFF'
    return str(config.get(name, 'fit'))


def draw_options(draw, config, group, selected):
    from .render import _text, PANEL, CYAN, WHITE, AMBER, MUTED
    title, rows = GROUPS[group % len(GROUPS)]
    selected %= len(rows)
    start = (selected // 7) * 7
    draw.rectangle((36, 24, 476, 234), fill=PANEL, outline=CYAN)
    _text(draw, (48, 33), 'OPTIONS / ' + title, CYAN)
    _text(draw, (462, 33), f'{group + 1}/{len(GROUPS)}', MUTED, align='right')
    _text(draw, (48, 49), 'TAB/A GROUP  UP/DOWN SELECT  LEFT/RIGHT CHANGE', MUTED)
    for index, name in enumerate(rows[start:start + 7], start):
        y = 72 + (index - start) * 17
        color = AMBER if index == selected else WHITE
        _text(draw, (48, y), ('> ' if index == selected else '  ') + LABELS[name].upper(), color)
        _text(draw, (460, y), value_label(config, name).upper(), color, align='right')
        if name == 'nearby_radius_miles':
            # Visible discrete slider: six supported owner-selected distances.
            points = [250 + n * 22 for n in range(len(RADIUS_PRESETS))]
            draw.line((points[0], y + 12, points[-1], y + 12), fill=MUTED)
            nearest = min(range(len(RADIUS_PRESETS)), key=lambda n: abs(RADIUS_PRESETS[n] - nearby_radius(config)))
            for n, x in enumerate(points):
                draw.rectangle((x - 2, y + 10, x + 2, y + 14), fill=AMBER if n == nearest else MUTED)
    _text(draw, (48, 199), f'ROW {selected + 1}/{len(rows)} / CHANGES SAVE AUTOMATICALLY', MUTED)
    _text(draw, (48, 216), 'S/ESC CLOSE / CARDS SHOW WHEN ELIGIBLE', MUTED)
