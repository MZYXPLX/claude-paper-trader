"""Load and validate config.yaml, and define where state lives on disk."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ROOT = Path(__file__).resolve().parent.parent
MARKETS = ("crypto", "stocks")


class ConfigError(Exception):
    """Raised when config.yaml is missing or invalid."""


class _Strict(BaseModel):
    # Unknown keys are rejected so a typo in config.yaml fails loudly.
    model_config = ConfigDict(extra="forbid")


class ExperimentConfig(_Strict):
    starting_capital_usd: float = Field(gt=0)
    dry_run: Union[Literal["auto"], bool] = "auto"
    trading_enabled: bool = True


class MarketConfig(_Strict):
    enabled: bool = True
    symbols: list[str] = Field(min_length=1)
    benchmark: str
    fee_bps: float = Field(ge=0, le=500)
    slippage_bps: float = Field(ge=0, le=500)
    max_data_age_minutes: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_symbols(self):
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols contains duplicates")
        if self.benchmark not in self.symbols:
            raise ValueError(f"benchmark {self.benchmark!r} must also be listed in symbols")
        return self

    @property
    def round_trip_cost_bps(self) -> float:
        return 2 * (self.fee_bps + self.slippage_bps)


class CryptoMarketConfig(MarketConfig):
    data_source: Literal["coinbase", "kraken"] = "coinbase"

    @model_validator(mode="after")
    def _check_product_ids(self):
        for symbol in self.symbols:
            parts = symbol.split("-")
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"crypto symbol {symbol!r} must look like BASE-QUOTE, for example BTC-USD")
        return self


class StocksMarketConfig(MarketConfig):
    data_feed: Literal["iex", "sip"] = "iex"
    mirror_claude_to_alpaca_paper: bool = False


class MarketsConfig(_Strict):
    crypto: CryptoMarketConfig
    stocks: StocksMarketConfig


class StrategyConfig(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")
    type: Literal["claude", "rule_trend", "buy_hold"]
    model: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _check_model(self):
        if self.type == "claude" and not self.model:
            raise ValueError(f"strategy {self.id!r} has type claude and needs a model")
        if self.type != "claude" and self.model:
            raise ValueError(f"strategy {self.id!r} has type {self.type} and must not set a model")
        return self


class RiskConfig(_Strict):
    max_position_weight: float = Field(gt=0, le=1)
    min_cash_weight: float = Field(ge=0, lt=1)
    max_turnover_per_run: float = Field(gt=0, le=2)
    min_trade_usd: float = Field(ge=0)
    min_weight_change: float = Field(ge=0, lt=1)
    drawdown_halt: float = Field(gt=0, lt=1)


class ClaudeConfig(_Strict):
    max_tokens: int = Field(ge=256, le=64000)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    request_timeout_seconds: float = Field(gt=0, le=600)
    max_retries: int = Field(ge=0, le=10)
    history_runs_in_prompt: int = Field(ge=0, le=50)


class ModelPrice(_Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class BudgetConfig(_Strict):
    monthly_usd_cap: float = Field(ge=0)
    prices_per_mtok: dict[str, ModelPrice]


class Config(_Strict):
    experiment: ExperimentConfig
    markets: MarketsConfig
    strategies: list[StrategyConfig] = Field(min_length=1)
    risk: RiskConfig
    claude: ClaudeConfig
    budget: BudgetConfig

    @model_validator(mode="after")
    def _check_strategies(self):
        ids = [s.id for s in self.strategies]
        if len(set(ids)) != len(ids):
            raise ValueError("strategy ids must be unique")
        for s in self.strategies:
            if s.type == "claude" and s.model not in self.budget.prices_per_mtok:
                raise ValueError(f"model {s.model!r} (strategy {s.id!r}) has no entry in budget.prices_per_mtok")
        return self

    def market(self, name: str) -> MarketConfig:
        if name not in MARKETS:
            raise ConfigError(f"unknown market {name!r}; expected one of {', '.join(MARKETS)}")
        return getattr(self.markets, name)

    def enabled_strategies(self) -> list[StrategyConfig]:
        return [s for s in self.strategies if s.enabled]


def _format_validation_error(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "(root)"
        lines.append(f"  {where}: {e['msg']}")
    return "\n".join(lines)


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"config file {path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must contain a YAML mapping")
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config in {path}:\n{_format_validation_error(e)}") from e


@dataclass(frozen=True)
class Paths:
    """Where the bot reads and writes. Tests point this at a temporary directory."""

    root: Path = ROOT

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def docs_data(self) -> Path:
        return self.root / "docs" / "data"

    @property
    def fixtures(self) -> Path:
        return self.root / "tests" / "fixtures"

    @property
    def runs_log(self) -> Path:
        return self.state / "runs.jsonl"

    @property
    def costs_log(self) -> Path:
        return self.state / "costs.jsonl"
