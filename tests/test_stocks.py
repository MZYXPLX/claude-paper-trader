"""US stocks with fake Alpaca clients: missing keys, market closed, market open, stale data."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alpaca.data.enums import DataFeed

from bot.data import MarketDisabled
from bot.data.stocks import StockDataSource
from bot.ledger import Ledger, read_jsonl
from bot.main import run
from bot.report import write_reports

# Wednesday 2026-10-28, 15:00 UTC = 11:00 in New York, market open
OPEN_NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)


class FakeTradingClient:
    def __init__(self, is_open=True, fail=False):
        self.is_open, self.fail = is_open, fail

    def get_clock(self):
        if self.fail:
            raise RuntimeError("clock unavailable")
        return SimpleNamespace(
            timestamp=OPEN_NOW,
            is_open=self.is_open,
            next_open=datetime(2026, 10, 29, 13, 30, tzinfo=timezone.utc),
            next_close=datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc),
        )


class FakeDataClient:
    """Rising prices for every symbol: 130 completed days plus today's partial bar."""

    def __init__(self, symbols, trade_age=timedelta(minutes=1), stale=()):
        self.symbols, self.trade_age, self.stale = symbols, trade_age, set(stale)
        self.requests = []

    def get_stock_bars(self, req):
        self.requests.append(req)
        data = {}
        for k, s in enumerate(self.symbols):
            bars = []
            for i in range(131):
                day = (OPEN_NOW - timedelta(days=130 - i)).replace(hour=4, minute=0)  # midnight New York
                close = 100 + k + i * 0.5
                bars.append(SimpleNamespace(timestamp=day, open=close - 0.2, high=close + 1, low=close - 1, close=close, volume=1000.0 + i))
            data[s] = bars
        return SimpleNamespace(data=data)

    def get_stock_latest_trade(self, req):
        self.requests.append(req)
        out = {}
        for k, s in enumerate(self.symbols):
            age = timedelta(hours=2) if s in self.stale else self.trade_age
            out[s] = SimpleNamespace(price=100 + k + 131 * 0.5, timestamp=OPEN_NOW - age)
        return out


def source(config, **kw):
    symbols = config.markets.stocks.symbols
    trading = FakeTradingClient(**{k: kw.pop(k) for k in ("is_open", "fail") if k in kw})
    return StockDataSource(FakeDataClient(symbols, **kw), trading, now_fn=lambda: OPEN_NOW)


def stocks_run(paths, config, provider=None, env=None, now=OPEN_NOW):
    return run(["--market", "stocks"], paths=paths, now=now, config=config, env=env or {}, provider=provider)


def test_missing_keys_disable_the_market(paths, config):
    with pytest.raises(MarketDisabled, match="ALPACA_API_KEY"):
        StockDataSource.from_env(env={})
    record = stocks_run(paths, config, env={})
    assert record["status"] == "skipped_disabled"
    assert "stocks market is disabled" in record["errors"][0]
    assert Ledger(paths, "stocks").strategy_ids() == []


def test_trading_client_is_paper_only(monkeypatch):
    seen = {}

    class FakeTC:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr("alpaca.trading.client.TradingClient", FakeTC)
    monkeypatch.setattr("alpaca.data.historical.stock.StockHistoricalDataClient", lambda **kw: None)
    StockDataSource.from_env(env={"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    assert seen["paper"] is True and "url_override" not in seen


def test_market_closed_is_skipped_cleanly(paths, config):
    record = stocks_run(paths, config, provider=source(config, is_open=False))
    assert record["status"] == "skipped_market_closed"
    assert record["next_open"] == "2026-10-29T13:30:00Z"
    assert Ledger(paths, "stocks").strategy_ids() == []  # no trades, no snapshots
    out = write_reports(paths, config, OPEN_NOW)
    assert out["summary"]["markets"]["stocks"]["last_run"]["status"] == "skipped_market_closed"


def test_clock_failure_is_failed_data(paths, config):
    record = stocks_run(paths, config, provider=source(config, fail=True))
    assert record["status"] == "failed_data" and "clock" in record["errors"][0]


def test_market_open_trades_and_marks_to_market(paths, config):
    provider = source(config)
    record = stocks_run(paths, config, provider=provider)
    assert record["status"] == "ok" and record["stale_symbols"] == []
    bars_req, trade_req = provider.data_client.requests
    assert bars_req.feed == DataFeed.IEX and trade_req.feed == DataFeed.IEX
    ledger = Ledger(paths, "stocks")
    bh = ledger.read_trades("buy_hold")
    assert len(bh) == 1 and bh[0]["symbol"] == "SPY"
    assert bh[0]["fill_price"] == pytest.approx(165.5 * 1.0005)   # 5 bps slippage, no fee
    assert bh[0]["fee"] == 0
    # every symbol is rising, so the trend rule holds all 12, each at 0.95 / 12
    rt = ledger.read_decisions("rule_trend")[-1]
    assert all(w == pytest.approx(0.95 / 12) for w in rt["proposed_targets"].values())
    # second run an hour later: prices unchanged, buy_hold does not trade again
    later = OPEN_NOW + timedelta(hours=1)
    provider2 = source(config)
    provider2.now_fn = lambda: later
    stocks_run(paths, config, provider=provider2, now=later)
    assert len(ledger.read_trades("buy_hold")) == 1
    assert len(ledger.read_equity("buy_hold")) == 2


def test_todays_partial_bar_is_excluded(config):
    md = source(config).fetch(["SPY"])
    sd = md.symbols["SPY"]
    assert len(sd.daily) == 120
    assert sd.daily["time"].iloc[-1].date() < OPEN_NOW.date()
    assert sd.last_price == 165.5 and sd.source == "alpaca-iex"


def test_stale_stock_is_not_traded(paths, config):
    record = stocks_run(paths, config, provider=source(config, stale=["SPY"]))
    assert record["stale_symbols"] == ["SPY"]
    assert Ledger(paths, "stocks").read_trades("buy_hold") == []  # its only target is stale
    assert read_jsonl(paths.runs_log)[-1]["status"] == "ok"
