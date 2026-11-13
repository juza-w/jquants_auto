"""戦略の候補を「作る用」期間だけで比較する。検証用期間はここでは絶対に使わない。

実行:  python scripts/grid_search.py
見るのは「最高の1組」ではなく、各項目の平均的な効き方（たまたまの当たりを選ばないため）。
"""
import itertools
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockalert import db  # noqa: E402
from stockalert.backtest import MARKETS, build_panels, cagr, max_drawdown, run_backtest  # noqa: E402
from stockalert.sources.base import split_symbol  # noqa: E402
from stockalert.strategy import BENCHMARKS  # noqa: E402
from stockalert.update import DB_PATH, WATCHLIST, load_watchlist  # noqa: E402

GRID = {
    "rebalance": ["weekly", "monthly"],
    "hold_mult": [1, 2, 3],          # 保有継続の順位 = top_n × この倍率
    "top_n": [5, 10],
    "atr_stop_mult": [2.0, 3.0],
    "exit_sma": ["sma50", "sma200"],
    "mom_col": ["mom_6_1", "mom_12_1"],
}


def main() -> None:
    conn = db.connect(DB_PATH)
    watch = load_watchlist(WATCHLIST)
    rows = []
    for market, cfg in MARKETS.items():
        universe = [s for s in watch if split_symbol(s)[1] == market and s != BENCHMARKS[market]]
        panels = build_panels(conn, universe)
        start, end = cfg["periods"]["作る用"]
        for combo in itertools.product(*GRID.values()):
            g = dict(zip(GRID, combo))
            params = replace(cfg["params"], rebalance=g["rebalance"], top_n=g["top_n"],
                             hold_rank=g["top_n"] * g["hold_mult"], atr_stop_mult=g["atr_stop_mult"],
                             exit_sma=g["exit_sma"], mom_col=g["mom_col"])
            res = run_backtest(panels, params, cfg["costs"], start, end, cfg["cash"])
            rows.append({"market": market, **g, "cagr": cagr(res.equity), "mdd": max_drawdown(res.equity),
                         "trades": len(res.trades)})
    df = pd.DataFrame(rows)
    out = Path(__file__).resolve().parent.parent / "data" / "grid_design.csv"
    df.to_csv(out, index=False)
    pd.set_option("display.width", 200)
    for key in GRID:
        print(f"\n== {key} ごとの平均（作る用期間） ==")
        print(df.groupby(["market", key])[["cagr", "mdd", "trades"]].mean().round(3).to_string())
    for market in MARKETS:
        print(f"\n== {market} 上位10 ==")
        print(df[df.market == market].nlargest(10, "cagr").round(3).to_string(index=False))


if __name__ == "__main__":
    main()
