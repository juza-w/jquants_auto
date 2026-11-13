"""戦略v1（仮説・未検証）: 中期モメンタム + トレンドフィルター + ATRストップ。

このファイルは「ある日の終値時点の情報から、何を買い・何を売るべきか」だけを決める。
バックテスト（過去検証）と本番のシグナル生成で同じ関数を使い、ルールのずれを防ぐ。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

# 比較対象（売買はしない）
BENCHMARKS = {"JP": "1306.JP", "US": "SPY.US"}


@dataclass(frozen=True)
class StrategyParams:
    top_n: int = 5               # 保有する最大銘柄数
    min_turnover: float = 0.0    # 20日平均売買代金の下限（流動性の足切り）
    atr_stop_mult: float = 2.0   # 損切り幅 = この倍率 × ATR(14)
    risk_per_trade: float = 0.01 # 1トレードの想定損失 = 資金のこの割合まで
    lot_size: int = 1            # 売買単位（日本株は100株）


JP_PARAMS = StrategyParams(min_turnover=1_000_000_000, lot_size=100)  # 10億円/日
US_PARAMS = StrategyParams(min_turnover=50_000_000, lot_size=1)       # 5,000万ドル/日


def rank_candidates(snapshot: pd.DataFrame, params: StrategyParams) -> list[str]:
    """買い候補を強い順に返す。

    snapshot: index=銘柄、列=close(調整済み終値), sma50, sma200, mom_6_1, avg_turnover20, atr14。
              その日に取引がなかった銘柄は値が NaN になっている前提（NaN は自動的に落ちる）。
    条件:
      - 流動性: 20日平均売買代金 >= 下限
      - トレンドフィルター: 終値 > SMA200 かつ SMA50 > SMA200（長期で上昇中の銘柄だけ）
      - 終値 >= SMA50（手仕舞い条件「終値 < SMA50」に既に当たっている銘柄は買わない）
    並び順: 6か月モメンタム（直近1か月を除く）の高い順。
    """
    s = snapshot
    ok = (
        (s["avg_turnover20"] >= params.min_turnover)
        & (s["close"] > s["sma200"])
        & (s["sma50"] > s["sma200"])
        & (s["close"] >= s["sma50"])
        & s["mom_6_1"].notna()
        & (s["atr14"] > 0)
    )
    return s[ok].sort_values("mom_6_1", ascending=False, kind="stable").index.tolist()


def position_size(equity: float, atr: float, params: StrategyParams, lot: float | None = None) -> float:
    """株数 = 資金×1% ÷ (2×ATR) を売買単位で切り捨て。

    損切りに掛かったときの損失がちょうど資金の約1%になる株数。
    値動きの荒い（ATRが大きい）銘柄ほど少なく買うことになる。
    """
    lot = lot or params.lot_size
    if not atr or atr <= 0 or math.isnan(atr):
        return 0.0
    raw = equity * params.risk_per_trade / (params.atr_stop_mult * atr)
    return math.floor(raw / lot) * lot


def stop_price(entry_price: float, atr: float, params: StrategyParams) -> float:
    return entry_price - params.atr_stop_mult * atr
