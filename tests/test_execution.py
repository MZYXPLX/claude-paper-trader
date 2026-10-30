from datetime import datetime, timezone

import pytest

from bot.execution import rebalance
from bot.ledger import Lot, Portfolio

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def portfolio(cash=10_000.0):
    p = Portfolio.new("s", "crypto", 10_000.0, "2026-10-30T00:00:00Z")
    p.cash = cash
    return p


def go(p, targets, prices, fee_bps=30, slippage_bps=10):
    return rebalance(p, targets, prices, fee_bps=fee_bps, slippage_bps=slippage_bps, now=NOW, run_id="r", reasons={"X": "why"})


def test_buy_fill_fee_slippage_and_cost_basis():
    p = portfolio()
    trades, _ = go(p, {"X": 0.5}, {"X": 100.0})
    t = trades[0]
    assert t["side"] == "buy"
    assert t["fill_price"] == pytest.approx(100.1)            # +10 bps
    assert t["quantity"] == pytest.approx(5000 / 100.1)
    assert t["notional"] == pytest.approx(5000.0)
    assert t["fee"] == pytest.approx(15.0)                    # 30 bps of $5,000
    assert t["slippage_cost"] == pytest.approx(5000 / 100.1 * 0.1)
    assert t["reason"] == "why"
    assert p.cash == pytest.approx(10_000 - 5_015)
    assert p.avg_cost("X") == pytest.approx(5015 / (5000 / 100.1))


def test_full_sell_realized_pnl():
    p = portfolio()
    go(p, {"X": 0.5}, {"X": 100.0})
    qty = 5000 / 100.1
    trades, _ = go(p, {"X": 0.0}, {"X": 110.0})
    t = trades[0]
    proceeds = qty * 110 * (1 - 0.001)
    fee = proceeds * 0.003
    assert t["side"] == "sell"
    assert t["quantity"] == pytest.approx(qty)
    assert t["fill_price"] == pytest.approx(109.89)
    assert t["realized_pnl"] == pytest.approx(proceeds - fee - 5015)
    assert p.quantity("X") == 0 and "X" not in p.lots
    assert p.cash == pytest.approx(10_000 - 5_015 + proceeds - fee)


def test_fifo_lots():
    p = portfolio(cash=0.0)
    p.lots["X"] = [Lot(qty=10, unit_cost=100, opened_at="t1"), Lot(qty=10, unit_cost=120, opened_at="t2")]
    # equity = 20 * 130 = 2600; target 25% = 650 = 5 units, so 15 units are sold
    trades, _ = go(p, {"X": 0.25}, {"X": 130.0}, fee_bps=0, slippage_bps=0)
    assert trades[0]["quantity"] == pytest.approx(15)
    assert trades[0]["realized_pnl"] == pytest.approx(15 * 130 - (10 * 100 + 5 * 120))
    assert [(lot.qty, lot.unit_cost) for lot in p.lots["X"]] == [(pytest.approx(5), 120)]


def test_sells_before_buys_and_cash_never_negative():
    p = portfolio(cash=0.0)
    p.lots["A"] = [Lot(qty=10, unit_cost=100, opened_at="t")]
    trades, notes = go(p, {"A": 0.0, "B": 1.0}, {"A": 100.0, "B": 50.0}, fee_bps=1000, slippage_bps=0)
    assert [t["side"] for t in trades] == ["sell", "buy"]
    assert trades[0]["fee"] == pytest.approx(100)
    assert trades[1]["notional"] == pytest.approx(900 / 1.1)   # shrunk to fit cash after fee
    assert p.cash >= 0 and p.cash == pytest.approx(0, abs=1e-9)
    assert any("shrunk" in n for n in notes)


def test_partial_trim():
    p = portfolio()
    go(p, {"X": 0.5}, {"X": 100.0}, fee_bps=0, slippage_bps=0)
    trades, _ = go(p, {"X": 0.25}, {"X": 100.0}, fee_bps=0, slippage_bps=0)
    assert trades[0]["side"] == "sell"
    assert trades[0]["quantity"] == pytest.approx(25)
    assert p.weights({"X": 100.0}, ["X"])["X"] == pytest.approx(0.25)


def test_missing_price_is_skipped():
    p = portfolio()
    trades, notes = go(p, {"X": 0.5}, {})
    assert trades == [] and "no price" in notes[0]
