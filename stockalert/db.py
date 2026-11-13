"""SQLite への保存。主キー (symbol, date) で upsert する。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from .sources.base import COLUMNS

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol    TEXT NOT NULL,
    market    TEXT NOT NULL,
    currency  TEXT NOT NULL,
    date      TEXT NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    volume    REAL,
    adj_close REAL,
    PRIMARY KEY (symbol, date)
);
"""

# 同じ (symbol, date) が既にあれば上書き、なければ追加（= upsert）
_UPSERT = f"""
INSERT INTO daily_bars ({", ".join(COLUMNS)})
VALUES ({", ".join("?" for _ in COLUMNS)})
ON CONFLICT(symbol, date) DO UPDATE SET
    {", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c not in ("symbol", "date"))}
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA)
    return conn


def _records(df: pd.DataFrame) -> list[tuple]:
    # NaN は SQLite の NULL にする（取引のなかった日）
    obj = df[COLUMNS].astype(object).where(df[COLUMNS].notna(), None)
    return [tuple(r) for r in obj.itertuples(index=False, name=None)]


def upsert_bars(conn: sqlite3.Connection, df: pd.DataFrame) -> None:
    with conn:  # 途中で失敗したら全部取り消す
        conn.executemany(_UPSERT, _records(df))


def replace_symbol(conn: sqlite3.Connection, symbol: str, df: pd.DataFrame) -> None:
    """その銘柄の行を全部消して入れ直す（分割で過去の調整済み株価が変わったとき用）。"""
    with conn:
        conn.execute("DELETE FROM daily_bars WHERE symbol = ?", (symbol,))
        conn.executemany(_UPSERT, _records(df))


def recent_dates(conn: sqlite3.Connection, symbol: str, n: int) -> list[str]:
    """保存済みの直近 n 営業日の日付（新しい順）。"""
    cur = conn.execute(
        "SELECT date FROM daily_bars WHERE symbol = ? ORDER BY date DESC LIMIT ?", (symbol, n)
    )
    return [r[0] for r in cur]


def adj_close_since(conn: sqlite3.Connection, symbol: str, start: str) -> dict[str, float | None]:
    cur = conn.execute(
        "SELECT date, adj_close FROM daily_bars WHERE symbol = ? AND date >= ?", (symbol, start)
    )
    return dict(cur.fetchall())


def count_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
