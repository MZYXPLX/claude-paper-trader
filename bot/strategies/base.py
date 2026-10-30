"""Strategy interface, the decision types, and validation of model-written decisions."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..config import StrategyConfig
from ..data.features import rounded


class InvalidDecision(Exception):
    """A model response that does not match the decision schema."""


class DecisionError(Exception):
    """A strategy could not produce a valid decision this run."""

    def __init__(self, message: str, meta: dict | None = None):
        super().__init__(message)
        self.meta = meta or {}


class BudgetExceeded(Exception):
    """The monthly Claude API budget would be exceeded by this call."""


class TargetDecision(BaseModel):
    """What a strategy wants. The engine turns targets into trades after the guardrails."""

    source: str
    hold: bool = False                       # True = make no trades this run
    targets: dict[str, float] = Field(default_factory=dict)
    reasons: dict[str, str] = Field(default_factory=dict)
    convictions: dict[str, int] = Field(default_factory=dict)
    market_view: str = ""
    risk_level: str | None = None
    notes_for_next_run: str = ""
    warnings: list[str] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)  # API logs: model, usage, latency, raw responses


@dataclass
class Snapshot:
    run_id: str
    market: str
    now: datetime
    universe: list[str]
    benchmark: str
    features: dict[str, dict]       # full precision, per symbol
    stale: dict[str, bool]
    weights: dict[str, float]       # current weight per universe symbol
    portfolio: dict                 # cash, equity, positions, return and drawdown
    risk: dict                      # rules with the numbers filled in
    has_traded: bool
    history: list[dict] = field(default_factory=list)
    recent_trades: list[dict] = field(default_factory=list)

    @property
    def max_invested_weight(self) -> float:
        return self.risk["max_invested_weight"]

    @property
    def max_position_weight(self) -> float:
        return self.risk["max_position_weight"]

    def payload(self) -> dict:
        """The compact JSON the Claude strategy receives."""
        assets = {}
        for symbol in self.universe:
            f = self.features.get(symbol)
            assets[symbol] = {**(rounded(f) if f else {"no_data": True}), "stale": self.stale.get(symbol, True)}
        bench = self.features.get(self.benchmark)
        return {
            "time_utc": self.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "market": self.market,
            "run_id": self.run_id,
            "universe": self.universe,
            "portfolio": self.portfolio,
            "rules": self.risk,
            "assets": assets,
            "benchmark": {"symbol": self.benchmark, **(rounded(bench) if bench else {"no_data": True})},
            "your_previous_decisions": self.history,
            "your_recent_trades": self.recent_trades,
        }


class Strategy(ABC):
    # buy_hold is the benchmark itself, so the position cap and turnover cap do not apply.
    benchmark_exempt = False
    is_claude = False

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.id = cfg.id

    @abstractmethod
    def decide(self, snapshot: Snapshot) -> TargetDecision: ...


# --- model decision schema (Claude and the dry-run mock both use it) ------------


class TargetItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    symbol: str
    target_weight: float = Field(ge=0, le=1)
    conviction: int = Field(ge=1, le=5)
    reason: str

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, v: str) -> str:
        return v.strip()[:200]


class ModelDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    market_view: str
    risk_level: Literal["low", "medium", "high"]
    targets: list[TargetItem]
    notes_for_next_run: str

    @field_validator("market_view")
    @classmethod
    def _trim_view(cls, v: str) -> str:
        return v.strip()[:400]

    @field_validator("notes_for_next_run")
    @classmethod
    def _trim_notes(cls, v: str) -> str:
        return v.strip()[:300]


def decision_json_schema(universe: list[str]) -> dict:
    """JSON schema for structured outputs. Ranges are stated in descriptions and checked by pydantic."""
    return {
        "type": "object",
        "properties": {
            "market_view": {"type": "string", "description": "Your overall view, at most 400 characters."},
            "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
            "targets": {
                "type": "array",
                "description": "One entry per symbol in the universe.",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": list(universe)},
                        "target_weight": {"type": "number", "description": "Fraction of equity, 0 to the max position weight."},
                        "conviction": {"type": "integer", "description": "1 (low) to 5 (high)."},
                        "reason": {"type": "string", "description": "At most 200 characters."},
                    },
                    "required": ["symbol", "target_weight", "conviction", "reason"],
                    "additionalProperties": False,
                },
            },
            "notes_for_next_run": {"type": "string", "description": "At most 300 characters."},
        },
        "required": ["market_view", "risk_level", "targets", "notes_for_next_run"],
        "additionalProperties": False,
    }


def _format_errors(err: ValidationError) -> str:
    parts = []
    for e in err.errors()[:10]:
        where = ".".join(str(p) for p in e["loc"]) or "(root)"
        parts.append(f"{where}: {e['msg']}")
    return "; ".join(parts)


def parse_model_decision(data, universe: list[str], source: str) -> TargetDecision:
    """Validate a model-written decision. Raises InvalidDecision with a readable message."""
    if not isinstance(data, dict):
        raise InvalidDecision("the response must be a JSON object")
    try:
        parsed = ModelDecision.model_validate(data)
    except ValidationError as e:
        raise InvalidDecision(_format_errors(e)) from e

    seen: set[str] = set()
    for item in parsed.targets:
        if item.symbol not in universe:
            raise InvalidDecision(f"unknown symbol {item.symbol!r}; allowed: {', '.join(universe)}")
        if item.symbol in seen:
            raise InvalidDecision(f"symbol {item.symbol!r} appears more than once")
        seen.add(item.symbol)

    missing = [s for s in universe if s not in seen]
    warnings = [f"{s} missing from targets; keeping its current weight" for s in missing]
    return TargetDecision(
        source=source,
        targets={t.symbol: t.target_weight for t in parsed.targets},
        reasons={t.symbol: t.reason for t in parsed.targets},
        convictions={t.symbol: t.conviction for t in parsed.targets},
        market_view=parsed.market_view,
        risk_level=parsed.risk_level,
        notes_for_next_run=parsed.notes_for_next_run,
        warnings=warnings,
    )
