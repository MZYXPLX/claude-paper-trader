"""End-to-end offline runs: fixtures in, state files and dashboard JSON out."""
import json
from argparse import Namespace
from datetime import timedelta

import pytest

from bot.config import ConfigError
from bot.data import MarketData
from bot.data.fixtures import load_fixtures
from bot.ledger import Ledger, Portfolio, read_jsonl, write_json
from bot.main import resolve_dry_run, run
from bot.report import write_reports

from .conftest import NOW

STRATEGIES = ["claude_sonnet", "rule_trend", "buy_hold"]


class StaticProvider:
    def __init__(self, md):
        self.md = md

    def fetch(self, symbols):
        return self.md


def offline(paths, config, now=NOW, extra=()):
    return run(["--market", "crypto", "--offline", *extra], paths=paths, now=now, config=config, env={})


def test_offline_run_writes_state(paths, config):
    record = offline(paths, config)
    assert record["status"] == "ok" and record["mode"] == "dry_run" and record["offline"]
    ledger = Ledger(paths, "crypto")
    assert ledger.strategy_ids() == sorted(STRATEGIES)
    for sid in STRATEGIES:
        assert len(ledger.read_equity(sid)) == 1
        assert len(ledger.read_decisions(sid)) == 1
    bh = ledger.read_trades("buy_hold")
    assert len(bh) == 1 and bh[0]["symbol"] == "BTC-USD" and bh[0]["notional"] == pytest.approx(9500)
    claude = Portfolio.from_dict(json.loads((ledger.strategy_dir("claude_sonnet") / "portfolio.json").read_text()))
    assert claude.mode == "dry_run" and claude.mock_decisions == 1
    assert read_jsonl(paths.runs_log)[-1]["run_id"] == record["run_id"]


def test_buy_hold_never_trades_again(paths, config):
    offline(paths, config)
    offline(paths, config, now=NOW + timedelta(hours=4))
    ledger = Ledger(paths, "crypto")
    assert len(ledger.read_trades("buy_hold")) == 1
    assert len(ledger.read_equity("buy_hold")) == 2


def test_stale_symbol_is_not_traded_but_is_valued(paths, config):
    md = load_fixtures("crypto", config.markets.crypto.symbols, paths.fixtures, NOW)
    md.symbols["ETH-USD"].price_time = NOW - timedelta(hours=3)  # older than 90 minutes
    record = run(["--market", "crypto"], paths=paths, now=NOW, config=config, env={}, provider=StaticProvider(md))
    assert record["stale_symbols"] == ["ETH-USD"]
    ledger = Ledger(paths, "crypto")
    for sid in STRATEGIES:
        assert all(t["symbol"] != "ETH-USD" for t in ledger.read_trades(sid))
    decision = ledger.read_decisions("claude_sonnet")[-1]
    assert any("ETH-USD: data is stale" in a for a in decision["guardrail_adjustments"])
    p = Portfolio.from_dict(json.loads((ledger.strategy_dir("claude_sonnet") / "portfolio.json").read_text()))
    assert p.last_prices["ETH-USD"] == md.symbols["ETH-USD"].last_price


def test_failed_data_keeps_previous_state(paths, config):
    offline(paths, config)
    ledger = Ledger(paths, "crypto")
    before = (ledger.strategy_dir("claude_sonnet") / "portfolio.json").read_text()
    empty = MarketData(market="crypto", fetched_at=NOW, errors={"BTC-USD": "timeout"})
    record = run(["--market", "crypto"], paths=paths, now=NOW + timedelta(hours=4), config=config, env={}, provider=StaticProvider(empty))
    assert record["status"] == "failed_data"
    assert (ledger.strategy_dir("claude_sonnet") / "portfolio.json").read_text() == before
    assert len(ledger.read_equity("claude_sonnet")) == 1


def test_missing_fixtures_fail_cleanly(paths, config):
    record = run(["--market", "stocks", "--offline"], paths=paths, now=NOW, config=config, env={})
    assert record["status"] == "failed_data"
    assert "Record them with" in record["errors"][0]


def test_reset_archives_and_starts_fresh(paths, config):
    offline(paths, config)
    later = NOW + timedelta(days=1)
    record = offline(paths, config, now=later, extra=["--reset"])
    assert record["reset"] and record["archived_to"].startswith("archive/")
    archived = paths.root / record["archived_to"]
    assert (archived / "claude_sonnet" / "portfolio.json").exists()
    ledger = Ledger(paths, "crypto")
    assert ledger.experiment()["started_at"] == later.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(ledger.read_equity("buy_hold")) == 1
    assert len(read_jsonl(paths.runs_log)) == 2  # the runs log is not archived


def test_kill_switch_records_equity_without_trades(paths, config):
    cfg = config.model_copy(update={"experiment": config.experiment.model_copy(update={"trading_enabled": False})})
    offline(paths, cfg)
    ledger = Ledger(paths, "crypto")
    for sid in STRATEGIES:
        assert ledger.read_trades(sid) == []
        assert ledger.read_equity(sid)[0]["equity"] == 10_000


def test_drawdown_halt_blocks_buys(paths, config):
    ledger = Ledger(paths, "crypto")
    ledger.start_experiment(NOW - timedelta(days=10), "dry_run", "r0")
    p = Portfolio.new("claude_sonnet", "crypto", 10_000, "2026-10-20T00:00:00Z")
    p.peak_equity = 20_000  # equity 10,000 is 50% below the peak
    ledger.save_portfolio(p)
    offline(paths, config)
    d = ledger.read_decisions("claude_sonnet")[-1]
    assert d["drawdown_halted"] and d["trades"] == 0
    assert ledger.read_trades("claude_sonnet") == []


def test_report_contains_only_computed_numbers(paths, config):
    offline(paths, config)
    offline(paths, config, now=NOW + timedelta(hours=4))
    out = write_reports(paths, config, NOW + timedelta(hours=5))
    text = (paths.docs_data / "crypto.json").read_text()
    assert "NaN" not in text and "Infinity" not in text
    crypto = out["crypto"]
    ids = [s["id"] for s in crypto["strategies"]]
    assert ids == STRATEGIES
    by_id = {s["id"]: s for s in crypto["strategies"]}
    assert by_id["claude_sonnet"]["mode"] == "dry_run"
    assert by_id["claude_sonnet"]["latest_decision"]["source"] == "mock_claude"
    assert by_id["buy_hold"]["metrics"]["excess_return_vs_buy_hold_pct"] is None
    assert by_id["claude_sonnet"]["equity_curve"][0][1] == 10_000
    assert len(crypto["recent_trades"]) <= 50
    summary = out["summary"]
    assert summary["mode_label"] == "Dry run"
    assert summary["costs"]["month_to_date"] == 0
    assert [r["market"] for r in summary["runs"]] == ["crypto", "crypto"]
    assert out["stocks"]["strategies"] == []


def test_dry_run_resolution(config):
    args = Namespace(offline=False, dry_run=False)
    assert resolve_dry_run(config, args, {}) is True
    assert resolve_dry_run(config, args, {"ANTHROPIC_API_KEY": "k"}) is False
    assert resolve_dry_run(config, Namespace(offline=False, dry_run=True), {"ANTHROPIC_API_KEY": "k"}) is True
    assert resolve_dry_run(config, Namespace(offline=True, dry_run=False), {"ANTHROPIC_API_KEY": "k"}) is True
    live = config.model_copy(update={"experiment": config.experiment.model_copy(update={"dry_run": False})})
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        resolve_dry_run(live, args, {})


def test_existing_portfolio_mode_is_kept(paths, config):
    ledger = Ledger(paths, "crypto")
    ledger.start_experiment(NOW, "dry_run", "r0")
    p = Portfolio.new("claude_sonnet", "crypto", 10_000, "2026-10-30T00:00:00Z")
    p.live_decisions = 3
    ledger.save_portfolio(p)
    offline(paths, config)
    p2 = Portfolio.from_dict(json.loads((ledger.strategy_dir("claude_sonnet") / "portfolio.json").read_text()))
    assert p2.mode == "mixed"
    out = write_reports(paths, config, NOW)
    assert any("mixes dry-run and live" in b["text"] for b in out["crypto"]["banners"])


def test_write_json_roundtrip(tmp_path):
    write_json(tmp_path / "a" / "b.json", {"x": 1})
    assert json.loads((tmp_path / "a" / "b.json").read_text()) == {"x": 1}
