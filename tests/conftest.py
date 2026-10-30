import shutil
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from bot.config import Paths, load_config
from bot.data import SymbolData

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may call a real API: any socket connection fails loudly."""

    def guard(*args, **kwargs):
        raise RuntimeError("network access is not allowed in tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture
def config():
    return load_config(REPO / "config.yaml")


@pytest.fixture
def paths(tmp_path):
    """A throwaway repo root with the recorded crypto fixtures copied in."""
    shutil.copytree(REPO / "tests" / "fixtures" / "crypto", tmp_path / "tests" / "fixtures" / "crypto")
    return Paths(root=tmp_path)


def make_daily(closes, highs=None, lows=None, volumes=None, end=NOW):
    n = len(closes)
    start = (end - timedelta(days=n)).replace(hour=0, minute=0, second=0, microsecond=0)
    return pd.DataFrame(
        {
            "time": [pd.Timestamp(start + timedelta(days=i)) for i in range(n)],
            "open": closes,
            "high": highs or closes,
            "low": lows or closes,
            "close": closes,
            "volume": volumes or [100.0] * n,
        }
    )


def make_symbol(symbol, closes, last_price=None, price_time=NOW, hourly_closes=None, **kw):
    hourly = None
    if hourly_closes is not None:
        hourly = pd.DataFrame(
            {
                "time": [pd.Timestamp(NOW - timedelta(hours=len(hourly_closes) - i)) for i in range(len(hourly_closes))],
                "open": hourly_closes,
                "high": hourly_closes,
                "low": hourly_closes,
                "close": hourly_closes,
                "volume": [1.0] * len(hourly_closes),
            }
        )
    return SymbolData(
        symbol=symbol,
        daily=make_daily(closes, **kw),
        last_price=last_price if last_price is not None else closes[-1],
        price_time=price_time,
        source="test",
        hourly=hourly,
    )
