"""Market data containers shared by the crypto and stock data sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

CANDLE_COLUMNS = ["time", "open", "high", "low", "close", "volume"]


class DataError(Exception):
    """Raised when market data cannot be fetched or parsed."""


class MarketDisabled(Exception):
    """Raised when a market cannot run at all, for example because its API keys are missing."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def candles_frame(rows: list[list]) -> pd.DataFrame:
    """Build an ascending candle frame from [unix_seconds, open, high, low, close, volume] rows."""
    if not rows:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    df = pd.DataFrame(rows, columns=CANDLE_COLUMNS)
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="s", utc=True)
    for col in CANDLE_COLUMNS[1:]:
        df[col] = df[col].astype(float)
    return df.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def frame_rows(df: pd.DataFrame | None) -> list[list]:
    """Inverse of candles_frame, used to write fixtures."""
    if df is None or df.empty:
        return []
    return [
        [int(r.time.timestamp()), r.open, r.high, r.low, r.close, r.volume]
        for r in df.itertuples(index=False)
    ]


@dataclass
class SymbolData:
    symbol: str
    daily: pd.DataFrame          # completed daily bars only, ascending
    last_price: float
    price_time: datetime         # when last_price was observed (UTC)
    source: str
    hourly: pd.DataFrame | None = None  # crypto only; includes the current partial hour

    def age_minutes(self, now: datetime) -> float:
        return max(0.0, (now - self.price_time).total_seconds() / 60)


@dataclass
class MarketData:
    market: str
    fetched_at: datetime
    symbols: dict[str, SymbolData] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
