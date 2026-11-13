"""増分更新。

ルール:
  1. 毎回、保存済みの直近10営業日分は取り直して上書きする（取りこぼし・訂正対策）。
  2. 取り直した範囲で「保存済みの調整済み終値」と「新しく取った調整済み終値」が
     食い違っていたら分割・併合があったとみなし、その銘柄の全期間を取り直す。
     （分割があると過去の調整済み株価がさかのぼって全部変わるため）
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from . import db
from .sources.base import DataSource, split_symbol

REFRESH_DAYS = 10       # 毎回取り直す営業日数
SPLIT_TOLERANCE = 1e-3  # 調整済み終値が0.1%超ずれたら「調整が変わった」と判定


@dataclass
class UpdateResult:
    symbol: str
    mode: str            # "initial"（初回）/ "incremental"（増分）/ "full_refetch"（分割検知で全取得）
    rows: int = 0
    error: str | None = None


def _adjustment_changed(stored: dict[str, float | None], fresh: pd.DataFrame) -> bool:
    for d, new in zip(fresh["date"], fresh["adj_close"]):
        old = stored.get(d)
        if old is None or new is None or pd.isna(new) or old == 0:
            continue  # 片方がNull（取引なし等）の日は比較しない
        if not math.isclose(old, new, rel_tol=SPLIT_TOLERANCE):
            return True
    return False


def update_symbol(conn: sqlite3.Connection, source: DataSource, symbol: str, today: date | None = None) -> UpdateResult:
    today = today or datetime.now(ZoneInfo(source.timezone)).date()  # 取引所の現地日付
    recent = db.recent_dates(conn, symbol, REFRESH_DAYS)

    if not recent:
        df = source.fetch_daily(symbol, None, today)
        db.upsert_bars(conn, df)
        return UpdateResult(symbol, "initial", len(df))

    window_start = recent[-1]  # 保存済みの10営業日前（10日未満ならある分だけ）
    fresh = source.fetch_daily(symbol, date.fromisoformat(window_start), today)
    stored = db.adj_close_since(conn, symbol, window_start)

    if _adjustment_changed(stored, fresh):
        full = source.fetch_daily(symbol, None, today)
        if len(full) == 0:
            raise RuntimeError(f"{symbol}: 分割を検知したが全期間の再取得が空だった（既存データは残した）")
        db.replace_symbol(conn, symbol, full)
        return UpdateResult(symbol, "full_refetch", len(full))

    db.upsert_bars(conn, fresh)
    return UpdateResult(symbol, "incremental", len(fresh))


def update_all(conn: sqlite3.Connection, sources: dict[str, DataSource], symbols: list[str]) -> list[UpdateResult]:
    """全銘柄を更新。1銘柄が失敗しても残りは続ける。"""
    results = []
    for symbol in symbols:
        try:
            _, market = split_symbol(symbol)
            results.append(update_symbol(conn, sources[market], symbol))
        except Exception as exc:  # noqa: BLE001 - 1銘柄の失敗で全体を止めない
            results.append(UpdateResult(symbol, "error", error=f"{type(exc).__name__}: {exc}"))
    return results
