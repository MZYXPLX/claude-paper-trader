"""Write docs/data/*.json for the dashboard from the state files.

    python -m bot.report

Every number in these files is computed from state/. Nothing is invented: when there
is no data the value is null or the list is empty, and the page shows an empty state.
"""
from __future__ import annotations

import logging
from datetime import datetime

from .config import MARKETS, Config, Paths, load_config
from .costs import CostTracker
from .data import utcnow
from .ledger import Ledger, Portfolio, iso, read_json, read_jsonl, write_json
from .metrics import _parse_ts, strategy_metrics

log = logging.getLogger("bot.report")

RECENT_TRADES = 50
RUNS_SHOWN = 30
MAX_CURVE_POINTS = 3000

STRATEGY_LABELS = {"claude": "Claude", "rule_trend": "Trend rule", "buy_hold": "Buy and hold"}


def _ms(ts: str) -> int:
    return int(_parse_ts(ts).timestamp() * 1000)


def _round(x, nd=2):
    return None if x is None else round(x, nd)


def _round_metrics(m: dict) -> dict:
    out = {}
    for k, v in m.items():
        if isinstance(v, float):
            out[k] = round(v, 4 if k in ("sharpe", "turnover", "api_cost") else 2)
        else:
            out[k] = v
    return out


def _label(sid: str, cfg) -> str:
    if cfg is None:
        return sid
    if cfg.type == "claude":
        return f"Claude ({cfg.model})"
    return STRATEGY_LABELS.get(cfg.type, sid)


def _thin(points: list, limit: int) -> list:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    thinned = [points[int(i * step)] for i in range(limit - 1)]
    return thinned + [points[-1]]


def _latest_decision(ledger: Ledger, sid: str) -> dict | None:
    decisions = ledger.read_decisions(sid, last=1)
    if not decisions:
        return None
    d = decisions[-1]
    proposed = d.get("proposed_targets") or {}
    final = d.get("final_targets") or {}
    reasons = d.get("reasons") or {}
    convictions = d.get("convictions") or {}
    symbols = list(final) or list(proposed)
    meta = d.get("meta") or {}
    return {
        "timestamp": d["timestamp"],
        "status": d["status"],
        "error": d.get("error"),
        "source": d.get("source"),
        "market_view": d.get("market_view"),
        "risk_level": d.get("risk_level"),
        "notes_for_next_run": d.get("notes_for_next_run"),
        "warnings": d.get("warnings") or [],
        "guardrail_adjustments": d.get("guardrail_adjustments") or [],
        "model": meta.get("model"),
        "cost_usd": _round(meta.get("cost_usd"), 4),
        "targets": [
            {
                "symbol": s,
                "proposed": _round(proposed.get(s), 4),
                "final": _round(final.get(s), 4),
                "conviction": convictions.get(s),
                "reason": reasons.get(s),
            }
            for s in symbols
        ],
    }


def _holdings(p: Portfolio) -> list[dict]:
    prices = p.last_prices
    equity = p.equity(prices)
    rows = []
    for s in p.held_symbols():
        price = prices.get(s)
        qty = p.quantity(s)
        value = qty * price if price is not None else None
        basis = p.cost_basis(s)
        rows.append(
            {
                "symbol": s,
                "quantity": qty,
                "price": price,
                "value": _round(value),
                "weight_pct": _round(value / equity * 100) if value is not None and equity > 0 else None,
                "avg_cost": p.avg_cost(s),
                "unrealized_pct": _round((value / basis - 1) * 100) if value is not None and basis > 0 else None,
            }
        )
    rows.sort(key=lambda r: -(r["value"] or 0))
    return rows


def build_market_report(paths: Paths, config: Config, market: str, now: datetime, costs: CostTracker) -> dict:
    ledger = Ledger(paths, market)
    market_cfg = config.market(market)
    exp = ledger.experiment()
    runs = [r for r in read_jsonl(paths.runs_log) if r.get("market") == market]
    last_run = runs[-1] if runs else None
    periods = 365 if market == "crypto" else 252
    cfg_by_id = {s.id: s for s in config.strategies}
    present = ledger.strategy_ids()
    ordered = [s.id for s in config.strategies if s.id in present] + [s for s in present if s not in cfg_by_id]

    base = {
        "market": market,
        "generated_at": iso(now),
        "enabled": market_cfg.enabled,
        "benchmark": market_cfg.benchmark,
        "symbols": market_cfg.symbols,
        "started_at": exp["started_at"] if exp else None,
        "last_run": _run_view(last_run) if last_run else None,
        "strategies": [],
        "recent_trades": [],
        "banners": [],
    }
    if not exp or not ordered:
        return base

    loaded = {}
    for sid in ordered:
        p = Portfolio.from_dict(read_json(ledger.strategy_dir(sid) / "portfolio.json"))
        equity_rows = [(r["timestamp"], r["equity"]) for r in ledger.read_equity(sid)]
        loaded[sid] = (p, equity_rows, ledger.read_trades(sid))

    def metrics_for(sid, bench_return=None):
        p, rows, trades = loaded[sid]
        cfg = cfg_by_id.get(sid)
        api_cost = costs.spend_for(market, sid, since=exp["started_at"]) if cfg and cfg.type == "claude" else None
        return strategy_metrics(
            equity_rows=rows,
            trades=trades,
            starting_capital=p.starting_capital,
            periods_per_year=periods,
            started_at=exp["started_at"],
            now=now,
            api_cost=api_cost,
            benchmark_return_pct=bench_return,
        )

    bench_return = metrics_for("buy_hold")["total_return_pct"] if "buy_hold" in loaded else None
    all_trades = []
    for sid in ordered:
        p, rows, trades = loaded[sid]
        cfg = cfg_by_id.get(sid)
        is_claude = bool(cfg and cfg.type == "claude")
        m = metrics_for(sid, None if sid == "buy_hold" else bench_return)
        curve = [[_ms(p.created_at), round(p.starting_capital, 2)]] + [[_ms(ts), round(e, 2)] for ts, e in rows]
        base["strategies"].append(
            {
                "id": sid,
                "label": _label(sid, cfg),
                "type": cfg.type if cfg else None,
                "model": cfg.model if cfg else None,
                "enabled": bool(cfg and cfg.enabled),
                "is_claude": is_claude,
                "mode": p.mode if is_claude else None,
                "created_at": p.created_at,
                "cash": round(p.cash, 2),
                "drawdown_halted": p.drawdown_halted,
                "metrics": _round_metrics(m),
                "equity_curve": _thin(curve, MAX_CURVE_POINTS),
                "holdings": _holdings(p),
                "latest_decision": _latest_decision(ledger, sid) if is_claude else None,
            }
        )
        label = _label(sid, cfg)
        for t in trades:
            all_trades.append({**t, "strategy_label": label})
        if p.drawdown_halted:
            base["banners"].append(
                {"level": "error", "text": f"{label}: drawdown halt active. Equity is {config.risk.drawdown_halt * 100:.0f}% or more below its peak, so only position reductions are allowed."}
            )
        if is_claude and p.mode == "mixed":
            base["banners"].append(
                {"level": "error", "text": f"{label} mixes dry-run and live decisions. Run the {market} workflow once with reset set to true."}
            )

    all_trades.sort(key=lambda t: t["timestamp"], reverse=True)
    base["recent_trades"] = [
        {
            "timestamp": t["timestamp"],
            "strategy": t["strategy_id"],
            "strategy_label": t["strategy_label"],
            "symbol": t["symbol"],
            "side": t["side"],
            "quantity": t["quantity"],
            "fill_price": t["fill_price"],
            "notional": round(t["notional"], 2),
            "fee": round(t["fee"], 2),
            "realized_pnl": _round(t.get("realized_pnl")),
            "reason": t.get("reason", ""),
        }
        for t in all_trades[:RECENT_TRADES]
    ]

    if last_run and last_run.get("stale_symbols"):
        base["banners"].append(
            {"level": "warning", "text": f"Stale data in the last run for {', '.join(last_run['stale_symbols'])}. Those symbols were not traded and are valued at their last known price."}
        )
    return base


def _run_view(r: dict) -> dict:
    trades = sum(s.get("trades", 0) for s in (r.get("strategies") or {}).values())
    return {
        "run_id": r["run_id"],
        "market": r["market"],
        "started_at": r["started_at"],
        "finished_at": r.get("finished_at"),
        "status": r["status"],
        "mode": r.get("mode"),
        "offline": r.get("offline", False),
        "trades": trades,
        "errors": [e[:300] for e in (r.get("errors") or [])][:5],
        "next_open": r.get("next_open"),
        "reset": r.get("reset", False),
    }


def build_summary(paths: Paths, config: Config, now: datetime, costs: CostTracker, market_reports: dict) -> dict:
    runs = read_jsonl(paths.runs_log)
    markets = {}
    for m in MARKETS:
        rep = market_reports[m]
        last = rep["last_run"]
        markets[m] = {
            "enabled": rep["enabled"],
            "started_at": rep["started_at"],
            "last_run": last,
        }
    modes = {mk["last_run"]["mode"] for mk in markets.values() if mk["last_run"]}
    if modes == {"live"}:
        mode_label = "Live (Claude API)"
    elif modes == {"dry_run"}:
        mode_label = "Dry run"
    elif modes:
        mode_label = "Dry run and live (see each market)"
    else:
        mode_label = "No runs yet"
    starts = [mk["started_at"] for mk in markets.values() if mk["started_at"]]

    cap = config.budget.monthly_usd_cap
    mtd = costs.month_to_date(now)
    banners = []
    for m, mk in markets.items():
        last = mk["last_run"]
        if not last:
            continue
        if last["status"] == "failed_data":
            banners.append({"level": "error", "text": f"The last {m} run could not get market data, so no trades were made and the previous state was kept."})
        if last["status"] == "failed":
            banners.append({"level": "error", "text": f"The last {m} run stopped with an error. The previous state was kept. See the runs log below."})
        if last["status"] == "budget_exceeded":
            banners.append({"level": "error", "text": f"Monthly API budget reached in the last {m} run. Claude portfolios held their positions."})
    if mtd >= cap > 0 and not any("budget" in b["text"] for b in banners):
        banners.append({"level": "error", "text": f"Monthly API budget of ${cap:.2f} is used up. Claude portfolios will hold until next month."})
    for m in MARKETS:
        banners += [{**b, "market": m} for b in market_reports[m]["banners"]]

    return {
        "generated_at": iso(now),
        "experiment_started_at": min(starts) if starts else None,
        "starting_capital_usd": config.experiment.starting_capital_usd,
        "mode_label": mode_label,
        "markets": markets,
        "costs": {
            "month": now.strftime("%Y-%m"),
            "month_to_date": round(mtd, 4),
            "cap": cap,
            "total": round(costs.total(), 4),
        },
        "runs": [_run_view(r) for r in reversed(runs[-RUNS_SHOWN:])],
        "banners": banners,
    }


def write_reports(paths: Paths | None = None, config: Config | None = None, now: datetime | None = None) -> dict:
    paths = paths or Paths()
    config = config or load_config(paths.root / "config.yaml")
    now = (now or utcnow()).replace(microsecond=0)
    costs = CostTracker(paths.costs_log, config.budget)
    reports = {m: build_market_report(paths, config, m, now, costs) for m in MARKETS}
    summary = build_summary(paths, config, now, costs, reports)
    paths.docs_data.mkdir(parents=True, exist_ok=True)
    for m, rep in reports.items():
        write_json(paths.docs_data / f"{m}.json", rep)
    write_json(paths.docs_data / "summary.json", summary)
    return {"summary": summary, **reports}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    write_reports()
    log.info("wrote dashboard data to docs/data/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
