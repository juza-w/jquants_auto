"""DB と増分更新のテスト。ネットワークは使わず、偽の DataSource で検証する。"""
from datetime import date

import pandas as pd

from stockalert import db
from stockalert.sources.base import COLUMNS, DataSource
from stockalert.updater import update_all, update_symbol

TODAY = date(2026, 3, 31)


class FakeSource(DataSource):
    """bars: {date文字列: (close, adj_close)}。close=None は取引のなかった日。"""

    market = "JP"
    timezone = "Asia/Tokyo"

    def __init__(self, bars):
        self.bars = dict(bars)
        self.calls = []

    def fetch_daily(self, symbol, start, end):
        self.calls.append((start, end))
        rows = []
        for d, (close, adj) in sorted(self.bars.items()):
            if start and d < start.isoformat():
                continue
            if end and d > end.isoformat():
                continue
            rows.append([symbol, "JP", "JPY", d, close, close, close, close,
                         None if close is None else 1000.0, adj])
        return pd.DataFrame(rows, columns=COLUMNS)


def make_bars(n, price=100.0):
    days = pd.bdate_range("2026-01-05", periods=n).strftime("%Y-%m-%d")
    return {d: (price + i, price + i) for i, d in enumerate(days)}


def test_second_run_does_not_add_rows():
    conn = db.connect(":memory:")
    src = FakeSource(make_bars(30))
    assert update_symbol(conn, src, "7203.JP", TODAY).mode == "initial"
    first = db.count_rows(conn)
    assert first == 30
    assert update_symbol(conn, src, "7203.JP", TODAY).mode == "incremental"
    assert db.count_rows(conn) == first


def test_incremental_refetches_last_10_trading_days_and_overwrites_corrections():
    conn = db.connect(":memory:")
    bars = make_bars(30)
    src = FakeSource(bars)
    update_symbol(conn, src, "7203.JP", TODAY)

    days = sorted(bars)
    src.bars[days[-3]] = (999.0, bars[days[-3]][1])  # 取引所が後から終値を訂正した想定
    new_day = "2026-02-16"
    src.bars[new_day] = (200.0, 200.0)               # 新しい営業日
    r = update_symbol(conn, src, "7203.JP", TODAY)

    assert r.mode == "incremental"
    assert src.calls[-1][0] == date.fromisoformat(days[-10])  # 直近10営業日前から取り直す
    assert db.count_rows(conn) == 31
    got = dict(conn.execute("SELECT date, close FROM daily_bars"))
    assert got[days[-3]] == 999.0 and got[new_day] == 200.0


def test_null_day_is_stored_and_does_not_stop():
    conn = db.connect(":memory:")
    bars = make_bars(15)
    null_day = sorted(bars)[7]
    bars[null_day] = (None, None)  # 取引なし: 四本値・出来高がNull
    src = FakeSource(bars)
    update_symbol(conn, src, "7203.JP", TODAY)
    assert update_symbol(conn, src, "7203.JP", TODAY).mode == "incremental"  # Nullを分割と誤認しない

    row = conn.execute(
        "SELECT open, close, volume, adj_close FROM daily_bars WHERE date = ?", (null_day,)
    ).fetchone()
    assert row == (None, None, None, None)
    assert db.count_rows(conn) == 15


def test_split_triggers_full_refetch():
    conn = db.connect(":memory:")
    bars = make_bars(30)
    src = FakeSource(bars)
    update_symbol(conn, src, "7203.JP", TODAY)

    # 1:2 の株式分割 → 過去の調整済み終値がすべて半分になる
    src.bars = {d: (c, a / 2) for d, (c, a) in bars.items()}
    r = update_symbol(conn, src, "7203.JP", TODAY)

    assert r.mode == "full_refetch"
    assert src.calls[-1][0] is None  # 全期間を取り直した
    oldest = sorted(bars)[0]
    adj = conn.execute("SELECT adj_close FROM daily_bars WHERE date = ?", (oldest,)).fetchone()[0]
    assert adj == bars[oldest][1] / 2  # 10日より前の古い行も更新されている
    assert db.count_rows(conn) == 30


def test_one_failing_symbol_does_not_stop_others():
    class Broken(FakeSource):
        def fetch_daily(self, symbol, start, end):
            if symbol == "0000.JP":
                raise RuntimeError("boom")
            return super().fetch_daily(symbol, start, end)

    conn = db.connect(":memory:")
    results = update_all(conn, {"JP": Broken(make_bars(5))}, ["0000.JP", "7203.JP"])
    assert results[0].error and results[1].error is None
    assert db.count_rows(conn) == 5


def test_latest_20_adjusted_closes_by_sql():
    conn = db.connect(":memory:")
    bars = make_bars(30)
    update_symbol(conn, FakeSource(bars), "7203.JP", TODAY)
    rows = conn.execute(
        "SELECT date, adj_close FROM daily_bars WHERE symbol = ? ORDER BY date DESC LIMIT 20",
        ("7203.JP",),
    ).fetchall()
    assert len(rows) == 20
    assert rows[0] == (sorted(bars)[-1], bars[sorted(bars)[-1]][1])
