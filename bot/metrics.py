"""Performance metrics per strategy. Each one is unit tested against hand-computed values.

- Daily returns use the last equity snapshot of each UTC day.
- Volatility and Sharpe (risk-free rate 0) are annualized with 365 for crypto and 252
  for stocks, and are only reported once there are 30 daily points.
- Max drawdown includes the starting capital as the first peak.
- A closed round trip is one sell, matched FIFO against earlier buys; it is a win when
  its realized P&L after fees is positive.
- Turnover is total traded value divided by starting capital.
"""
from __future__ import annotations

import math
from datetime import datetime

MIN_DAILY_POINTS = 30
NEEDS_DATA = "Needs 30+ days of data"


def _parse_ts(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def max_drawdown(equities: list[float], start: float | None = None) -> float:
    """Largest peak-to-trough fall as a positive fraction (0.25 = 25%)."""
    peak = start if start is not None else (equities[0] if equities else 0.0)
    worst = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, 1 - e / peak)
    return worst


def daily_points(rows: list[tuple]) -> list[tuple]:
    """[(timestamp, equity), ...] -> [(date, equity)] keeping the last snapshot of each UTC day."""
    by_day: dict = {}
    for ts, equity in sorted(rows, key=lambda r: _parse_ts(r[0])):
        by_day[_parse_ts(ts).date()] = equity
    return sorted(by_day.items())


def daily_returns(points: list[tuple]) -> list[float]:
    values = [e for _, e in points]
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]


def annualized_volatility(returns: list[float], periods_per_year: int) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def sharpe_ratio(returns: list[float], periods_per_year: int) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var == 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(periods_per_year)


def win_rate(trades: list[dict]) -> tuple[int, int]:
    """(wins, closed round trips) from sell trades."""
    closed = [t for t in trades if t.get("side") == "sell" and t.get("realized_pnl") is not None]
    wins = sum(1 for t in closed if t["realized_pnl"] > 0)
    return wins, len(closed)


def strategy_metrics(
    *,
    equity_rows: list[tuple],
    trades: list[dict],
    starting_capital: float,
    periods_per_year: int,
    started_at: str,
    now: datetime,
    api_cost: float | None = None,
    benchmark_return_pct: float | None = None,
) -> dict:
    equities = [e for _, e in sorted(equity_rows, key=lambda r: _parse_ts(r[0]))]
    equity = equities[-1] if equities else starting_capital
    total_return_pct = (equity / starting_capital - 1) * 100
    points = daily_points(equity_rows)
    returns = daily_returns(points)
    enough = len(points) >= MIN_DAILY_POINTS
    vol = annualized_volatility(returns, periods_per_year) if enough else None
    sharpe = sharpe_ratio(returns, periods_per_year) if enough else None
    wins, closed = win_rate(trades)
    notional = sum(t["notional"] for t in trades)
    out = {
        "equity": equity,
        "total_return_pct": total_return_pct,
        "excess_return_vs_buy_hold_pct": (
            total_return_pct - benchmark_return_pct if benchmark_return_pct is not None else None
        ),
        "max_drawdown_pct": max_drawdown(equities, start=starting_capital) * 100,
        "daily_points": len(points),
        "volatility_pct": vol * 100 if vol is not None else None,
        "sharpe": sharpe,
        "risk_stats_note": None if enough else NEEDS_DATA,
        "trades": len(trades),
        "turnover": notional / starting_capital if starting_capital else None,
        "fees_paid": sum(t["fee"] for t in trades),
        "slippage_paid": sum(t["slippage_cost"] for t in trades),
        "round_trips_closed": closed,
        "win_rate_pct": wins / closed * 100 if closed else None,
        "days_running": max(0.0, (now - _parse_ts(started_at)).total_seconds() / 86400),
        "api_cost": api_cost,
        "equity_net_of_api_cost": equity - api_cost if api_cost is not None else None,
        "return_net_of_api_cost_pct": (
            ((equity - api_cost) / starting_capital - 1) * 100 if api_cost is not None else None
        ),
    }
    return out
