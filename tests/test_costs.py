from datetime import datetime, timezone

import pytest

from bot.costs import CostTracker
from bot.ledger import append_jsonl
from bot.strategies.base import BudgetExceeded

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def tracker(tmp_path, config):
    return CostTracker(tmp_path / "costs.jsonl", config.budget)


def test_cost_from_usage(tracker):
    usage = {"input_tokens": 1000, "output_tokens": 500}
    assert tracker.cost_of("claude-sonnet-5-5", usage) == pytest.approx((1000 * 2 + 500 * 10) / 1e6)
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}
    assert tracker.cost_of("claude-sonnet-5-5", usage) == pytest.approx((2000 + 5000 + 100 * 2 * 1.25 + 1000 * 2 * 0.1) / 1e6)
    assert tracker.cost_of("claude-haiku-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(0.6)


def test_unknown_model_price(tracker):
    with pytest.raises(KeyError, match="no price"):
        tracker.cost_of("claude-unknown", {})


def test_record_and_month_to_date(tracker):
    tracker.record(now=NOW, run_id="r1", market="crypto", strategy_id="claude_sonnet", model="claude-sonnet-5-5", usage={"input_tokens": 1_000_000, "output_tokens": 0})
    append_jsonl(tracker.path, {"timestamp": "2026-09-30T23:00:00Z", "market": "crypto", "strategy_id": "claude_sonnet", "cost_usd": 5.0})
    assert tracker.month_to_date(NOW) == pytest.approx(2.0)
    assert tracker.total() == pytest.approx(7.0)
    assert tracker.spend_for("crypto", "claude_sonnet") == pytest.approx(7.0)
    assert tracker.spend_for("crypto", "claude_sonnet", since="2026-10-01T00:00:00Z") == pytest.approx(2.0)
    assert tracker.spend_for("stocks", "claude_sonnet") == 0


def test_budget_cap(tracker):
    append_jsonl(tracker.path, {"timestamp": "2026-10-02T00:00:00Z", "market": "crypto", "strategy_id": "x", "cost_usd": 9.95})
    tracker.check(NOW, 0.04)  # 9.99 is under the $10 cap
    with pytest.raises(BudgetExceeded, match="exceed"):
        tracker.check(NOW, 0.06)


def test_estimate_is_worst_case(tracker):
    est = tracker.estimate("claude-sonnet-5-5", prompt_chars=30_000, max_tokens=4000)
    assert est == pytest.approx((10_000 * 2 + 4000 * 10) / 1e6)
