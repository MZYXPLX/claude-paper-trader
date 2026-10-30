"""Deterministic trend-following baseline.

A symbol is eligible when its last completed daily close is above SMA50 and SMA20 is
above SMA50. Eligible symbols get equal shares of the investable weight
(1 - min_cash_weight), each capped at max_position_weight. The rest stays in cash.
"""
from __future__ import annotations

from .base import Snapshot, Strategy, TargetDecision


def is_uptrend(features: dict | None) -> tuple[bool, str]:
    if not features:
        return False, "no data"
    close, sma20, sma50 = features.get("last_close"), features.get("sma20"), features.get("sma50")
    if close is None or sma20 is None or sma50 is None:
        return False, "not enough history for SMA50"
    if close > sma50 and sma20 > sma50:
        return True, f"close {close:.6g} > SMA50 {sma50:.6g} and SMA20 {sma20:.6g} > SMA50"
    return False, f"no uptrend: close {close:.6g}, SMA20 {sma20:.6g}, SMA50 {sma50:.6g}"


def trend_targets(universe: list[str], features: dict[str, dict], max_invested: float, max_position: float):
    signals = {s: is_uptrend(features.get(s)) for s in universe}
    eligible = [s for s in universe if signals[s][0]]
    weight = min(max_invested / len(eligible), max_position) if eligible else 0.0
    targets = {s: (weight if s in eligible else 0.0) for s in universe}
    reasons = {s: signals[s][1] for s in universe}
    return targets, reasons


class RuleTrendStrategy(Strategy):
    def decide(self, snapshot: Snapshot) -> TargetDecision:
        targets, reasons = trend_targets(
            snapshot.universe, snapshot.features, snapshot.max_invested_weight, snapshot.max_position_weight
        )
        held = sum(1 for w in targets.values() if w > 0)
        return TargetDecision(
            source="rule_trend",
            targets=targets,
            reasons=reasons,
            market_view=f"{held} of {len(snapshot.universe)} symbols in an uptrend.",
        )
