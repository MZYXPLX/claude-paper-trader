import pytest
import yaml

from bot.config import ConfigError, load_config

from .conftest import REPO


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def raw():
    return yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))


def test_shipped_config_is_valid(config):
    assert config.experiment.starting_capital_usd == 10000
    assert config.experiment.dry_run == "auto"
    assert [s.id for s in config.enabled_strategies()] == ["claude_sonnet", "rule_trend", "buy_hold"]
    assert config.markets.crypto.round_trip_cost_bps == 80


def test_unknown_key_is_rejected(tmp_path, raw):
    raw["risk"]["max_positon_weight"] = 0.3  # typo
    with pytest.raises(ConfigError, match="max_positon_weight"):
        load_config(write(tmp_path, raw))


def test_weight_out_of_range(tmp_path, raw):
    raw["risk"]["max_position_weight"] = 1.5
    with pytest.raises(ConfigError, match="risk.max_position_weight"):
        load_config(write(tmp_path, raw))


def test_benchmark_must_be_in_symbols(tmp_path, raw):
    raw["markets"]["stocks"]["benchmark"] = "VTI"
    with pytest.raises(ConfigError, match="benchmark"):
        load_config(write(tmp_path, raw))


def test_claude_strategy_needs_model_and_price(tmp_path, raw):
    raw["strategies"][0]["model"] = None
    with pytest.raises(ConfigError, match="needs a model"):
        load_config(write(tmp_path, raw))
    raw["strategies"][0]["model"] = "claude-unknown"
    with pytest.raises(ConfigError, match="prices_per_mtok"):
        load_config(write(tmp_path, raw))


def test_duplicate_strategy_ids(tmp_path, raw):
    raw["strategies"][1]["id"] = "claude_sonnet"
    with pytest.raises(ConfigError, match="unique"):
        load_config(write(tmp_path, raw))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
