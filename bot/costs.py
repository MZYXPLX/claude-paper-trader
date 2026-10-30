"""Claude API cost tracking and the monthly budget cap.

Cost = input * input_price + output * output_price, plus prompt-cache tokens at
1.25x the input price for cache writes and 0.1x for cache reads (Anthropic's 5-minute
cache rates). Prices are per million tokens and come from config.yaml.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import BudgetConfig
from .ledger import append_jsonl, iso, read_jsonl
from .strategies.base import BudgetExceeded

CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10
CHARS_PER_TOKEN_ESTIMATE = 3.0  # conservative: JSON with many numbers tokenizes densely


def usage_dict(usage) -> dict:
    """Normalize an SDK usage object (or dict) to plain token counts."""
    get = usage.get if isinstance(usage, dict) else (lambda k, d=None: getattr(usage, k, d))
    return {
        "input_tokens": int(get("input_tokens", 0) or 0),
        "output_tokens": int(get("output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(get("cache_creation_input_tokens", 0) or 0),
        "cache_read_input_tokens": int(get("cache_read_input_tokens", 0) or 0),
    }


class CostTracker:
    def __init__(self, path: Path, budget: BudgetConfig):
        self.path = path
        self.budget = budget

    def _price(self, model: str):
        try:
            return self.budget.prices_per_mtok[model]
        except KeyError:
            raise KeyError(f"no price configured for model {model!r} in budget.prices_per_mtok") from None

    def cost_of(self, model: str, usage: dict) -> float:
        p = self._price(model)
        u = usage_dict(usage)
        return (
            u["input_tokens"] * p.input
            + u["cache_creation_input_tokens"] * p.input * CACHE_WRITE_MULTIPLIER
            + u["cache_read_input_tokens"] * p.input * CACHE_READ_MULTIPLIER
            + u["output_tokens"] * p.output
        ) / 1_000_000

    def estimate(self, model: str, prompt_chars: int, max_tokens: int) -> float:
        """Worst-case cost of one call: estimated input plus the full max_tokens of output."""
        p = self._price(model)
        input_tokens = prompt_chars / CHARS_PER_TOKEN_ESTIMATE
        return (input_tokens * p.input + max_tokens * p.output) / 1_000_000

    def records(self) -> list[dict]:
        return read_jsonl(self.path)

    def month_to_date(self, now: datetime) -> float:
        prefix = now.strftime("%Y-%m")
        return sum(r["cost_usd"] for r in self.records() if r["timestamp"].startswith(prefix))

    def total(self) -> float:
        return sum(r["cost_usd"] for r in self.records())

    def spend_for(self, market: str, strategy_id: str, since: str | None = None) -> float:
        return sum(
            r["cost_usd"]
            for r in self.records()
            if r["market"] == market and r["strategy_id"] == strategy_id and (since is None or r["timestamp"] >= since)
        )

    def check(self, now: datetime, estimate: float) -> None:
        spent = self.month_to_date(now)
        cap = self.budget.monthly_usd_cap
        if spent + estimate > cap:
            raise BudgetExceeded(
                f"month-to-date API spend ${spent:.4f} plus this call (up to ${estimate:.4f}) would exceed the ${cap:.2f} cap"
            )

    def record(self, *, now: datetime, run_id: str, market: str, strategy_id: str, model: str, usage, request_id=None) -> float:
        u = usage_dict(usage)
        cost = self.cost_of(model, u)
        append_jsonl(
            self.path,
            {
                "timestamp": iso(now),
                "run_id": run_id,
                "market": market,
                "strategy_id": strategy_id,
                "model": model,
                **u,
                "cost_usd": round(cost, 8),
                "request_id": request_id,
            },
        )
        return cost
