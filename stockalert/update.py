"""実行入口:  python -m stockalert.update

watchlist.txt の銘柄を更新して data/stock.db に保存する。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import db
from .sources.base import split_symbol
from .updater import update_all

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "stock.db"
WATCHLIST = ROOT / "watchlist.txt"


def load_watchlist(path: Path) -> list[str]:
    symbols = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            symbols.append(line)
    return symbols


def build_sources(markets: set[str]) -> dict:
    sources = {}
    if "JP" in markets:
        from .sources.jquants import JQuantsSource

        sources["JP"] = JQuantsSource(
            api_key=os.environ.get("JQUANTS_API_KEY", "").strip(),
            requests_per_min=int(os.environ.get("JQUANTS_REQUESTS_PER_MIN", "5")),
        )
    if "US" in markets:
        from .sources.yfinance_source import YFinanceSource

        sources["US"] = YFinanceSource()
    return sources


def main() -> int:
    load_dotenv(ROOT / ".env")
    symbols = load_watchlist(WATCHLIST)
    sources = build_sources({split_symbol(s)[1] for s in symbols})
    conn = db.connect(DB_PATH)

    before = db.count_rows(conn)
    results = update_all(conn, sources, symbols)
    after = db.count_rows(conn)

    for r in results:
        if r.error:
            print(f"[NG] {r.symbol}: {r.error}")
        else:
            print(f"[OK] {r.symbol}: {r.mode} {r.rows}行")
    print(f"daily_bars: {before} -> {after} 行")
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
