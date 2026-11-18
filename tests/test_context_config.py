"""New network consent and paid allowances require local owner configuration."""
import math

import pytest

from flightwall.config import ConfigStore, DEFAULTS, PUBLIC_FIELDS, REMOTE_SETTINGS, validate
from flightwall.onboarding import WIZARD_SETTINGS


@pytest.mark.parametrize("key", ("nearby_enrichment_enabled", "nearby_enrichment_monthly_limit_usd",
                                 "trip_weather_enabled", "national_radar_enabled", "airport_alerts_enabled"))
def test_provider_choices_and_optional_budget_cannot_be_changed_remotely(tmp_path, key):
    store = ConfigStore(tmp_path)
    with pytest.raises(ValueError, match="local CLI"):
        store.update({key: True if isinstance(DEFAULTS[key], bool) else .5}, remote=True)


@pytest.mark.parametrize("value", (-1, 2.001, True, math.inf, math.nan))
def test_optional_nearby_allowance_cannot_exceed_two_dollars(value):
    with pytest.raises(ValueError):
        validate("nearby_enrichment_monthly_limit_usd", value)


def test_network_defaults_remain_offline_and_optional_allowance_is_local_only(tmp_path):
    store = ConfigStore(tmp_path)
    for key in ("nearby_enrichment_enabled", "trip_weather_enabled", "national_radar_enabled", "airport_alerts_enabled"):
        assert store.get()[key] is False
    key = "nearby_enrichment_monthly_limit_usd"
    assert store.get()[key] == 1.0
    assert key not in PUBLIC_FIELDS | REMOTE_SETTINGS | WIZARD_SETTINGS
    with pytest.raises(ValueError, match="local CLI"):
        store.update({key: 2}, remote=True)
    for value in (.25, 1.005, 2):
        store.update({key: value})
        assert ConfigStore(tmp_path).get()[key] == value
