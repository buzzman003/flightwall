import json
from unittest.mock import patch

from PIL import Image, ImageDraw
import pytest

from flightwall.config import DEFAULTS, validate
from flightwall.display import RotationCursor
from flightwall.household_policy import build_rotation
from flightwall.render import _deck_progress, _header
from flightwall.tv_options import next_value, value_label
from datetime import datetime, timezone


def test_pause_and_timed_hold_freeze_card_progress_then_resume():
    entries = [{'key': 'one', 'duration_seconds': 15}, {'key': 'two', 'duration_seconds': 20}]
    cursor = RotationCursor(entries, 0)
    cursor.tick(entries, 5, automatic=False)
    cursor.tick(entries, 95, automatic=False)
    assert cursor.remaining(entries[0], 95) == pytest.approx(2 / 3)
    cursor.tick(entries, 100)
    assert cursor.key == 'one'
    assert cursor.remaining(entries[0], 100) == pytest.approx(2 / 3)
    cursor.hold_until = 146
    cursor.tick(entries, 101)
    cursor.tick(entries, 145)
    assert cursor.key == 'one'
    cursor.tick(entries, 146)
    assert cursor.remaining(entries[0], 146) == pytest.approx(.6)
    cursor.tick(entries, 155)
    assert cursor.key == 'two'


def test_per_card_duration_preserves_global_default_and_rejects_unknown_settings():
    now = datetime(2026, 11, 5, 16, tzinfo=timezone.utc)
    config = DEFAULTS | {'demo': False, 'weather_enabled': True,
                         'card_seconds': '{"weather":20}'}
    entries = build_rotation({'snapshot': {'demo': False, 'flights': []}}, config, now)
    assert next(row for row in entries if row['view'] == 'weather')['duration_seconds'] == 20
    assert next(row for row in entries if row['view'] == 'home')['duration_seconds'] == 15
    assert validate('card_seconds', '{"weather":20}') == '{"weather":20}'
    for bad in ('{"weather":true}', '{"weather":0}', '{"private":20}', '[]'):
        with pytest.raises(ValueError):
            validate('card_seconds', bad)
    value = next_value(config, 'timing:trip_weather', 1)
    assert json.loads(value)['trip_weather'] == 5
    assert value_label(config, 'timing:trip_weather') == 'GLOBAL'


def test_config_cli_accepts_timing_and_owner_trip_json_and_preserves_canonical_private_storage(tmp_path, capsys):
    from flightwall.config import ConfigStore
    from flightwall.main import main

    trips = [{"start_date": "2026-11-06", "end_date": "2026-11-11"}]
    assert main(["--data-dir", str(tmp_path), "config",
                 "--set", "card_seconds", '{"weather":20,"dinner":30}',
                 "--set", "planned_trip_days", json.dumps(trips)]) == 0
    emitted = json.loads(capsys.readouterr().out)
    store = ConfigStore(tmp_path)
    assert emitted["card_seconds"] == store.get()["card_seconds"] == '{"dinner":30,"weather":20}'
    assert emitted["planned_trip_days"] == store.get()["planned_trip_days"] == json.dumps(trips, sort_keys=True, separators=(',', ':'))
    assert "planned_trip_days" not in store.public()


def test_config_cli_rejects_invalid_json_fields_without_partially_saving_settings(tmp_path, capsys):
    from flightwall.config import ConfigStore
    from flightwall.main import main

    store = ConfigStore(tmp_path)
    before = store.get()
    for key, value in (("card_seconds", '{"weather":true}'),
                       ("planned_trip_days", '[{"start_date":"2026-11-06","end_date":"2026-11-11","private":"unapproved"}]')):
        assert main(["--data-dir", str(tmp_path), "config",
                     "--set", "rotation_seconds", "30", "--set", key, value]) == 1
        output = capsys.readouterr()
        assert output.out == "" and "Flight Wall:" in output.err
        assert ConfigStore(tmp_path).get() == before


def test_header_reports_pause_and_delay_and_footer_ring_changes():
    now = datetime.now(timezone.utc)
    for fields, expected in (({'rotation_paused': True}, 'PAUSED'),
                             ({'rotation_hold_seconds': 44}, 'DELAY 44S')):
        with patch('flightwall.render._text') as text:
            _header(ImageDraw.Draw(Image.new('RGB', (512, 256))), fields, now, 'WEATHER')
        assert expected in [call.args[2] for call in text.call_args_list]
    full, half = (Image.new('RGB', (512, 256)) for _ in range(2))
    for image, remaining in ((full, 1), (half, .5)):
        _deck_progress(ImageDraw.Draw(image), {'deck_count': 15, 'deck_index': 11, 'card_remaining': remaining})
    assert full.tobytes() != half.tobytes()
