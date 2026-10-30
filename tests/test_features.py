import math

import pytest

from bot.data.features import atr_wilder, compute_features, realized_vol, rsi_wilder, sma, volume_ratio

from .conftest import make_symbol


def test_sma_hand_computed():
    closes = list(range(1, 61))  # 1..60
    assert sma(closes, 20) == pytest.approx(50.5)  # mean of 41..60
    assert sma(closes, 50) == pytest.approx(35.5)  # mean of 11..60
    assert sma(closes[:10], 20) is None


def test_rsi_first_value_is_simple_average():
    closes = [44, 45, 44, 46, 47, 46, 48, 49, 48, 50, 51, 50, 52, 53, 52]
    # gains sum to 13 and losses to 5 over 14 changes: RS = 13/5 = 2.6
    assert rsi_wilder(closes) == pytest.approx(100 - 100 / 3.6)


def test_rsi_wilder_smoothing_step():
    closes = [44, 45, 44, 46, 47, 46, 48, 49, 48, 50, 51, 50, 52, 53, 52, 50]
    avg_gain = (13 / 14 * 13 + 0) / 14
    avg_loss = (5 / 14 * 13 + 2) / 14
    assert rsi_wilder(closes) == pytest.approx(100 - 100 / (1 + avg_gain / avg_loss))
    assert rsi_wilder(closes) == pytest.approx(64.5038, abs=1e-4)


def test_rsi_edges():
    assert rsi_wilder(list(range(1, 20))) == 100.0
    assert rsi_wilder([5.0] * 20) == 50.0
    assert rsi_wilder([1, 2, 3]) is None


def test_atr_constant_range():
    closes, highs, lows = [10.0] * 15, [11.0] * 15, [9.0] * 15
    assert atr_wilder(highs, lows, closes) == pytest.approx(2.0)


def test_atr_gap_uses_previous_close_and_smoothing():
    closes = [10.0] * 15 + [14.0]
    highs = [11.0] * 15 + [15.0]
    lows = [9.0] * 15 + [13.0]
    # 14 true ranges of 2, then max(15-13, |15-10|, |13-10|) = 5
    assert atr_wilder(highs, lows, closes) == pytest.approx((2 * 13 + 5) / 14)


def test_realized_vol_alternating_moves():
    closes = [100.0]
    for i in range(30):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    expected = math.log(1.01) * math.sqrt(30 / 29) * math.sqrt(365) * 100
    assert realized_vol(closes, 30, 365) == pytest.approx(expected)
    assert realized_vol(closes[:20], 30, 365) is None


def test_volume_ratio():
    volumes = [100.0] * 19 + [300.0]
    assert volume_ratio(volumes) == pytest.approx(300 / 110)


def test_compute_features_crypto():
    closes = [float(x) for x in range(1, 61)]
    sd = make_symbol("BTC-USD", closes, last_price=66.0, hourly_closes=[60, 61, 62, 63, 64, 66])
    f = compute_features(sd, "crypto")
    assert f["last_price"] == 66.0
    assert f["ret_1d_pct"] == pytest.approx((66 / 60 - 1) * 100)
    assert f["ret_7d_pct"] == pytest.approx((66 / 54 - 1) * 100)
    assert f["ret_30d_pct"] == pytest.approx((66 / 31 - 1) * 100)
    assert f["ret_4h_pct"] == pytest.approx((66 / 61 - 1) * 100)  # current hour vs 4 hours earlier
    assert f["sma20"] == pytest.approx(50.5)
    assert f["sma50"] == pytest.approx(35.5)
    assert f["last_close"] == 60.0
    assert f["price_vs_sma50_pct"] == pytest.approx((66 / 35.5 - 1) * 100)
    assert f["closes_14d"] == closes[-14:]
    assert f["rsi14"] == 100.0


def test_compute_features_stocks_use_trading_day_periods():
    closes = [float(x) for x in range(1, 61)]
    f = compute_features(make_symbol("SPY", closes, last_price=66.0), "stocks")
    assert "ret_4h_pct" not in f
    assert f["ret_7d_pct"] == pytest.approx((66 / 56 - 1) * 100)   # 5 trading days back
    assert f["ret_30d_pct"] == pytest.approx((66 / 40 - 1) * 100)  # 21 trading days back


def test_short_history_gives_none():
    f = compute_features(make_symbol("NEW-USD", [1.0, 2.0, 3.0]), "crypto")
    assert f["sma20"] is None and f["sma50"] is None and f["rsi14"] is None and f["ret_30d_pct"] is None
