"""判断案の記録（M5）。

すべての判断案を proposals テーブルに残し、その後の実際の値動きを後から埋める。
「通知に従っていたらどうなったか」を前向きに検証するための成績表になる。
株価DB（data/、取り直せる）とは別のファイル（state/、消えると困る）に置く。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,   -- 記録した時刻（UTC）
    market        TEXT NOT NULL,
    signal_date   TEXT NOT NULL,   -- シグナルの基準日（この日の終値までの情報で作った）
    symbol        TEXT NOT NULL,
    signal        TEXT NOT NULL,   -- ルールが出したシグナル: buy / sell
    signal_detail TEXT,            -- ルール側の情報（JSON）
    ref_close     REAL,            -- 基準日の調整済み終値
    action        TEXT,            -- Claude の判断: buy / sell / hold（Claude なしなら NULL）
    confidence    REAL,
    proposal_json TEXT,            -- Claude の判断案の全文（JSON）
    model         TEXT,
    error         TEXT,            -- Claude 判断に失敗した場合の理由
    next_open     REAL,            -- 以下は後から埋める: 翌営業日の始値（調整済み）
    ret_5d        REAL,            -- 翌営業日の始値で買ったと仮定した 5 / 20 / 60 営業日後の騰落率
    ret_20d       REAL,
    ret_60d       REAL,
    UNIQUE (market, signal_date, symbol, signal)
);
CREATE TABLE IF NOT EXISTS runs (
    market      TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (market, signal_date)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def already_run(conn: sqlite3.Connection, market: str, signal_date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM runs WHERE market = ? AND signal_date = ?", (market, signal_date)
    ).fetchone() is not None


def mark_run(conn: sqlite3.Connection, market: str, signal_date: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?)", (market, signal_date, _now()))


def record(conn: sqlite3.Connection, market: str, signal_date: str, kind: str, sig_item: dict,
           proposal: dict | None, model: str | None, error: str | None) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO proposals (created_at, market, signal_date, symbol, signal, signal_detail, "
            "ref_close, action, confidence, proposal_json, model, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), market, signal_date, sig_item["symbol"], kind, json.dumps(sig_item, ensure_ascii=False),
             sig_item["close"], proposal["action"] if proposal else None,
             proposal["confidence"] if proposal else None,
             json.dumps(proposal, ensure_ascii=False) if proposal else None, model, error),
        )


def update_outcomes(conn: sqlite3.Connection, bars: sqlite3.Connection) -> int:
    """まだ埋まっていない「その後の値動き」を株価DBから計算して埋める。更新した行数を返す。"""
    rows = conn.execute(
        "SELECT id, symbol, signal_date FROM proposals WHERE ret_60d IS NULL"
    ).fetchall()
    updated = 0
    for pid, symbol, signal_date in rows:
        later = bars.execute(
            "SELECT open * adj_close / close, adj_close FROM daily_bars "
            "WHERE symbol = ? AND date > ? AND close IS NOT NULL ORDER BY date LIMIT 60",
            (symbol, signal_date),
        ).fetchall()
        if not later:
            continue
        entry = later[0][0]  # 翌営業日の始値（調整済み）
        ret = {n: (later[n - 1][1] / entry - 1) if len(later) >= n else None for n in (5, 20, 60)}
        with conn:
            conn.execute(
                "UPDATE proposals SET next_open = ?, ret_5d = ?, ret_20d = ?, ret_60d = ? WHERE id = ?",
                (entry, ret[5], ret[20], ret[60], pid),
            )
        updated += 1
    return updated
