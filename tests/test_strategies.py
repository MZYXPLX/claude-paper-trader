from datetime import datetime, timezone

import pytest

from bot.config import StrategyConfig
from bot.strategies.base import InvalidDecision, Snapshot, parse_model_decision
from bot.strategies.buy_hold import BuyHoldStrategy
from bot.strategies.mock_claude import MockClaudeStrategy
from bot.strategies.rule_trend import RuleTrendStrategy, trend_targets

UNIVERSE = ["A", "B", "C", "D"]
UP = {"last_close": 110, "sma20": 105, "sma50": 100}


def snap(features=None, has_traded=False, universe=UNIVERSE, run_id="r1"):
    return Snapshot(
        run_id=run_id,
        market="crypto",
        now=datetime(2026, 10, 30, tzinfo=timezone.utc),
        universe=universe,
        benchmark=universe[0],
        features=features or {},
        stale={s: False for s in universe},
        weights={s: 0.0 for s in universe},
        portfolio={},
        risk={"max_invested_weight": 0.95, "max_position_weight": 0.25},
        has_traded=has_traded,
    )


# --- rule_trend ----------------------------------------------------------------


def test_trend_eligibility_rules():
    features = {
        "A": UP,
        "B": {"last_close": 95, "sma20": 105, "sma50": 100},   # close below SMA50
        "C": {"last_close": 110, "sma20": 99, "sma50": 100},   # SMA20 below SMA50
        "D": {"last_close": 110, "sma20": 105, "sma50": None},  # not enough history
    }
    targets, reasons = trend_targets(UNIVERSE, features, 0.95, 0.25)
    assert targets == {"A": 0.25, "B": 0.0, "C": 0.0, "D": 0.0}
    assert "not enough history" in reasons["D"]


def test_trend_equal_weight_uses_investable_share():
    universe = [f"S{i}" for i in range(6)]
    targets, _ = trend_targets(universe, {s: UP for s in universe}, 0.95, 0.25)
    assert all(w == pytest.approx(0.95 / 6) for w in targets.values())


def test_trend_cap_applies():
    targets, _ = trend_targets(UNIVERSE, {"A": UP, "B": UP, "C": UP}, 0.95, 0.25)
    assert targets == {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.0}


def test_trend_all_cash_when_nothing_qualifies():
    d = RuleTrendStrategy(StrategyConfig(id="rule_trend", type="rule_trend")).decide(snap({}))
    assert set(d.targets.values()) == {0.0}
    assert d.market_view == "0 of 4 symbols in an uptrend."


def test_trend_equality_is_not_an_uptrend():
    targets, _ = trend_targets(["A"], {"A": {"last_close": 100, "sma20": 100, "sma50": 100}}, 0.95, 0.25)
    assert targets == {"A": 0.0}


# --- buy_hold --------------------------------------------------------------------


def test_buy_hold_first_run_then_hold():
    s = BuyHoldStrategy(StrategyConfig(id="buy_hold", type="buy_hold"))
    first = s.decide(snap())
    assert first.targets == {"A": 0.95, "B": 0.0, "C": 0.0, "D": 0.0}
    assert s.benchmark_exempt
    assert s.decide(snap(has_traded=True)).hold


# --- model decision validation ------------------------------------------------------


def valid_raw(universe=UNIVERSE):
    return {
        "market_view": "Calm market.",
        "risk_level": "medium",
        "targets": [{"symbol": s, "target_weight": 0.1, "conviction": 3, "reason": "ok"} for s in universe],
        "notes_for_next_run": "Watch A.",
    }


def test_valid_decision_parses():
    d = parse_model_decision(valid_raw(), UNIVERSE, "claude")
    assert d.targets == {s: 0.1 for s in UNIVERSE}
    assert d.convictions["A"] == 3 and d.warnings == []


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["targets"][0].update(target_weight="0.1"), "target_weight"),
        (lambda r: r["targets"][0].update(target_weight=1.5), "target_weight"),
        (lambda r: r["targets"][0].update(conviction=0), "conviction"),
        (lambda r: r["targets"][0].update(symbol="ZZZ"), "unknown symbol"),
        (lambda r: r["targets"].append(dict(r["targets"][0])), "more than once"),
        (lambda r: r.update(risk_level="extreme"), "risk_level"),
        (lambda r: r.pop("notes_for_next_run"), "notes_for_next_run"),
        (lambda r: r.update(extra="x"), "extra"),
    ],
)
def test_invalid_decisions(mutate, message):
    raw = valid_raw()
    mutate(raw)
    with pytest.raises(InvalidDecision, match=message):
        parse_model_decision(raw, UNIVERSE, "claude")


def test_missing_symbol_keeps_weight_with_warning():
    raw = valid_raw()
    raw["targets"] = raw["targets"][:3]
    d = parse_model_decision(raw, UNIVERSE, "claude")
    assert "D" not in d.targets
    assert d.warnings == ["D missing from targets; keeping its current weight"]


def test_long_text_is_trimmed():
    raw = valid_raw()
    raw["market_view"] = "x" * 1000
    raw["targets"][0]["reason"] = "y" * 500
    d = parse_model_decision(raw, UNIVERSE, "claude")
    assert len(d.market_view) == 400 and len(d.reasons["A"]) == 200


# --- mock claude ----------------------------------------------------------------------


def test_mock_claude_is_valid_and_reproducible():
    s = MockClaudeStrategy(StrategyConfig(id="claude_sonnet", type="claude", model="claude-sonnet-5-5"))
    a, b = s.decide(snap(run_id="x")), s.decide(snap(run_id="x"))
    assert a.targets == b.targets
    assert set(a.targets) == set(UNIVERSE)
    assert all(0 <= w <= 0.95 / 4 * 1.3 + 1e-9 for w in a.targets.values())
    assert a.source == "mock_claude" and a.meta["dry_run"] is True
    assert s.decide(snap(run_id="y")).targets != a.targets
