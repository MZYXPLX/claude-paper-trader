"""Code-enforced guardrails, applied to every strategy after it decides and before any fill.

Order (each adjustment is logged):
1. Drop symbols outside the universe. Stale symbols keep their current weight.
2. Clamp each weight to [0, max_position_weight].
3. If the total exceeds 1 - min_cash_weight, scale the tradable weights down proportionally.
4. If the drawdown halt is active, allow only reductions.
5. Ignore changes smaller than min_weight_change and trades smaller than min_trade_usd.
   Full exits (target 0) are always allowed so small leftover positions can be closed.
6. If total turnover exceeds max_turnover_per_run, scale all trades down proportionally.
7. If trading is disabled, keep every weight where it is.

The buy_hold benchmark is exempt from the position cap (2) and the turnover cap (6).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import RiskConfig

EPS = 1e-9


@dataclass
class RiskResult:
    targets: dict[str, float]
    adjustments: list[str] = field(default_factory=list)

    def changed(self, current: dict[str, float]) -> dict[str, float]:
        return {s: w for s, w in self.targets.items() if abs(w - current.get(s, 0.0)) > EPS}


def apply_guardrails(
    proposed: dict[str, float],
    current: dict[str, float],
    *,
    universe: list[str],
    stale: set[str],
    equity: float,
    risk: RiskConfig,
    trading_enabled: bool = True,
    drawdown_halted: bool = False,
    benchmark_exempt: bool = False,
) -> RiskResult:
    adj: list[str] = []
    cur = {s: float(current.get(s, 0.0)) for s in universe}
    t: dict[str, float] = {}

    # 1. universe and stale symbols
    for s, w in proposed.items():
        if s not in universe:
            adj.append(f"dropped {s}: not in the universe")
            continue
        if w is None or not math.isfinite(float(w)):
            adj.append(f"{s}: invalid weight {w!r}, kept current weight")
            w = cur[s]
        t[s] = float(w)
    for s in universe:
        t.setdefault(s, cur[s])
    for s in sorted(stale & set(universe)):
        if abs(t[s] - cur[s]) > EPS:
            adj.append(f"{s}: data is stale, kept current weight {cur[s]:.4f} instead of {t[s]:.4f}")
            t[s] = cur[s]
    tradable = [s for s in universe if s not in stale]

    # 2. clamp
    upper = 1.0 if benchmark_exempt else risk.max_position_weight
    for s in tradable:
        clamped = min(max(t[s], 0.0), upper)
        if abs(clamped - t[s]) > EPS:
            adj.append(f"{s}: clamped weight {t[s]:.4f} to {clamped:.4f}")
            t[s] = clamped

    # 3. cash floor
    max_invested = 1.0 - risk.min_cash_weight
    total = sum(t.values())
    if total > max_invested + EPS:
        fixed = sum(t[s] for s in universe if s in stale)
        free_total = sum(t[s] for s in tradable)
        available = max(0.0, max_invested - fixed)
        factor = available / free_total if free_total > 0 else 0.0
        for s in tradable:
            t[s] *= factor
        adj.append(f"total weight {total:.4f} above {max_invested:.4f}; scaled tradable weights by {factor:.4f}")

    # 4. drawdown halt: reductions only
    if drawdown_halted:
        for s in tradable:
            if t[s] > cur[s] + EPS:
                adj.append(f"{s}: drawdown halt active, blocked increase {cur[s]:.4f} -> {t[s]:.4f}")
                t[s] = cur[s]

    # 5. minimum change and minimum trade size
    for s in tradable:
        delta = t[s] - cur[s]
        if abs(delta) <= EPS:
            continue
        full_exit = t[s] <= EPS and cur[s] > EPS
        if full_exit:
            t[s] = 0.0
            continue
        # Small tolerances so a change exactly at a threshold is not lost to float rounding.
        if abs(delta) < risk.min_weight_change - EPS:
            adj.append(f"{s}: change {delta:+.4f} below min_weight_change, ignored")
            t[s] = cur[s]
        elif abs(delta) * equity < risk.min_trade_usd - 1e-6:
            adj.append(f"{s}: trade ${abs(delta) * equity:.2f} below min_trade_usd, ignored")
            t[s] = cur[s]

    # 6. turnover cap
    turnover = sum(abs(t[s] - cur[s]) for s in tradable)
    if not benchmark_exempt and turnover > risk.max_turnover_per_run + EPS:
        factor = risk.max_turnover_per_run / turnover
        for s in tradable:
            t[s] = cur[s] + (t[s] - cur[s]) * factor
        adj.append(f"turnover {turnover:.4f} above {risk.max_turnover_per_run:.4f}; scaled trades by {factor:.4f}")
        for s in tradable:
            delta = t[s] - cur[s]
            if EPS < abs(delta) and abs(delta) * equity < risk.min_trade_usd - 1e-6:
                adj.append(f"{s}: scaled trade ${abs(delta) * equity:.2f} below min_trade_usd, ignored")
                t[s] = cur[s]

    # 7. kill switch
    if not trading_enabled:
        if any(abs(t[s] - cur[s]) > EPS for s in universe):
            adj.append("trading_enabled is false: no trades placed")
        t = dict(cur)

    return RiskResult(targets=t, adjustments=adj)
