"""CLI entry point.

    python -m bot.main --market crypto|stocks [--dry-run] [--offline] [--reset]

One run: fetch prices, compute features, ask each strategy for target weights, apply
the guardrails, simulate fills, and record everything under state/. There is no
live-trading code path anywhere in this program.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .config import Config, ConfigError, Paths, load_config
from .costs import CostTracker
from .data import MarketData, MarketDisabled, utcnow
from .data.features import compute_features
from .execution import rebalance
from .ledger import Ledger, Portfolio, append_jsonl, iso
from .risk import apply_guardrails
from .strategies import build_strategy
from .strategies.base import BudgetExceeded, DecisionError, Snapshot, Strategy

log = logging.getLogger("bot")

# Worst first; a run's status is the worst status of any part of it.
STATUS_ORDER = ["failed", "failed_data", "budget_exceeded", "decision_error", "skipped_disabled", "skipped_market_closed", "ok"]


def worst(statuses) -> str:
    statuses = list(statuses) or ["ok"]
    return min(statuses, key=STATUS_ORDER.index)


# --- data providers -------------------------------------------------------------


class FixtureProvider:
    """Offline data: recorded fixtures, no network calls. The market counts as open."""

    def __init__(self, market: str, paths: Paths, now: datetime):
        self.market, self.paths, self.now = market, paths, now

    def fetch(self, symbols: list[str]) -> MarketData:
        from .data.fixtures import load_fixtures

        return load_fixtures(self.market, symbols, self.paths.fixtures, self.now)


def build_provider(market: str, config: Config, paths: Paths, offline: bool, now: datetime, env: Mapping[str, str]):
    market_cfg = config.market(market)
    if offline:
        return FixtureProvider(market, paths, now)
    if market == "crypto":
        from .data.crypto import CryptoDataSource

        return CryptoDataSource(primary=market_cfg.data_source)
    from .data.stocks import StockDataSource

    return StockDataSource.from_env(feed=market_cfg.data_feed, env=env)


# --- run -----------------------------------------------------------------------


@dataclass
class RunContext:
    config: Config
    paths: Paths
    market: str
    run_id: str
    now: datetime
    dry_run: bool
    offline: bool
    ledger: Ledger
    costs: CostTracker
    env: Mapping[str, str]

    @property
    def mode(self) -> str:
        return "dry_run" if self.dry_run else "live"


def resolve_dry_run(config: Config, args, env: Mapping[str, str]) -> bool:
    has_key = bool(env.get("ANTHROPIC_API_KEY", "").strip())
    if args.offline or args.dry_run:
        return True
    setting = config.experiment.dry_run
    if setting == "auto":
        return not has_key
    if setting is False and not has_key:
        raise ConfigError("experiment.dry_run is false but ANTHROPIC_API_KEY is not set")
    return bool(setting)


def make_run_id(market: str, now: datetime) -> str:
    return f"{market}-{now.strftime('%Y%m%dT%H%M%SZ')}"


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def portfolio_summary(p: Portfolio, prices: dict[str, float], universe: list[str]) -> dict:
    equity = p.equity(prices)
    positions = []
    for s in p.held_symbols():
        price = p.price(s, prices)
        qty = p.quantity(s)
        value = qty * price if price is not None else None
        basis = p.cost_basis(s)
        positions.append(
            {
                "symbol": s,
                "quantity": float(f"{qty:.8g}"),
                "avg_cost": float(f"{p.avg_cost(s):.6g}"),
                "market_value": _r(value),
                "weight": _r(value / equity, 4) if value is not None and equity > 0 else None,
                "unrealized_pnl_pct": _r((value / basis - 1) * 100) if value is not None and basis > 0 else None,
            }
        )
    peak = max(p.peak_equity, equity)
    return {
        "starting_capital": p.starting_capital,
        "cash": _r(p.cash),
        "equity": _r(equity),
        "return_since_start_pct": _r((equity / p.starting_capital - 1) * 100),
        "drawdown_from_peak_pct": _r((1 - equity / peak) * 100 if peak > 0 else 0.0),
        "positions": positions,
    }


def decision_history(ledger: Ledger, strategy_id: str, n: int) -> list[dict]:
    out = []
    for d in ledger.read_decisions(strategy_id, last=n):
        before = d.get("weights_before") or {}
        after = d.get("final_targets") or {}
        changes = {
            s: {"from": round(before.get(s, 0.0), 3), "to": round(w, 3)}
            for s, w in after.items()
            if abs(w - before.get(s, 0.0)) >= 0.005
        }
        out.append(
            {
                "time_utc": d.get("timestamp"),
                "status": d.get("status"),
                "market_view": d.get("market_view"),
                "risk_level": d.get("risk_level"),
                "target_changes": changes,
                "notes_for_next_run": d.get("notes_for_next_run"),
            }
        )
    return out


def recent_trades(ledger: Ledger, strategy_id: str, n: int = 10) -> list[dict]:
    return [
        {
            "time_utc": t["timestamp"],
            "symbol": t["symbol"],
            "side": t["side"],
            "quantity": float(f"{t['quantity']:.6g}"),
            "fill_price": float(f"{t['fill_price']:.6g}"),
            "notional": round(t["notional"], 2),
            "reason": t.get("reason", ""),
        }
        for t in ledger.read_trades(strategy_id, last=n)
    ]


def run_strategy(ctx: RunContext, strategy: Strategy, md: MarketData, features: dict, stale: dict, experiment: dict) -> dict:
    config, ledger = ctx.config, ctx.ledger
    market_cfg = config.market(ctx.market)
    risk = config.risk
    universe = market_cfg.symbols
    p = ledger.load_portfolio(strategy.id, config.experiment.starting_capital_usd, experiment["started_at"])

    prices = {s: sd.last_price for s, sd in md.symbols.items() if s in universe}
    p.last_prices.update(prices)
    prices = {s: p.last_prices[s] for s in universe if s in p.last_prices}
    equity_before = p.equity(prices)
    p.peak_equity = max(p.peak_equity, equity_before)
    drawdown = 1 - equity_before / p.peak_equity if p.peak_equity > 0 else 0.0
    halted = drawdown >= risk.drawdown_halt
    if halted and not p.drawdown_halted:
        log.warning("%s: drawdown %.1f%% reached the halt level; only reductions allowed", strategy.id, drawdown * 100)
    p.drawdown_halted = halted
    weights = p.weights(prices, universe)

    rules = {
        "long_only": True,
        "max_position_weight": risk.max_position_weight,
        "max_invested_weight": round(1 - risk.min_cash_weight, 6),
        "min_cash_weight": risk.min_cash_weight,
        "max_turnover_per_run": risk.max_turnover_per_run,
        "min_trade_usd": risk.min_trade_usd,
        "min_weight_change": risk.min_weight_change,
        "fee_bps": market_cfg.fee_bps,
        "slippage_bps": market_cfg.slippage_bps,
        "round_trip_cost_bps": market_cfg.round_trip_cost_bps,
        "drawdown_halt": risk.drawdown_halt,
        "drawdown_halt_active": halted,
        "trading_enabled": config.experiment.trading_enabled,
    }
    snapshot = Snapshot(
        run_id=ctx.run_id,
        market=ctx.market,
        now=ctx.now,
        universe=universe,
        benchmark=market_cfg.benchmark,
        features=features,
        stale=stale,
        weights=weights,
        portfolio=portfolio_summary(p, prices, universe),
        risk=rules,
        has_traded=p.has_traded,
        history=decision_history(ledger, strategy.id, config.claude.history_runs_in_prompt) if strategy.is_claude else [],
        recent_trades=recent_trades(ledger, strategy.id) if strategy.is_claude else [],
    )

    status, error, decision, meta = "ok", None, None, {}
    try:
        decision = strategy.decide(snapshot)
        meta = decision.meta
    except BudgetExceeded as e:
        status, error = "budget_exceeded", str(e)
        log.warning("%s: %s", strategy.id, e)
    except DecisionError as e:
        status, error, meta = "decision_error", str(e), e.meta
        log.error("%s: decision error: %s", strategy.id, e)
    except Exception as e:  # a bug in one strategy must not stop the others
        status, error = "decision_error", f"{type(e).__name__}: {e}"
        log.error("%s: unexpected error\n%s", strategy.id, traceback.format_exc())

    trades, adjustments, notes, final = [], [], [], dict(weights)
    if decision is not None and not decision.hold:
        proposed = {**{s: weights[s] for s in universe}, **decision.targets}
        result = apply_guardrails(
            proposed,
            weights,
            universe=universe,
            stale={s for s, is_stale in stale.items() if is_stale} | {s for s in universe if s not in prices},
            equity=equity_before,
            risk=risk,
            trading_enabled=config.experiment.trading_enabled,
            drawdown_halted=halted,
            benchmark_exempt=strategy.benchmark_exempt,
        )
        adjustments, final = result.adjustments, result.targets
        changed = result.changed(weights)
        if changed:
            trades, notes = rebalance(
                p,
                changed,
                prices,
                fee_bps=market_cfg.fee_bps,
                slippage_bps=market_cfg.slippage_bps,
                now=ctx.now,
                run_id=ctx.run_id,
                reasons=decision.reasons,
                order=universe,
            )

    if strategy.is_claude and decision is not None:
        if decision.source == "mock_claude":
            p.mock_decisions += 1
        else:
            p.live_decisions += 1

    equity_after = p.equity(prices)
    p.peak_equity = max(p.peak_equity, equity_after)
    p.last_run_id, p.updated_at = ctx.run_id, iso(ctx.now)

    ledger.append_trades(strategy.id, trades)
    ledger.append_decision(
        strategy.id,
        {
            "timestamp": iso(ctx.now),
            "run_id": ctx.run_id,
            "strategy_id": strategy.id,
            "mode": ctx.mode,
            "status": status,
            "error": error,
            "source": decision.source if decision else None,
            "hold": decision.hold if decision else None,
            "market_view": decision.market_view if decision else None,
            "risk_level": decision.risk_level if decision else None,
            "notes_for_next_run": decision.notes_for_next_run if decision else None,
            "proposed_targets": decision.targets if decision else None,
            "reasons": decision.reasons if decision else None,
            "convictions": decision.convictions if decision else None,
            "warnings": decision.warnings if decision else [],
            "weights_before": {s: round(w, 6) for s, w in weights.items()},
            "final_targets": {s: round(w, 6) for s, w in final.items()},
            "guardrail_adjustments": adjustments,
            "execution_notes": notes,
            "stale_symbols": sorted(s for s, v in stale.items() if v),
            "trades": len(trades),
            "equity_before": round(equity_before, 2),
            "equity_after": round(equity_after, 2),
            "drawdown_pct": round(drawdown * 100, 2),
            "drawdown_halted": halted,
            "meta": meta,
        },
    )
    positions_value = equity_after - p.cash
    ledger.append_equity(
        strategy.id,
        {
            "timestamp": iso(ctx.now),
            "run_id": ctx.run_id,
            "cash": round(p.cash, 4),
            "positions_value": round(positions_value, 4),
            "equity": round(equity_after, 4),
        },
    )
    ledger.save_portfolio(p)
    return {
        "status": status,
        "error": error,
        "trades": len(trades),
        "equity": round(equity_after, 2),
        "drawdown_halted": halted,
        "source": decision.source if decision else None,
    }


def run_market(ctx: RunContext, provider, *, reset: bool = False, claude_client=None) -> dict:
    config, ledger = ctx.config, ctx.ledger
    market_cfg = config.market(ctx.market)
    record = {
        "run_id": ctx.run_id,
        "market": ctx.market,
        "started_at": iso(ctx.now),
        "mode": ctx.mode,
        "offline": ctx.offline,
        "status": "ok",
        "errors": [],
        "strategies": {},
        "stale_symbols": [],
    }

    if reset:
        archived = ledger.reset(ctx.now)
        record["reset"] = True
        record["archived_to"] = archived.relative_to(ctx.paths.root).as_posix() if archived else None
        log.info("reset %s: archived previous state to %s", ctx.market, archived)

    if not market_cfg.enabled:
        record["status"] = "skipped_disabled"
        record["errors"].append(f"{ctx.market} market is disabled in config.yaml")
        return record

    if provider is None:
        try:
            provider = build_provider(ctx.market, config, ctx.paths, ctx.offline, ctx.now, ctx.env)
        except MarketDisabled as e:
            record["status"] = "skipped_disabled"
            record["errors"].append(str(e))
            log.warning("%s", e)
            return record

    clock = getattr(provider, "clock", None)
    if clock is not None:
        try:
            c = clock()
        except Exception as e:
            record["status"] = "failed_data"
            record["errors"].append(f"market clock failed: {type(e).__name__}: {e}")
            return record
        record["market_open"] = bool(c.is_open)
        if not c.is_open:
            record["status"] = "skipped_market_closed"
            record["next_open"] = iso(c.next_open) if getattr(c, "next_open", None) else None
            log.info("market closed; next open %s", record["next_open"])
            return record

    validate = getattr(provider, "validate_products", None)
    if validate is not None and not ctx.offline:
        unknown = validate(market_cfg.symbols)
        if unknown:
            record["status"] = "failed_data"
            record["errors"].append(f"unknown or offline products in config.yaml: {', '.join(unknown)}")
            return record

    try:
        md = provider.fetch(market_cfg.symbols)
    except Exception as e:
        md = MarketData(market=ctx.market, fetched_at=ctx.now, errors={"*": f"{type(e).__name__}: {e}"})
    for symbol, err in md.errors.items():
        record["errors"].append(f"{symbol}: {err}")
    if not md.symbols:
        record["status"] = "failed_data"
        log.error("no market data: %s", md.errors)
        return record

    features, stale = {}, {}
    for s in market_cfg.symbols:
        sd = md.symbols.get(s)
        if sd is None:
            stale[s] = True
            continue
        features[s] = compute_features(sd, ctx.market)
        stale[s] = sd.age_minutes(ctx.now) > market_cfg.max_data_age_minutes
    record["stale_symbols"] = sorted(s for s, v in stale.items() if v)
    record["data_sources"] = sorted({sd.source for sd in md.symbols.values()})
    if record["stale_symbols"]:
        log.warning("stale symbols: %s", ", ".join(record["stale_symbols"]))

    experiment = ledger.start_experiment(ctx.now, ctx.mode, ctx.run_id)
    statuses = []
    for scfg in config.enabled_strategies():
        strategy = build_strategy(
            scfg,
            config=config,
            market=ctx.market,
            dry_run=ctx.dry_run,
            cost_tracker=ctx.costs,
            claude_client=claude_client,
        )
        result = run_strategy(ctx, strategy, md, features, stale, experiment)
        record["strategies"][scfg.id] = result
        statuses.append(result["status"])
        if result["error"]:
            record["errors"].append(f"{scfg.id}: {result['error']}")
    record["status"] = worst(statuses)

    if getattr(market_cfg, "mirror_claude_to_alpaca_paper", False):
        record["errors"].append("mirror_claude_to_alpaca_paper is not implemented yet (optional phase 5); ignored")
    return record


def write_step_summary(record: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"## {record['market']} run {record['run_id']}",
        "",
        f"Status: **{record['status']}**, mode: {record['mode']}",
        "",
    ]
    if record["strategies"]:
        lines += ["| Strategy | Equity | Trades | Status |", "|---|---:|---:|---|"]
        for sid, r in record["strategies"].items():
            lines.append(f"| {sid} | ${r['equity']:,.2f} | {r['trades']} | {r['status']} |")
        lines.append("")
    for err in record["errors"]:
        lines.append(f"- {err}")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="python -m bot.main", description="Run one paper-trading cycle.")
    parser.add_argument("--market", choices=["crypto", "stocks"], required=True)
    parser.add_argument("--dry-run", action="store_true", help="use mock Claude decisions even if a key is set")
    parser.add_argument("--offline", action="store_true", help="read recorded fixtures; no network calls")
    parser.add_argument("--reset", action="store_true", help="archive this market's state and start fresh")
    return parser.parse_args(argv)


def run(argv=None, *, paths: Paths | None = None, now: datetime | None = None, provider=None, claude_client=None, env: Mapping[str, str] | None = None, config: Config | None = None) -> dict:
    args = parse_args(argv)
    paths = paths or Paths()
    env = os.environ if env is None else env
    now = (now or utcnow()).replace(microsecond=0)
    config = config or load_config(paths.root / "config.yaml")
    ctx = RunContext(
        config=config,
        paths=paths,
        market=args.market,
        run_id=make_run_id(args.market, now),
        now=now,
        dry_run=resolve_dry_run(config, args, env),
        offline=args.offline,
        ledger=Ledger(paths, args.market),
        costs=CostTracker(paths.costs_log, config.budget),
        env=env,
    )
    try:
        record = run_market(ctx, provider, reset=args.reset, claude_client=claude_client)
    except Exception as e:
        log.error("run failed\n%s", traceback.format_exc())
        record = {
            "run_id": ctx.run_id,
            "market": ctx.market,
            "started_at": iso(now),
            "mode": ctx.mode,
            "offline": ctx.offline,
            "status": "failed",
            "errors": [f"{type(e).__name__}: {e}"],
            "strategies": {},
            "stale_symbols": [],
        }
    record["finished_at"] = iso(utcnow())
    append_jsonl(paths.runs_log, record)
    return record


def main(argv=None) -> int:
    from dotenv import load_dotenv

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    load_dotenv()
    try:
        record = run(argv)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    write_step_summary(record)
    log.info("run %s finished with status %s", record["run_id"], record["status"])
    for sid, r in record["strategies"].items():
        log.info("  %-16s equity $%10.2f  trades %d  %s", sid, r["equity"], r["trades"], r["status"])
    return 1 if record["status"] in ("failed", "failed_data") else 0


if __name__ == "__main__":
    raise SystemExit(main())
