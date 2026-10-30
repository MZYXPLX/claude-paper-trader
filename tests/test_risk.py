import pytest

from bot.config import RiskConfig
from bot.risk import apply_guardrails

U = ["A", "B", "C", "D"]
RISK = RiskConfig(
    max_position_weight=0.25,
    min_cash_weight=0.05,
    max_turnover_per_run=0.50,
    min_trade_usd=25,
    min_weight_change=0.02,
    drawdown_halt=0.30,
)
ZERO = {s: 0.0 for s in U}


def run(proposed, current=None, **kw):
    kw.setdefault("universe", U)
    kw.setdefault("stale", set())
    kw.setdefault("equity", 10_000)
    kw.setdefault("risk", RISK)
    return apply_guardrails(proposed, current or ZERO, **kw)


def test_rule1_drops_unknown_symbols_and_keeps_stale_weights():
    r = run({"A": 0.1, "ZZZ": 0.2, "B": 0.2}, {**ZERO, "B": 0.1}, stale={"B"})
    assert "ZZZ" not in r.targets
    assert r.targets["B"] == 0.1
    assert any("ZZZ" in a for a in r.adjustments)
    assert any("B: data is stale" in a for a in r.adjustments)


def test_rule2_clamps_to_position_cap_and_zero():
    r = run({"A": 0.4, "B": -0.1})
    assert r.targets["A"] == 0.25 and r.targets["B"] == 0.0


def test_rule3_scales_to_cash_floor():
    no_turnover_cap = RISK.model_copy(update={"max_turnover_per_run": 2})
    r = run({s: 0.25 for s in U}, risk=no_turnover_cap)
    assert sum(r.targets.values()) == pytest.approx(0.95)
    assert all(w == pytest.approx(0.2375) for w in r.targets.values())


def test_rule3_leaves_stale_weights_fixed():
    current = {**ZERO, "D": 0.30}
    r = run({"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.0}, current, stale={"D"}, risk=RISK.model_copy(update={"max_turnover_per_run": 2}))
    assert r.targets["D"] == 0.30
    assert r.targets["A"] == pytest.approx(0.25 * 0.65 / 0.75)
    assert sum(r.targets.values()) == pytest.approx(0.95)


def test_rule4_drawdown_halt_allows_only_reductions():
    current = {**ZERO, "A": 0.20, "B": 0.20}
    r = run({"A": 0.25, "B": 0.10, "C": 0.10}, current, drawdown_halted=True)
    assert r.targets["A"] == 0.20      # increase blocked
    assert r.targets["B"] == 0.10      # reduction allowed
    assert r.targets["C"] == 0.0       # new position blocked


def test_rule5_small_changes_and_small_trades_are_ignored():
    current = {**ZERO, "A": 0.10, "B": 0.10, "C": 0.01}
    r = run({"A": 0.11, "B": 0.125, "C": 0.0}, current, equity=1_000)
    assert r.targets["A"] == 0.10      # +0.01 < min_weight_change
    assert r.targets["B"] == 0.125     # +0.025 of $1,000 = $25, exactly min_trade_usd, so it trades
    assert r.targets["C"] == 0.0       # full exits are always allowed
    r2 = run({"B": 0.124}, current, equity=1_000)
    assert r2.targets["B"] == 0.10     # $24 < min_trade_usd


def test_rule5_exact_threshold_trades():
    r = run({"A": 0.03}, equity=1_000)
    assert r.targets["A"] == 0.03  # change 0.03 >= 0.02 and $30 >= $25


def test_rule6_turnover_cap_scales_all_trades():
    r = run({s: 0.2375 for s in U})
    assert sum(r.targets.values()) == pytest.approx(0.50)
    assert all(w == pytest.approx(0.125) for w in r.targets.values())
    assert any("turnover" in a for a in r.adjustments)


def test_rule6_counts_both_sides():
    current = {**ZERO, "A": 0.25, "B": 0.25}
    r = run({"A": 0.0, "B": 0.0, "C": 0.25, "D": 0.25}, current)  # turnover 1.0
    assert r.targets["A"] == pytest.approx(0.125)
    assert r.targets["C"] == pytest.approx(0.125)


def test_rule7_kill_switch():
    r = run({"A": 0.2}, trading_enabled=False)
    assert r.targets == ZERO
    assert any("trading_enabled" in a for a in r.adjustments)


def test_benchmark_exempt_from_position_and_turnover_caps():
    r = run({"A": 0.95}, benchmark_exempt=True)
    assert r.targets["A"] == 0.95


def test_benchmark_still_respects_cash_floor():
    r = run({"A": 1.0}, benchmark_exempt=True)
    assert r.targets["A"] == pytest.approx(0.95)
