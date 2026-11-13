"""今日のシグナルを作る。

方法: バックテストと同じエンジンを「固定の開始日 → 最新の営業日」まで走らせ、
最終日の終値時点で残っている注文（翌営業日に買う・売る）をシグナルとする。
バックテストと全く同じコードを通るので、検証したルールと本番のルールがずれない。

ここで追跡しているのは「ルールどおりに売買したと仮定したモデルポートフォリオ」であり、
開発者の実際の保有とは別物（実際の売買は人間が判断して手動で行う）。
"""
from __future__ import annotations

import sqlite3

from .backtest import MARKETS, build_panels, run_backtest
from .indicators import compute_indicators, load_bars
from .sources.base import split_symbol
from .strategy import BENCHMARKS, stop_price

# モデルポートフォリオの開始日。変えると保有銘柄の履歴が変わるので固定する。
LIVE_START = "2025-01-06"

SELL_REASONS = {
    "below_sma": "終値が手仕舞い用の移動平均（SMA200）を下回った",
    "rank_out": "モメンタム順位が保有継続の範囲から外れた",
    "stop": "損切り価格に到達した",
}


def compute_signals(conn: sqlite3.Connection, market: str, names: dict[str, str], as_of: str | None = None) -> dict:
    """market（JP/US）の最新営業日のシグナルを返す。names は 銘柄 -> 表示名。

    as_of を指定すると、その日までのデータだけで作る（動作確認用）。
    """
    cfg = MARKETS[market]
    params = cfg["params"]
    bm = BENCHMARKS[market]
    universe = [s for s in names if split_symbol(s)[1] == market and s != bm]
    panels = build_panels(conn, universe)
    all_dates = [d for d in panels["adj_close"].index if d >= LIVE_START and (as_of is None or d <= as_of)]
    if not all_dates:
        raise RuntimeError(f"{market}: {LIVE_START} 以降のデータがありません")
    day = all_dates[-1]
    res = run_backtest(panels, params, cfg["costs"], LIVE_START, day, cfg["cash"])
    close = panels["adj_close"].loc[day]

    def label(sym: str) -> str:
        return names.get(sym) or sym

    buys = []
    for sym, shares, atr in res.pending_buys:
        buys.append({
            "symbol": sym, "name": label(sym), "shares": shares, "close": float(close[sym]),
            "atr14": float(atr), "stop_ref": float(stop_price(close[sym], atr, params)),
            "rank": res.ranking.index(sym) + 1,
        })
    sells = []
    for sym, reason in res.pending_sells.items():
        pos = res.positions[sym]
        sells.append({
            "symbol": sym, "name": label(sym), "reason": reason, "reason_text": SELL_REASONS.get(reason, reason),
            "shares": pos.shares, "entry_date": pos.entry_date, "close": float(close[sym]),
            "pnl_pct": float(pos.shares * close[sym] / pos.entry_cost - 1),
        })
    stopped = [
        {"symbol": t["symbol"], "name": label(t["symbol"]), "entry_date": t["entry_date"], "pnl_pct": float(t["return"])}
        for t in res.trades if t["exit_date"] == day and t["reason"] == "stop"
    ]
    last_close = panels["adj_close"].ffill().loc[day]
    holdings = [
        {"symbol": s, "name": label(s), "shares": p.shares, "entry_date": p.entry_date, "stop": float(p.stop),
         "close": float(last_close[s]), "pnl_pct": float(p.shares * last_close[s] / p.entry_cost - 1),
         "sell_pending": s in res.pending_sells}
        for s, p in res.positions.items()
    ]

    bench = compute_indicators(load_bars(conn, bm))
    b = bench[bench["date"] <= day].iloc[-1]
    return {
        "market": market, "date": day, "currency": "JPY" if market == "JP" else "USD",
        "model_equity": float(res.equity.iloc[-1]), "model_start": LIVE_START, "model_initial_cash": cfg["cash"],
        "buys": buys, "sells": sells, "stopped_today": stopped, "holdings": holdings,
        "ranking_top": [{"symbol": s, "name": label(s)} for s in res.ranking[: params.top_n]],
        "benchmark": {"symbol": bm, "close": float(b["adj_close"]), "sma200": float(b["sma200"]),
                      "above_sma200": bool(b["adj_close"] > b["sma200"]), "ret_3m": float(b["ret_3m"])},
    }
