import math
from datetime import datetime, timedelta, timezone

import pytest

from bot.metrics import (
    NEEDS_DATA,
    annualized_volatility,
    daily_points,
    daily_returns,
    max_drawdown,
    sharpe_ratio,
    strategy_metrics,
    win_rate,
)

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)


def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130, 104], start=100) == pytest.approx(0.25)
    assert max_drawdown([90, 95], start=100) == pytest.approx(0.10)  # start counts as the first peak
    assert max_drawdown([], start=100) == 0.0


def test_daily_points_keep_last_snapshot_per_utc_day():
    rows = [
        ("2026-10-01T00:00:00Z", 100),
        ("2026-10-01T20:00:00Z", 101),
        ("2026-10-02T04:00:00Z", 103),
        ("2026-10-01T23:59:00Z", 102),
    ]
    points = daily_points(rows)
    assert [e for _, e in points] == [102, 103]


def test_daily_returns():
    assert daily_returns([("d1", 100), ("d2", 110), ("d3", 99)]) == pytest.approx([0.1, -0.1])


def test_volatility_and_sharpe_hand_computed():
    returns = [0.01, -0.01, 0.02, 0.0]
    std = math.sqrt(5e-4 / 3)
    assert annualized_volatility(returns, 365) == pytest.approx(std * math.sqrt(365))
    assert sharpe_ratio(returns, 252) == pytest.approx(0.005 / std * math.sqrt(252))
    assert sharpe_ratio([0.01, 0.01], 252) is None  # zero variance


def test_win_rate():
    trades = [
        {"side": "buy", "realized_pnl": None},
        {"side": "sell", "realized_pnl": 10},
        {"side": "sell", "realized_pnl": -5},
        {"side": "sell", "realized_pnl": 0},
        {"side": "sell", "realized_pnl": 3},
    ]
    assert win_rate(trades) == (2, 4)


def rows_for(days):
    start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    return [((start + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), 10_000 * (1.001 ** i)) for i in range(days)]


def test_risk_stats_need_30_daily_points():
    m = strategy_metrics(equity_rows=rows_for(29), trades=[], starting_capital=10_000, periods_per_year=365, started_at="2026-10-01T12:00:00Z", now=NOW)
    assert m["volatility_pct"] is None and m["sharpe"] is None and m["risk_stats_note"] == NEEDS_DATA
    m = strategy_metrics(equity_rows=rows_for(30), trades=[], starting_capital=10_000, periods_per_year=365, started_at="2026-10-01T12:00:00Z", now=NOW)
    assert m["risk_stats_note"] is None and m["volatility_pct"] is not None


def test_strategy_metrics_totals():
    rows = [("2026-11-01T00:00:00Z", 10_200.0), ("2026-11-02T00:00:00Z", 10_500.0)]
    trades = [
        {"side": "buy", "notional": 3000.0, "fee": 9.0, "slippage_cost": 3.0, "realized_pnl": None},
        {"side": "sell", "notional": 2000.0, "fee": 6.0, "slippage_cost": 2.0, "realized_pnl": 40.0},
    ]
    m = strategy_metrics(
        equity_rows=rows,
        trades=trades,
        starting_capital=10_000,
        periods_per_year=252,
        started_at="2026-11-01T00:00:00Z",
        now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc),
        api_cost=2.0,
        benchmark_return_pct=3.0,
    )
    assert m["equity"] == 10_500
    assert m["total_return_pct"] == pytest.approx(5.0)
    assert m["excess_return_vs_buy_hold_pct"] == pytest.approx(2.0)
    assert m["return_net_of_api_cost_pct"] == pytest.approx(4.98)
    assert m["equity_net_of_api_cost"] == pytest.approx(10_498)
    assert m["turnover"] == pytest.approx(0.5)
    assert m["fees_paid"] == pytest.approx(15) and m["slippage_paid"] == pytest.approx(5)
    assert m["trades"] == 2 and m["round_trips_closed"] == 1 and m["win_rate_pct"] == 100
    assert m["days_running"] == pytest.approx(2.5)
    assert m["max_drawdown_pct"] == 0
