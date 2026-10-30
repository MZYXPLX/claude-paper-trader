"""Buy the benchmark on the first run, then never trade again."""
from __future__ import annotations

from .base import Snapshot, Strategy, TargetDecision


class BuyHoldStrategy(Strategy):
    benchmark_exempt = True

    def decide(self, snapshot: Snapshot) -> TargetDecision:
        if snapshot.has_traded:
            return TargetDecision(source="buy_hold", hold=True, market_view="Holding the benchmark bought on day one.")
        targets = {s: 0.0 for s in snapshot.universe}
        targets[snapshot.benchmark] = snapshot.max_invested_weight
        return TargetDecision(
            source="buy_hold",
            targets=targets,
            reasons={snapshot.benchmark: "Initial purchase of the benchmark"},
            market_view="First run: buy the benchmark.",
        )
