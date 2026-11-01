"""New network consent and paid allowances require local owner configuration."""
import math

import pytest

from flightwall.config import ConfigStore, DEFAULTS, PUBLIC_FIELDS, validate


@pytest.mark.parametrize("key", ("nearby_enrichment_enabled", "nearby_enrichment_monthly_limit_usd",
                                 "trip_weather_enabled", "national_radar_enabled", "airport_alerts_enabled"))
def test_provider_choices_and_optional_budget_cannot_be_changed_remotely(tmp_path, key):
    store = ConfigStore(tmp_path)
    with pytest.raises(ValueError, match="local CLI"):
        store.update({key: True if isinstance(DEFAULTS[key], bool) else .5}, remote=True)


@pytest.mark.parametrize("value", (-1, 1.01, True, math.inf, math.nan))
def test_optional_nearby_allowance_cannot_exceed_one_dollar(value):
    with pytest.raises(ValueError):
        validate("nearby_enrichment_monthly_limit_usd", value)


def test_network_defaults_remain_offline_but_owner_can_set_a_lower_optional_allowance(tmp_path):
    store = ConfigStore(tmp_path)
    for key in ("nearby_enrichment_enabled", "trip_weather_enabled", "national_radar_enabled", "airport_alerts_enabled"):
        assert store.get()[key] is False
    store.update({"nearby_enrichment_monthly_limit_usd": .25})
    assert store.get()["nearby_enrichment_monthly_limit_usd"] == .25
    assert "nearby_enrichment_monthly_limit_usd" not in PUBLIC_FIELDS
