"""Claude strategy with a fake Anthropic client: valid, invalid-then-valid, invalid twice,
budget cap, API errors, and an end-to-end live-mode run. No real API is called."""
import json
from datetime import timedelta
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from bot.config import StrategyConfig
from bot.costs import CostTracker
from bot.data.fixtures import load_fixtures
from bot.ledger import Ledger, Portfolio, append_jsonl, read_jsonl
from bot.main import run
from bot.report import write_reports
from bot.strategies.base import BudgetExceeded, DecisionError, Snapshot
from bot.strategies.claude_strategy import ClaudeStrategy, render_system_prompt

from .conftest import NOW

UNIVERSE = ["BTC-USD", "ETH-USD"]
RULES = {
    "max_position_weight": 0.25,
    "max_invested_weight": 0.95,
    "round_trip_cost_bps": 80.0,
}


def decision_json(universe=UNIVERSE, weight=0.2):
    return json.dumps(
        {
            "market_view": "Downtrend across the board; staying light.",
            "risk_level": "low",
            "targets": [{"symbol": s, "target_weight": weight, "conviction": 2, "reason": "below SMA50"} for s in universe],
            "notes_for_next_run": "Re-enter if price reclaims SMA50.",
        }
    )


def response(text, stop_reason="end_turn", input_tokens=3000, output_tokens=800):
    resp = SimpleNamespace(
        id="msg_test",
        model="claude-sonnet-5-5",
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )
    resp.to_dict = lambda: {"id": "msg_test", "stop_reason": stop_reason, "content": [{"type": "text", "text": text}]}
    return resp


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def snapshot(universe=UNIVERSE):
    return Snapshot(
        run_id="crypto-test",
        market="crypto",
        now=NOW,
        universe=universe,
        benchmark=universe[0],
        features={},
        stale={s: False for s in universe},
        weights={s: 0.0 for s in universe},
        portfolio={"cash": 10000, "equity": 10000, "positions": []},
        risk=RULES,
        has_traded=False,
    )


@pytest.fixture
def tracker(tmp_path, config):
    return CostTracker(tmp_path / "costs.jsonl", config.budget)


def strategy(config, tracker, client):
    cfg = StrategyConfig(id="claude_sonnet", type="claude", model="claude-sonnet-5-5")
    return ClaudeStrategy(cfg, config=config, market="crypto", cost_tracker=tracker, client=client)


def test_system_prompt_is_filled_in():
    text = render_system_prompt("crypto", RULES)
    assert "simulated crypto portfolio" in text
    assert "between 0 and 0.25" in text and "must not exceed 0.95" in text and "about 80 basis points" in text
    assert "{" not in text


def test_valid_response(config, tracker):
    client = FakeClient(response(decision_json()))
    d = strategy(config, tracker, client).decide(snapshot())
    assert d.source == "claude" and d.targets == {"BTC-USD": 0.2, "ETH-USD": 0.2}
    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["max_tokens"] == config.claude.max_tokens
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["schema"]["properties"]["targets"]["items"]["properties"]["symbol"]["enum"] == UNIVERSE
    assert "temperature" not in call and "tool_choice" not in call
    assert json.loads(call["messages"][0]["content"])["universe"] == UNIVERSE
    # usage, cost and the raw response are logged
    assert d.meta["usage"]["input_tokens"] == 3000 and d.meta["attempts"] == 1
    assert d.meta["cost_usd"] == pytest.approx((3000 * 2 + 800 * 10) / 1e6)
    assert d.meta["responses"][0]["raw_response"]["id"] == "msg_test"
    assert "latency_ms" in d.meta
    costs = read_jsonl(tracker.path)
    assert len(costs) == 1 and costs[0]["strategy_id"] == "claude_sonnet" and costs[0]["model"] == "claude-sonnet-5-5"


def test_invalid_then_valid(config, tracker):
    bad = json.dumps({"market_view": "x", "risk_level": "low", "targets": [{"symbol": "BTC-USD", "target_weight": 2, "conviction": 2, "reason": "r"}], "notes_for_next_run": ""})
    client = FakeClient(response(bad), response(decision_json()))
    d = strategy(config, tracker, client).decide(snapshot())
    assert d.targets["BTC-USD"] == 0.2
    assert len(client.calls) == 2
    retry_text = client.calls[1]["messages"][0]["content"]
    assert "Your previous response was invalid" in retry_text and "target_weight" in retry_text
    assert d.meta["attempts"] == 2 and "validation_error" in d.meta["responses"][0]
    assert len(read_jsonl(tracker.path)) == 2  # both calls are paid for and logged


def test_invalid_twice(config, tracker):
    client = FakeClient(response("not json"), response(decision_json(["BTC-USD", "DOGE-USD"])))
    with pytest.raises(DecisionError, match="after 2 attempts") as info:
        strategy(config, tracker, client).decide(snapshot())
    assert info.value.meta["attempts"] == 2
    assert "not valid JSON" in info.value.meta["responses"][0]["validation_error"]
    assert "unknown symbol" in info.value.meta["responses"][1]["validation_error"]


def test_refusal_and_truncation_are_invalid(config, tracker):
    client = FakeClient(response("", stop_reason="refusal"), response('{"market', stop_reason="max_tokens"))
    with pytest.raises(DecisionError) as info:
        strategy(config, tracker, client).decide(snapshot())
    errors = [r["validation_error"] for r in info.value.meta["responses"]]
    assert "declined" in errors[0] and "max_tokens" in errors[1]


def test_budget_cap_skips_the_call(config, tracker):
    append_jsonl(tracker.path, {"timestamp": NOW.strftime("%Y-%m-%dT00:00:00Z"), "market": "crypto", "strategy_id": "x", "cost_usd": 9.99})
    client = FakeClient(response(decision_json()))
    with pytest.raises(BudgetExceeded):
        strategy(config, tracker, client).decide(snapshot())
    assert client.calls == []


def test_api_error_becomes_decision_error(config, tracker):
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client = FakeClient(err)
    with pytest.raises(DecisionError, match="APIConnectionError"):
        strategy(config, tracker, client).decide(snapshot())


def test_missing_symbol_keeps_current_weight(config, tracker):
    client = FakeClient(response(decision_json(["BTC-USD"])))
    d = strategy(config, tracker, client).decide(snapshot())
    assert "ETH-USD" not in d.targets and d.warnings


def test_snapshot_stays_compact(paths, config):
    """The prompt for the largest universe, with full history, stays well under 8,000 tokens."""
    md = load_fixtures("crypto", config.markets.crypto.symbols, paths.fixtures, NOW)
    from bot.data.features import compute_features

    features = {s: compute_features(sd, "crypto") for s, sd in md.symbols.items()}
    universe = [f"S{i:02d}" for i in range(12)]
    feats = {u: features["BTC-USD"] for u in universe}
    history = [{"time_utc": "2026-10-30T00:00:00Z", "status": "ok", "market_view": "v" * 400, "risk_level": "low", "target_changes": {u: {"from": 0.1, "to": 0.2} for u in universe}, "notes_for_next_run": "n" * 300}] * 5
    trades = [{"time_utc": "2026-10-30T00:00:00Z", "symbol": "S01", "side": "buy", "quantity": 1.23456, "fill_price": 123.456, "notional": 152.4, "reason": "r" * 200}] * 10
    snap = Snapshot(
        run_id="r", market="stocks", now=NOW, universe=universe, benchmark="S00", features=feats,
        stale={u: False for u in universe}, weights={u: 0.0 for u in universe},
        portfolio={"cash": 1, "positions": [{"symbol": u, "quantity": 1, "avg_cost": 1, "market_value": 1, "weight": 0.08, "unrealized_pnl_pct": 1} for u in universe]},
        risk=RULES, has_traded=True, history=history, recent_trades=trades,
    )
    chars = len(json.dumps(snap.payload(), separators=(",", ":")))
    assert chars < 8000 * 3, chars  # about 3 characters per token for numeric JSON


class StaticProvider:
    def __init__(self, md):
        self.md = md

    def fetch(self, symbols):
        return self.md


def test_live_mode_end_to_end(paths, config):
    universe = config.markets.crypto.symbols
    md = load_fixtures("crypto", universe, paths.fixtures, NOW)
    client = FakeClient(response(decision_json(universe, weight=0.1)))
    record = run(
        ["--market", "crypto"], paths=paths, now=NOW, config=config,
        env={"ANTHROPIC_API_KEY": "test-key"}, provider=StaticProvider(md), claude_client=client,
    )
    assert record["mode"] == "live" and record["status"] == "ok"
    assert record["strategies"]["claude_sonnet"]["source"] == "claude"
    ledger = Ledger(paths, "crypto")
    d = ledger.read_decisions("claude_sonnet")[-1]
    assert d["meta"]["usage"]["output_tokens"] == 800 and d["meta"]["cost_usd"] > 0
    assert len(ledger.read_trades("claude_sonnet")) == 6
    p = Portfolio.from_dict(json.loads((ledger.strategy_dir("claude_sonnet") / "portfolio.json").read_text()))
    assert p.mode == "live"
    out = write_reports(paths, config, NOW + timedelta(minutes=1))
    claude = next(s for s in out["crypto"]["strategies"] if s["id"] == "claude_sonnet")
    assert claude["metrics"]["api_cost"] == pytest.approx(0.014)
    assert claude["metrics"]["equity_net_of_api_cost"] == pytest.approx(claude["metrics"]["equity"] - 0.014, abs=0.01)
    assert out["summary"]["mode_label"] == "Live (Claude API)"
    assert out["summary"]["costs"]["total"] == pytest.approx(0.014)


def test_budget_exceeded_run_holds_positions(paths, config):
    universe = config.markets.crypto.symbols
    md = load_fixtures("crypto", universe, paths.fixtures, NOW)
    append_jsonl(paths.costs_log, {"timestamp": NOW.strftime("%Y-%m-%dT00:00:00Z"), "market": "crypto", "strategy_id": "claude_sonnet", "cost_usd": 10.0})
    client = FakeClient()
    record = run(["--market", "crypto"], paths=paths, now=NOW, config=config, env={"ANTHROPIC_API_KEY": "k"}, provider=StaticProvider(md), claude_client=client)
    assert record["status"] == "budget_exceeded"
    assert record["strategies"]["claude_sonnet"]["trades"] == 0
    assert record["strategies"]["buy_hold"]["trades"] == 1  # the other strategies still run
    out = write_reports(paths, config, NOW)
    assert any("budget" in b["text"].lower() for b in out["summary"]["banners"])


def test_decision_error_run_records_status(paths, config):
    universe = config.markets.crypto.symbols
    md = load_fixtures("crypto", universe, paths.fixtures, NOW)
    client = FakeClient(response("nope"), response("still nope"))
    record = run(["--market", "crypto"], paths=paths, now=NOW, config=config, env={"ANTHROPIC_API_KEY": "k"}, provider=StaticProvider(md), claude_client=client)
    assert record["status"] == "decision_error"
    assert record["strategies"]["claude_sonnet"]["trades"] == 0
    d = Ledger(paths, "crypto").read_decisions("claude_sonnet")[-1]
    assert d["status"] == "decision_error" and d["meta"]["attempts"] == 2
