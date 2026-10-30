"""Stand-in for Claude in dry-run mode.

Returns small random tilts around equal weight, seeded by run and strategy so a run is
reproducible. The output goes through the same validation as a real Claude response.
Dry-run decisions never count as Claude's record.
"""
from __future__ import annotations

import hashlib
import random

from .base import Snapshot, Strategy, TargetDecision, parse_model_decision

MOCK_VIEW = "Dry run: mock decision with small random tilts around equal weight. Not a Claude decision."


class MockClaudeStrategy(Strategy):
    is_claude = True

    def decide(self, snapshot: Snapshot) -> TargetDecision:
        seed = int(hashlib.sha256(f"{snapshot.run_id}:{self.id}".encode()).hexdigest()[:12], 16)
        rng = random.Random(seed)
        base = snapshot.max_invested_weight / len(snapshot.universe)
        raw = {
            "market_view": MOCK_VIEW,
            "risk_level": rng.choice(["low", "medium", "high"]),
            "targets": [
                {
                    "symbol": s,
                    "target_weight": round(max(0.0, base * (1 + rng.uniform(-0.3, 0.3))), 4),
                    "conviction": rng.randint(1, 5),
                    "reason": "Mock tilt (dry run)",
                }
                for s in snapshot.universe
            ],
            "notes_for_next_run": "Dry run. No real analysis was done.",
        }
        decision = parse_model_decision(raw, snapshot.universe, source="mock_claude")
        decision.meta = {"model": "mock", "dry_run": True, "raw_response": raw}
        return decision
