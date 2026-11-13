"""バックテスト（M3）: 戦略v1 を過去データで検証する。

実行:  python -m stockalert.backtest

先読みを避けるための約束:
  - 売買の判断は「その日の終値まで」の情報で行い、約定は「翌営業日の始値」。
  - 損切りは日中の安値が損切り価格に触れたら執行（窓を開けて下で始まったら始値で執行）。
価格はすべて分割調整済み（配当は含まない。ベンチマークも同じ条件）。
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import db
from .indicators import compute_indicators, load_bars
from .sources.base import split_symbol
from .strategy import BENCHMARKS, JP_PARAMS, US_PARAMS, StrategyParams, position_size, rank_candidates, stop_price

FIELDS = ["adj_open", "adj_high", "adj_low", "adj_close", "close", "sma50", "sma200", "atr14", "mom_6_1", "mom_12_1", "avg_turnover20"]


@dataclass(frozen=True)
class Costs:
    commission: float  # 片道の手数料率
    slippage: float    # 片道のスリッページ率（注文が不利な値段で約定する分）


@dataclass
class Position:
    symbol: str
    shares: float
    entry_date: str
    entry_cost: float  # 手数料・スリッページ込みの取得総額
    stop: float


@dataclass
class Result:
    equity: pd.Series                     # 日次の資産額
    trades: list[dict] = field(default_factory=list)  # 決済済みトレード
    open_positions: int = 0
    skipped_buys: int = 0                 # 単元株が買えず見送った回数


def build_panels(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """指標を計算して「日付 × 銘柄」の表にする（項目ごとに1枚）。"""
    per_symbol = {}
    for sym in symbols:
        ind = compute_indicators(load_bars(conn, sym))
        if len(ind):
            per_symbol[sym] = ind.set_index("date")
    return {f: pd.DataFrame({s: d[f] for s, d in per_symbol.items()}).sort_index() for f in FIELDS}


def run_backtest(panels: dict[str, pd.DataFrame], params: StrategyParams, costs: Costs,
                 start: str, end: str, initial_cash: float) -> Result:
    dates = [d for d in panels["adj_close"].index if start <= d <= end]
    last_close = panels["adj_close"].ffill()  # 取引のない日は直近の終値で評価
    buy_mult = (1 + costs.slippage) * (1 + costs.commission)
    sell_mult = (1 - costs.slippage) * (1 - costs.commission)

    cash = initial_cash
    positions: dict[str, Position] = {}
    pending_sells: dict[str, str] = {}                 # 銘柄 -> 理由
    pending_buys: list[tuple[str, float, float]] = []  # (銘柄, 株数, シグナル日のATR)
    result = Result(equity=pd.Series(dtype=float))
    equity_curve = {}

    def close_position(sym: str, price: float, day: str, reason: str) -> None:
        nonlocal cash
        pos = positions.pop(sym)
        proceeds = pos.shares * price * sell_mult
        cash += proceeds
        result.trades.append({
            "symbol": sym, "entry_date": pos.entry_date, "exit_date": day, "reason": reason,
            "pnl": proceeds - pos.entry_cost, "return": proceeds / pos.entry_cost - 1,
        })

    for i, day in enumerate(dates):
        o, h, l, c = (panels[k].loc[day] for k in ("adj_open", "adj_high", "adj_low", "adj_close"))

        # 1) 前日までに決めた注文を、今日の始値で約定させる（売りが先）
        for sym in list(pending_sells):
            if sym not in positions:
                del pending_sells[sym]
            elif not math.isnan(o[sym]):  # 今日取引がなければ翌日に持ち越し
                close_position(sym, o[sym], day, pending_sells.pop(sym))
        for sym, shares, atr in pending_buys:
            price = o[sym]
            if sym in positions or math.isnan(price):
                continue
            lot = params.lot_size * panels["close"].loc[day, sym] / c[sym]  # 分割調整後の株数での1単元
            affordable = math.floor(cash / (price * buy_mult) / lot) * lot
            shares = min(shares, affordable)
            if shares <= 0:
                result.skipped_buys += 1
                continue
            cost = shares * price * buy_mult
            cash -= cost
            positions[sym] = Position(sym, shares, day, cost, stop_price(price, atr, params))
        pending_buys = []

        # 2) 損切り: 日中に損切り価格に触れたら決済
        for sym, pos in list(positions.items()):
            if math.isnan(l[sym]):
                continue
            if o[sym] <= pos.stop:
                close_position(sym, o[sym], day, "stop")      # 窓開け下落: 始値でしか売れない
            elif l[sym] <= pos.stop:
                close_position(sym, pos.stop, day, "stop")

        # 3) 終値で資産を評価
        equity = cash + sum(p.shares * last_close.loc[day, s] for s, p in positions.items())
        equity_curve[day] = equity

        # 4) 終値が確定した後に、翌営業日の注文を決める
        snap = pd.DataFrame({
            "close": c, "sma50": panels["sma50"].loc[day], "sma200": panels["sma200"].loc[day],
            params.mom_col: panels[params.mom_col].loc[day], "avg_turnover20": panels["avg_turnover20"].loc[day],
            "atr14": panels["atr14"].loc[day],
        })
        for sym in positions:  # 毎日: 終値 < 移動平均 なら手仕舞い
            if c[sym] < snap.loc[sym, params.exit_sma]:
                pending_sells.setdefault(sym, "below_sma")

        if i + 1 < len(dates) and is_rebalance_day(day, dates[i + 1], params.rebalance):  # 定期の入れ替え
            ranked = rank_candidates(snap, params)
            keep = set(ranked[: params.keep_rank])
            for sym in positions:
                if sym not in keep:
                    pending_sells.setdefault(sym, "rank_out")
            slots = params.top_n - sum(1 for s in positions if s not in pending_sells)
            for sym in ranked[: params.top_n]:
                if slots <= 0:
                    break
                if sym in positions:
                    continue
                slots -= 1
                atr = snap.loc[sym, "atr14"]
                lot = params.lot_size * panels["close"].loc[day, sym] / c[sym]
                shares = position_size(equity, atr, params, lot)
                if shares <= 0:
                    result.skipped_buys += 1
                else:
                    pending_buys.append((sym, shares, atr))

    result.equity = pd.Series(equity_curve)
    result.open_positions = len(positions)
    return result


def is_rebalance_day(day: str, next_day: str, rebalance: str) -> bool:
    """day がその週（または月）の最後の営業日か。次の営業日が別の週・月なら True。"""
    if rebalance == "monthly":
        return day[:7] != next_day[:7]
    a, b = pd.Timestamp(day).isocalendar(), pd.Timestamp(next_day).isocalendar()
    return (a.year, a.week) != (b.year, b.week)


# ---- 成績指標 ---------------------------------------------------------------
def cagr(series: pd.Series) -> float:
    """年率リターン: 期間全体の増え方を「1年あたり何%の複利か」に直したもの。"""
    days = (pd.Timestamp(series.index[-1]) - pd.Timestamp(series.index[0])).days
    return (series.iloc[-1] / series.iloc[0]) ** (365.25 / days) - 1 if days > 0 else float("nan")


def max_drawdown(series: pd.Series) -> float:
    """最大ドローダウン: それまでの最高額から最も大きく減った割合（負の値）。"""
    return float((series / series.cummax() - 1).min())


def summarize(result: Result, benchmark: pd.Series) -> dict:
    wins = [t["return"] for t in result.trades if t["pnl"] > 0]
    losses = [t["return"] for t in result.trades if t["pnl"] <= 0]
    n = len(result.trades)
    bench = benchmark.reindex(result.equity.index).ffill().dropna()
    return {
        "年率リターン": cagr(result.equity),
        "最大DD": max_drawdown(result.equity),
        "勝率": len(wins) / n if n else float("nan"),
        "平均利益": sum(wins) / len(wins) if wins else float("nan"),
        "平均損失": sum(losses) / len(losses) if losses else float("nan"),
        "取引回数": n,
        "見送り": result.skipped_buys,
        "BM年率": cagr(bench),
        "BM最大DD": max_drawdown(bench),
    }


# ---- 実行設定 ---------------------------------------------------------------
# 手数料・スリッページ・資金は「仮定」。実際の証券会社の条件に合わせて直すこと。
MARKETS = {
    "JP": {
        "params": JP_PARAMS, "costs": Costs(commission=0.001, slippage=0.001), "cash": 3_000_000,
        # J-Quants Light は5年分。SMA200 と12か月騰落率の助走に約1年使うため開始は2022-12
        "periods": {"作る用": ("2022-12-01", "2024-11-30"), "検証用": ("2024-12-01", "2026-12-31")},
    },
    "US": {
        "params": US_PARAMS, "costs": Costs(commission=0.005, slippage=0.001), "cash": 30_000,
        "periods": {"作る用": ("2011-01-01", "2019-12-31"), "検証用": ("2020-01-01", "2026-12-31")},
    },
}


def main() -> None:
    from .update import DB_PATH, WATCHLIST, load_watchlist

    conn = db.connect(DB_PATH)
    watch = load_watchlist(Path(WATCHLIST))
    rows = []
    for market, cfg in MARKETS.items():
        bm = BENCHMARKS[market]
        universe = [s for s in watch if split_symbol(s)[1] == market and s != bm]
        panels = build_panels(conn, universe)
        benchmark = compute_indicators(load_bars(conn, bm)).set_index("date")["adj_close"]
        for label, (start, end) in cfg["periods"].items():
            res = run_backtest(panels, cfg["params"], cfg["costs"], start, end, cfg["cash"])
            s = summarize(res, benchmark)
            rows.append({"市場": market, "期間": label, "開始": res.equity.index[0], "終了": res.equity.index[-1],
                         "銘柄数": len(universe), **s})
    out = pd.DataFrame(rows)
    for col in ("年率リターン", "最大DD", "勝率", "平均利益", "平均損失", "BM年率", "BM最大DD"):
        out[col] = out[col].map(lambda v: f"{v:+.1%}" if col != "勝率" else f"{v:.1%}")
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
