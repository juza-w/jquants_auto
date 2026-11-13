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
    rebalance: str = "weekly"    # 入れ替えの頻度: "weekly"（週の最初の営業日）/ "monthly"（月の最初の営業日）
    hold_rank: int | None = None # 保有中の銘柄はこの順位まで持ち続ける（None なら top_n と同じ）
    mom_col: str = "mom_6_1"     # ランキングに使う騰落率の列
    exit_sma: str = "sma50"      # 終値がこの移動平均を下回ったら手仕舞い

    @property
    def keep_rank(self) -> int:
        return self.hold_rank or self.top_n


# 戦略v2: 作る用期間の比較（scripts/grid_search.py）で、日米どちらでも同じ向きに効いた3点だけ v1 から変更。
#   1. 手仕舞いの移動平均 SMA50 → SMA200（短期の揺れで売らない）
#   2. 上位10で買い、30位以内なら持ち続ける（順位の小さな入れ替わりで売買しない）
#   3. 銘柄数 5 → 10
# 入れ替え頻度（週1）・損切り幅（2×ATR）・騰落率（6か月-1か月）は v1 のまま。
V2 = dict(top_n=10, hold_rank=30, exit_sma="sma200")
JP_PARAMS = StrategyParams(min_turnover=1_000_000_000, lot_size=100, **V2)  # 10億円/日
US_PARAMS = StrategyParams(min_turnover=50_000_000, lot_size=1, **V2)       # 5,000万ドル/日


def rank_candidates(snapshot: pd.DataFrame, params: StrategyParams) -> list[str]:
    """買い候補を強い順に返す。

    snapshot: index=銘柄、列=close(調整済み終値), sma50, sma200, avg_turnover20, atr14 と
              params.mom_col / params.exit_sma で指定した列。
              その日に取引がなかった銘柄は値が NaN になっている前提（NaN は自動的に落ちる）。
    条件:
      - 流動性: 20日平均売買代金 >= 下限
      - トレンドフィルター: 終値 > SMA200 かつ SMA50 > SMA200（長期で上昇中の銘柄だけ）
      - 終値 >= 手仕舞い用の移動平均（手仕舞い条件に既に当たっている銘柄は買わない）
    並び順: モメンタム（直近1か月を除く騰落率）の高い順。
    """
    s = snapshot
    ok = (
        (s["avg_turnover20"] >= params.min_turnover)
        & (s["close"] > s["sma200"])
        & (s["sma50"] > s["sma200"])
        & (s["close"] >= s[params.exit_sma])
        & s[params.mom_col].notna()
        & (s["atr14"] > 0)
    )
    return s[ok].sort_values(params.mom_col, ascending=False, kind="stable").index.tolist()


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
