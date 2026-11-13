"""テクニカル指標（M2）。

原則:
  - すべて「その日までのデータ」だけで計算する（先読みしない）。
  - 価格は分割調整済みを使う。分割で株価が半分になっても「暴落」と誤認しないため。
    高値・安値・始値は adj_close / close の比率（調整係数）を掛けて調整済みにする。
  - 取引のなかった日（終値がNull）は除いてから計算する。
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

# 「1か月」を営業日数に置き換える慣例（1年 ≒ 252営業日）
DAYS_1M, DAYS_3M, DAYS_6M, DAYS_12M = 21, 63, 126, 252


def load_bars(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT date, open, high, low, close, volume, adj_close FROM daily_bars "
        "WHERE symbol = ? ORDER BY date",
        conn,
        params=(symbol,),
    )


def wilder_smooth(values: pd.Series, n: int) -> pd.Series:
    """ワイルダーの平滑化（RSI・ATRの考案者 J.W.Wilder の方法）。

    最初の値 = 最初の n 個の単純平均、以降 = (前回値×(n-1) + 今回値) / n。
    values の先頭にある NaN は飛ばす。
    """
    out = np.full(len(values), np.nan)
    v = values.to_numpy(dtype=float)
    valid = np.flatnonzero(~np.isnan(v))
    if len(valid) < n:
        return pd.Series(out, index=values.index)
    first = valid[0]
    seed_end = first + n - 1
    out[seed_end] = v[first : seed_end + 1].mean()
    for i in range(seed_end + 1, len(v)):
        out[i] = (out[i - 1] * (n - 1) + v[i]) / n
    return pd.Series(out, index=values.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI: 直近の上げ幅と下げ幅の比から「買われすぎ/売られすぎ」を0〜100で表す。

    RSI = 100 − 100 / (1 + 平均上げ幅 / 平均下げ幅)。70超は買われすぎ、30未満は売られすぎの目安。
    """
    diff = close.diff()
    avg_gain = wilder_smooth(diff.clip(lower=0), n)
    avg_loss = wilder_smooth((-diff).clip(lower=0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100 - 100 / (1 + avg_gain / avg_loss)
    return out.where(avg_loss != 0, 100.0).where(avg_gain.notna())


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """1日の値動きの大きさ。前日終値からの窓（ギャップ）も含めて測る。"""
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.where(prev.notna())  # 初日は前日終値がないので計算しない


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """ATR: 真の値幅(TR)の平均。「この銘柄は1日に普通どれくらい動くか」を表す。"""
    return wilder_smooth(true_range(high, low, close), n)


def compute_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    """1銘柄の日足（date昇順）に指標の列を足して返す。"""
    df = bars.dropna(subset=["close", "adj_close"]).reset_index(drop=True).copy()
    factor = df["adj_close"] / df["close"]  # 調整係数（分割がなければ1）
    df["adj_open"] = df["open"] * factor
    df["adj_high"] = df["high"] * factor
    df["adj_low"] = df["low"] * factor
    c = df["adj_close"]

    for n in (20, 50, 200):
        df[f"sma{n}"] = c.rolling(n).mean()            # 単純移動平均
    df["rsi14"] = rsi(c, 14)
    df["atr14"] = atr(df["adj_high"], df["adj_low"], c, 14)
    df["avg_volume20"] = (df["volume"] / factor).rolling(20).mean()     # 出来高も分割調整して平均
    df["avg_turnover20"] = (df["close"] * df["volume"]).rolling(20).mean()  # 売買代金 ≒ 終値×出来高
    df["high20"] = df["adj_high"].rolling(20).max()    # 当日を含む直近20日の高値
    for name, n in (("ret_1m", DAYS_1M), ("ret_3m", DAYS_3M), ("ret_6m", DAYS_6M), ("ret_12m", DAYS_12M)):
        df[name] = c / c.shift(n) - 1                  # 騰落率
    # 6か月モメンタム（直近1か月を除く）: 6か月前 → 1か月前 の騰落率
    df["mom_6_1"] = c.shift(DAYS_1M) / c.shift(DAYS_6M) - 1
    df["mom_12_1"] = c.shift(DAYS_1M) / c.shift(DAYS_12M) - 1  # 12か月版（学術研究で標準的な形）
    return df
