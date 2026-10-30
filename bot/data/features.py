"""Indicators computed from candles. These numbers are what every strategy sees.

Conventions (tested against hand-computed values in tests/test_features.py):
- `closes` are completed daily closes, oldest first. The live price is separate.
- Returns compare the live price with the close N daily bars back. For crypto N is
  1/7/30 calendar days. For stocks N is 1/5/21 trading days (1 day, 1 week, 1 month).
- SMA20/SMA50 use completed daily closes only, so trend signals change once a day.
- RSI14 and ATR14 use Wilder's smoothing over the whole available history.
- Realized volatility is the sample standard deviation of the last 30 daily log
  returns, annualized (365 for crypto, 252 for stocks), in percent.
- Volume ratio is the last completed day's volume divided by the mean of the last
  20 completed days (including that day).
"""
from __future__ import annotations

import math

import numpy as np

from . import SymbolData

CRYPTO_PERIODS = {"1d": 1, "7d": 7, "30d": 30}
STOCK_PERIODS = {"1d": 1, "7d": 5, "30d": 21}


def sma(values, n: int) -> float | None:
    values = np.asarray(values, dtype=float)
    if len(values) < n or n <= 0:
        return None
    return float(values[-n:].mean())


def pct_change(current: float, reference: float | None) -> float | None:
    if reference is None or reference == 0:
        return None
    return (current / reference - 1) * 100


def rsi_wilder(closes, period: int = 14) -> float | None:
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def atr_wilder(high, low, close, period: int = 14) -> float | None:
    high, low, close = (np.asarray(x, dtype=float) for x in (high, low, close))
    if len(close) < period + 1:
        return None
    prev_close = close[:-1]
    h, l = high[1:], low[1:]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_close), np.abs(l - prev_close)))
    atr = tr[:period].mean()
    for value in tr[period:]:
        atr = (atr * (period - 1) + value) / period
    return float(atr)


def realized_vol(closes, window: int = 30, periods_per_year: int = 365) -> float | None:
    closes = np.asarray(closes, dtype=float)
    if len(closes) < window + 1:
        return None
    log_returns = np.diff(np.log(closes[-(window + 1):]))
    return float(log_returns.std(ddof=1) * math.sqrt(periods_per_year) * 100)


def volume_ratio(volumes, window: int = 20) -> float | None:
    volumes = np.asarray(volumes, dtype=float)
    if len(volumes) < window:
        return None
    mean = volumes[-window:].mean()
    if mean == 0:
        return None
    return float(volumes[-1] / mean)


def compute_features(sd: SymbolData, market: str) -> dict:
    """Return the full-precision feature dict for one symbol."""
    is_crypto = market == "crypto"
    periods = CRYPTO_PERIODS if is_crypto else STOCK_PERIODS
    annualization = 365 if is_crypto else 252
    daily = sd.daily
    closes = daily["close"].to_numpy(dtype=float)
    last = float(sd.last_price)

    out: dict = {"last_price": last}
    if is_crypto:
        ret_4h = None
        if sd.hourly is not None and len(sd.hourly) >= 5:
            ret_4h = pct_change(float(sd.hourly["close"].iloc[-1]), float(sd.hourly["close"].iloc[-5]))
        out["ret_4h_pct"] = ret_4h
    for label, n in periods.items():
        out[f"ret_{label}_pct"] = pct_change(last, float(closes[-n]) if len(closes) >= n else None)

    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    atr = atr_wilder(daily["high"], daily["low"], daily["close"], 14)
    out.update(
        {
            "last_close": float(closes[-1]) if len(closes) else None,
            "sma20": sma20,
            "sma50": sma50,
            "price_vs_sma50_pct": pct_change(last, sma50),
            "rsi14": rsi_wilder(closes, 14),
            "atr14_pct": (atr / last * 100) if atr is not None and last else None,
            "vol30d_pct": realized_vol(closes, 30, annualization),
            "volume_vs_20d": volume_ratio(daily["volume"].to_numpy(dtype=float), 20),
            "closes_14d": [float(c) for c in closes[-14:]],
        }
    )
    return out


def _sig(x: float, digits: int = 6) -> float:
    return float(f"{x:.{digits}g}")


def rounded(features: dict) -> dict:
    """A compact, rounded copy for prompts and the dashboard."""
    out = {}
    for key, value in features.items():
        if value is None:
            out[key] = None
        elif key == "closes_14d":
            out[key] = [_sig(v) for v in value]
        elif key in ("last_price", "last_close", "sma20", "sma50"):
            out[key] = _sig(value)
        elif key == "volume_vs_20d":
            out[key] = round(value, 2)
        elif key == "rsi14":
            out[key] = round(value, 1)
        else:
            out[key] = round(value, 2)
    return out
