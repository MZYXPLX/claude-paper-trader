"""Portfolio state, trades, equity snapshots, decisions, and archive/reset.

Layout under state/:
  <market>/experiment.json                    start time of the current experiment
  <market>/<strategy_id>/portfolio.json       cash, FIFO lots, counters
  <market>/<strategy_id>/trades.jsonl         one line per simulated fill
  <market>/<strategy_id>/equity.csv           one row per run with prices
  <market>/<strategy_id>/decisions.jsonl      one line per decision, with API logs
  runs.jsonl, costs.jsonl                     shared by both markets
"""
from __future__ import annotations

import csv
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Paths

EQUITY_FIELDS = ["timestamp", "run_id", "cash", "positions_value", "equity"]


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


def read_jsonl(path: Path, last: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if last is not None:
        lines = lines[-last:] if last > 0 else []
    return [json.loads(ln) for ln in lines]


@dataclass
class Lot:
    qty: float
    unit_cost: float  # per unit, including the buy fee and slippage
    opened_at: str


@dataclass
class Portfolio:
    strategy_id: str
    market: str
    created_at: str
    starting_capital: float
    cash: float
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)
    peak_equity: float = 0.0
    drawdown_halted: bool = False
    has_traded: bool = False
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    slippage_paid: float = 0.0
    traded_notional: float = 0.0
    trade_count: int = 0
    mock_decisions: int = 0
    live_decisions: int = 0
    last_run_id: str | None = None
    updated_at: str | None = None

    @classmethod
    def new(cls, strategy_id: str, market: str, starting_capital: float, created_at: str) -> "Portfolio":
        return cls(
            strategy_id=strategy_id,
            market=market,
            created_at=created_at,
            starting_capital=starting_capital,
            cash=starting_capital,
            peak_equity=starting_capital,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Portfolio":
        data = dict(data)
        data["lots"] = {s: [Lot(**lot) for lot in lots] for s, lots in data.get("lots", {}).items()}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    def quantity(self, symbol: str) -> float:
        return sum(lot.qty for lot in self.lots.get(symbol, []))

    def cost_basis(self, symbol: str) -> float:
        return sum(lot.qty * lot.unit_cost for lot in self.lots.get(symbol, []))

    def avg_cost(self, symbol: str) -> float | None:
        qty = self.quantity(symbol)
        return self.cost_basis(symbol) / qty if qty > 0 else None

    def held_symbols(self) -> list[str]:
        return [s for s, lots in self.lots.items() if lots and self.quantity(s) > 0]

    def price(self, symbol: str, prices: dict[str, float]) -> float | None:
        return prices.get(symbol, self.last_prices.get(symbol))

    def positions_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for s in self.held_symbols():
            p = self.price(s, prices)
            if p is not None:
                total += self.quantity(s) * p
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.positions_value(prices)

    def weights(self, prices: dict[str, float], universe: list[str]) -> dict[str, float]:
        eq = self.equity(prices)
        out = {s: 0.0 for s in universe}
        if eq <= 0:
            return out
        for s in self.held_symbols():
            p = self.price(s, prices)
            if p is not None:
                out[s] = self.quantity(s) * p / eq
        return out

    @property
    def mode(self) -> str:
        """dry_run, live or mixed, based on the Claude decisions this portfolio has made."""
        if self.mock_decisions and self.live_decisions:
            return "mixed"
        if self.live_decisions:
            return "live"
        if self.mock_decisions:
            return "dry_run"
        return "none"


class Ledger:
    def __init__(self, paths: Paths, market: str):
        self.paths = paths
        self.market = market

    @property
    def market_dir(self) -> Path:
        return self.paths.state / self.market

    def strategy_dir(self, strategy_id: str) -> Path:
        return self.market_dir / strategy_id

    def strategy_ids(self) -> list[str]:
        if not self.market_dir.exists():
            return []
        return sorted(p.name for p in self.market_dir.iterdir() if (p / "portfolio.json").exists())

    # --- experiment ---------------------------------------------------------

    def experiment(self) -> dict | None:
        return read_json(self.market_dir / "experiment.json")

    def start_experiment(self, now: datetime, mode: str, run_id: str) -> dict:
        exp = self.experiment()
        if exp is None:
            exp = {"market": self.market, "started_at": iso(now), "started_mode": mode, "started_run_id": run_id}
            write_json(self.market_dir / "experiment.json", exp)
        return exp

    # --- portfolio ----------------------------------------------------------

    def load_portfolio(self, strategy_id: str, starting_capital: float, created_at: str) -> Portfolio:
        data = read_json(self.strategy_dir(strategy_id) / "portfolio.json")
        if data is None:
            return Portfolio.new(strategy_id, self.market, starting_capital, created_at)
        return Portfolio.from_dict(data)

    def save_portfolio(self, portfolio: Portfolio) -> None:
        write_json(self.strategy_dir(portfolio.strategy_id) / "portfolio.json", portfolio.to_dict())

    # --- logs ---------------------------------------------------------------

    def append_trades(self, strategy_id: str, trades: list[dict]) -> None:
        for trade in trades:
            append_jsonl(self.strategy_dir(strategy_id) / "trades.jsonl", trade)

    def read_trades(self, strategy_id: str, last: int | None = None) -> list[dict]:
        return read_jsonl(self.strategy_dir(strategy_id) / "trades.jsonl", last)

    def append_decision(self, strategy_id: str, record: dict) -> None:
        append_jsonl(self.strategy_dir(strategy_id) / "decisions.jsonl", record)

    def read_decisions(self, strategy_id: str, last: int | None = None) -> list[dict]:
        return read_jsonl(self.strategy_dir(strategy_id) / "decisions.jsonl", last)

    def append_equity(self, strategy_id: str, row: dict) -> None:
        path = self.strategy_dir(strategy_id) / "equity.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=EQUITY_FIELDS)
            if new:
                writer.writeheader()
            writer.writerow({k: row[k] for k in EQUITY_FIELDS})

    def read_equity(self, strategy_id: str) -> list[dict]:
        path = self.strategy_dir(strategy_id) / "equity.csv"
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            for k in ("cash", "positions_value", "equity"):
                r[k] = float(r[k])
        return rows

    # --- reset --------------------------------------------------------------

    def reset(self, now: datetime) -> Path | None:
        """Move this market's state into archive/<UTC timestamp>/<market>/.

        runs.jsonl and costs.jsonl stay in place: the runs log is history, and API spend
        is real money that must keep counting toward the monthly budget.
        """
        if not self.market_dir.exists():
            return None
        dest = self.paths.archive / now.strftime("%Y%m%dT%H%M%SZ") / self.market
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.market_dir), str(dest))
        return dest
