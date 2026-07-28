"""Config loading guards."""

from __future__ import annotations

import pytest
import yaml

from twair.config import (
    ConfigError,
    _reject_boolean_keys,
    load_conf,
)


class TestNorwayProblem:
    """YAML 1.1 coerces unquoted NO/YES/ON/OFF to booleans.

    This bit for real: the pollutant key `NO` (nitric oxide) became `False`,
    silently removing it from range checking with no error anywhere.
    """

    def test_pyyaml_really_does_this(self) -> None:
        parsed = yaml.safe_load("pollutants:\n  NO:\n    unit: ppb\n")

        assert False in parsed["pollutants"]
        assert "NO" not in parsed["pollutants"]

    def test_quoting_fixes_it(self) -> None:
        parsed = yaml.safe_load('pollutants:\n  "NO":\n    unit: ppb\n')

        assert "NO" in parsed["pollutants"]

    def test_guard_rejects_boolean_keys(self) -> None:
        with pytest.raises(ConfigError, match="boolean key"):
            _reject_boolean_keys({"pollutants": {False: {"unit": "ppb"}}})

    def test_guard_reports_where_the_problem_is(self) -> None:
        with pytest.raises(ConfigError, match="pollutants"):
            _reject_boolean_keys({"pollutants": {True: {}}}, "conf")

    def test_guard_descends_into_lists(self) -> None:
        with pytest.raises(ConfigError):
            _reject_boolean_keys({"rules": [{"ok": 1}, {False: 2}]})

    def test_boolean_values_are_fine(self) -> None:
        _reject_boolean_keys({"pollutants": {"WD_HR": {"circular": True}}})


class TestShippedConfigs:
    @pytest.mark.parametrize("name", ["pollutants", "qc"])
    def test_configs_load_without_boolean_keys(self, name: str) -> None:
        assert load_conf(name)

    def test_nitric_oxide_survives_loading(self) -> None:
        """Regression: this key is the one YAML eats."""
        assert "NO" in load_conf("pollutants")["pollutants"]

    def test_every_pollutant_key_is_a_string(self) -> None:
        keys = load_conf("pollutants")["pollutants"].keys()

        assert all(isinstance(k, str) for k in keys)
