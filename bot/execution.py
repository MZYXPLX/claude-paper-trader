"""Simulated fills with fees and slippage, and the rebalancing math.

- Fill price = latest price plus slippage on buys, minus slippage on sells.
- Fee = fee_bps of the notional, paid from cash.
- Fractional quantities are allowed. Sells run before buys so cash is freed first.
- Cash never goes negative: a buy that does not fit is shrunk to the cash available.
- Lots are tracked FIFO. A lot's unit cost includes its buy fee and slippage, so
  realized P&L on a sell is proceeds minus sell fee minus the FIFO cost of the units sold.
"""
from __future__ import annotations

from datetime import datetime

from .ledger import Lot, Portfolio, iso

MIN_NOTIONAL = 0.01
EPS = 1e-12


def _consume_fifo(lots: list[Lot], qty: float) -> tuple[float, list[Lot]]:
    """Remove qty units from the oldest lots. Returns (cost of units removed, remaining lots)."""
    cost = 0.0
    remaining = []
    left = qty
    for lot in lots:
        if left <= EPS:
            remaining.append(lot)
            continue
        take = min(lot.qty, left)
        cost += take * lot.unit_cost
        left -= take
        if lot.qty - take > EPS:
            remaining.append(Lot(qty=lot.qty - take, unit_cost=lot.unit_cost, opened_at=lot.opened_at))
    return cost, remaining


def rebalance(
    portfolio: Portfolio,
    targets: dict[str, float],
    prices: dict[str, float],
    *,
    fee_bps: float,
    slippage_bps: float,
    now: datetime,
    run_id: str,
    reasons: dict[str, str] | None = None,
    order: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Trade the portfolio toward target weights. Mutates the portfolio.

    Returns (trades, notes). Only symbols whose target differs from the current weight
    should be passed; others are left alone.
    """
    reasons = reasons or {}
    fee_rate = fee_bps / 10_000
    slip_rate = slippage_bps / 10_000
    order = order or sorted(targets)
    equity = portfolio.equity(prices)
    ts = iso(now)
    trades: list[dict] = []
    notes: list[str] = []

    deltas = {}
    for s in order:
        if s not in targets:
            continue
        price = prices.get(s)
        if price is None or price <= 0:
            notes.append(f"{s}: no price, skipped")
            continue
        current_value = portfolio.quantity(s) * price
        deltas[s] = targets[s] * equity - current_value

    def record(symbol, side, qty, price, fill, fee, realized=None):
        notional = qty * fill
        trade = {
            "timestamp": ts,
            "run_id": run_id,
            "strategy_id": portfolio.strategy_id,
            "market": portfolio.market,
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "price": price,
            "fill_price": fill,
            "notional": notional,
            "fee": fee,
            "slippage_cost": qty * abs(fill - price),
            "realized_pnl": realized,
            "reason": reasons.get(symbol, ""),
        }
        trades.append(trade)
        portfolio.fees_paid += fee
        portfolio.slippage_paid += trade["slippage_cost"]
        portfolio.traded_notional += notional
        portfolio.trade_count += 1

    # Sells first.
    for s, delta in deltas.items():
        if delta >= -MIN_NOTIONAL:
            continue
        price = prices[s]
        held = portfolio.quantity(s)
        qty = held if targets[s] <= 0 else min(-delta / price, held)
        if qty <= EPS:
            continue
        fill = price * (1 - slip_rate)
        proceeds = qty * fill
        fee = proceeds * fee_rate
        cost, remaining = _consume_fifo(portfolio.lots.get(s, []), qty)
        portfolio.lots[s] = remaining
        if not remaining:
            portfolio.lots.pop(s, None)
        realized = proceeds - fee - cost
        portfolio.cash += proceeds - fee
        portfolio.realized_pnl += realized
        record(s, "sell", qty, price, fill, fee, realized)

    # Then buys, in universe order. A buy that does not fit is shrunk to the cash left.
    for s, delta in deltas.items():
        if delta <= MIN_NOTIONAL:
            continue
        price = prices[s]
        fill = price * (1 + slip_rate)
        notional = delta
        if notional * (1 + fee_rate) > portfolio.cash:
            shrunk = max(0.0, portfolio.cash / (1 + fee_rate))
            notes.append(f"{s}: buy of ${notional:.2f} shrunk to ${shrunk:.2f} to keep cash non-negative")
            notional = shrunk
        if notional < MIN_NOTIONAL:
            continue
        qty = notional / fill
        fee = notional * fee_rate
        portfolio.cash -= notional + fee
        if portfolio.cash < 0:  # float noise only
            portfolio.cash = 0.0
        portfolio.lots.setdefault(s, []).append(Lot(qty=qty, unit_cost=(notional + fee) / qty, opened_at=ts))
        portfolio.has_traded = True
        record(s, "buy", qty, price, fill, fee)

    return trades, notes
