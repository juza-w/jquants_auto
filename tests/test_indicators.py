"""指標のテスト: 手計算した値と照合する。"""
import numpy as np
import pandas as pd
import pytest

from stockalert.indicators import atr, compute_indicators, rsi, true_range, wilder_smooth


def bars_from_close(close, high=None, low=None, adj=None, volume=None):
    n = len(close)
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": close, "high": close if high is None else high,
        "low": close if low is None else low, "close": close,
        "volume": np.full(n, 1000.0) if volume is None else volume,
        "adj_close": close if adj is None else adj,
    })


def test_wilder_smooth_by_hand():
    s = pd.Series([np.nan, 1.0, 2.0, 3.0, 5.0, 2.0])
    out = wilder_smooth(s, 3)
    # 最初の値 = (1+2+3)/3 = 2、次 = (2×2+5)/3 = 3、次 = (3×2+2)/3 = 8/3
    assert out.isna().tolist() == [True, True, True, False, False, False]
    assert out.iloc[3:].tolist() == pytest.approx([2.0, 3.0, 8 / 3])


def test_rsi_by_hand():
    close = pd.Series([10.0, 11.0, 10.0, 12.0, 13.0, 12.0])
    out = rsi(close, 3)
    # 値動き: +1, -1, +2, +1, -1
    # 3日目: 平均上げ=(1+0+2)/3=1, 平均下げ=(0+1+0)/3=1/3 → RSI=100-100/(1+3)=75
    # 4日目: 上げ=(1×2+1)/3=1, 下げ=(1/3×2+0)/3=2/9 → RS=4.5 → 81.8181...
    # 5日目: 上げ=(1×2+0)/3=2/3, 下げ=(2/9×2+1)/3=13/27 → RS=18/13 → 58.0645...
    assert out.iloc[:3].isna().all()
    assert out.iloc[3:].tolist() == pytest.approx([75.0, 100 - 100 / 5.5, 100 - 100 / (1 + 18 / 13)])


def test_rsi_all_up_is_100():
    assert rsi(pd.Series(np.arange(1.0, 30.0)), 14).iloc[-1] == 100.0


def test_true_range_and_atr_by_hand():
    high = pd.Series([10.0, 12.0, 11.0, 15.0])
    low = pd.Series([9.0, 10.0, 8.0, 13.0])
    close = pd.Series([9.5, 11.0, 9.0, 14.0])
    tr = true_range(high, low, close)
    # 2日目: max(12-10, |12-9.5|, |10-9.5|) = 2.5
    # 3日目: max(11-8, |11-11|, |8-11|) = 3
    # 4日目: max(15-13, |15-9|, |13-9|) = 6  ← 窓を開けて上がった日は高値−安値より大きい
    assert np.isnan(tr.iloc[0])
    assert tr.iloc[1:].tolist() == [2.5, 3.0, 6.0]
    out = atr(high, low, close, 2)
    assert out.iloc[2:].tolist() == pytest.approx([2.75, (2.75 + 6.0) / 2])


def test_sma_returns_momentum_turnover_high():
    close = np.arange(1.0, 301.0)  # 1,2,...,300
    df = compute_indicators(bars_from_close(close, high=close + 1, low=close - 1))
    last = df.iloc[-1]
    assert last["sma20"] == pytest.approx(np.mean(close[-20:]))    # 290.5
    assert last["sma200"] == pytest.approx(np.mean(close[-200:]))  # 200.5
    assert np.isnan(df["sma200"].iloc[198]) and not np.isnan(df["sma200"].iloc[199])
    assert last["ret_1m"] == pytest.approx(300 / 279 - 1)
    assert last["ret_12m"] == pytest.approx(300 / 48 - 1)
    assert last["mom_6_1"] == pytest.approx(279 / 174 - 1)  # 21日前の値 ÷ 126日前の値 − 1
    assert last["high20"] == 301.0
    assert last["avg_volume20"] == 1000.0
    assert last["avg_turnover20"] == pytest.approx(np.mean(close[-20:]) * 1000)
    assert last["atr14"] == pytest.approx(2.0)  # 毎日 高値−安値=2、前日終値との差も2以下


def test_split_does_not_distort_indicators():
    """1:2分割があっても、調整済みで計算するので指標は分割なしの場合と一致する。"""
    adj = np.linspace(100, 160, 260)
    plain = compute_indicators(bars_from_close(adj, high=adj * 1.01, low=adj * 0.99))
    raw = adj.copy()
    raw[:200] *= 2  # 分割前は実際の値段が2倍
    vol = np.full(260, 1000.0)
    vol[:200] /= 2  # 分割前は株数が半分
    with_split = compute_indicators(
        bars_from_close(raw, high=raw * 1.01, low=raw * 0.99, adj=adj, volume=vol)
    )
    for col in ("sma200", "rsi14", "atr14", "ret_6m", "mom_6_1", "high20", "avg_volume20", "avg_turnover20"):
        assert with_split[col].iloc[-1] == pytest.approx(plain[col].iloc[-1]), col


def test_no_lookahead_and_null_days_skipped():
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, 300))
    bars = bars_from_close(close, high=close + 1, low=close - 1)
    full = compute_indicators(bars)
    part = compute_indicators(bars.iloc[:250])  # 未来の50日を消しても過去の値は変わらない
    pd.testing.assert_frame_equal(full.iloc[:250], part)

    bars.loc[100, ["open", "high", "low", "close", "volume", "adj_close"]] = None
    assert len(compute_indicators(bars)) == 299  # 取引なしの日は除外して計算が続く


def test_rsi_matches_published_example():
    """RSI(14) の計算例として広く引用されるデータ（StockCharts の解説）の公表値と一致する。"""
    prices = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89,
              46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21]
    out = rsi(pd.Series(prices), 14).iloc[14:].round(2).tolist()
    assert out == [70.46, 66.25, 66.48, 69.35, 66.29, 57.92, 62.88]
