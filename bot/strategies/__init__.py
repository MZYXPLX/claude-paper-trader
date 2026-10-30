"""Strategy factory."""
from __future__ import annotations

from ..config import Config, StrategyConfig
from .base import Strategy
from .buy_hold import BuyHoldStrategy
from .mock_claude import MockClaudeStrategy
from .rule_trend import RuleTrendStrategy


def build_strategy(
    cfg: StrategyConfig,
    *,
    config: Config,
    market: str,
    dry_run: bool,
    cost_tracker=None,
    claude_client=None,
) -> Strategy:
    if cfg.type == "buy_hold":
        return BuyHoldStrategy(cfg)
    if cfg.type == "rule_trend":
        return RuleTrendStrategy(cfg)
    if dry_run:
        return MockClaudeStrategy(cfg)
    from .claude_strategy import ClaudeStrategy

    return ClaudeStrategy(cfg, config=config, market=market, cost_tracker=cost_tracker, client=claude_client)
