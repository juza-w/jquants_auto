"""各 DataSource が共通形式に正しく変換するかのテスト（通信は偽物に差し替え）。"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from stockalert.sources.base import COLUMNS, split_symbol
from stockalert.sources.jquants import JQuantsError, JQuantsSource
from stockalert.sources.yfinance_source import YFinanceSource


def test_split_symbol():
    assert split_symbol("7203.JP") == ("7203", "JP")
    assert split_symbol("BRK.B.US") == ("BRK.B", "US")
    with pytest.raises(ValueError):
        split_symbol("7203")


# ---- J-Quants -------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append((url, dict(headers), dict(params)))
        return self.responses.pop(0)


def jq_row(d, close, adj=None):
    """実レスポンスと同じキー（Freeプランで返る項目）。close=None は取引なしの日。"""
    return {"Date": d, "Code": "72030", "O": close, "H": close, "L": close, "C": close,
            "UL": "0", "LL": "0", "Vo": None if close is None else 100.0,
            "Va": None if close is None else 1.0, "AdjFactor": 1.0,
            "AdjO": adj or close, "AdjH": adj or close, "AdjL": adj or close,
            "AdjC": adj or close, "AdjVo": None if close is None else 100.0,
            "MktCap": None, "ExRT": None}


def make_jq(responses):
    session = FakeSession(responses)
    return JQuantsSource("dummy-key", session=session, sleep=lambda s: None), session


def test_jquants_maps_fields_follows_pagination_and_keeps_null_day():
    src, session = make_jq([
        FakeResponse(200, {"data": [jq_row("2026-08-18", 3000.0, 1500.0)], "pagination_key": "k1"}),
        FakeResponse(200, {"data": [jq_row("2026-08-19", None), jq_row("2026-08-20", 3100.0)]}),
    ])
    df = src.fetch_daily("7203.JP", date(2026, 8, 18), date(2026, 8, 20))

    assert list(df.columns) == COLUMNS
    assert list(df["date"]) == ["2026-08-18", "2026-08-19", "2026-08-20"]
    assert df.loc[0, ["symbol", "market", "currency"]].tolist() == ["7203.JP", "JP", "JPY"]
    assert df.loc[0, "close"] == 3000.0 and df.loc[0, "adj_close"] == 1500.0
    assert pd.isna(df.loc[1, "close"]) and pd.isna(df.loc[1, "volume"])

    url, headers, params = session.requests[0]
    assert url == "https://api.jquants.com/v2/equities/bars/daily"
    assert headers == {"x-api-key": "dummy-key"}
    assert params == {"code": "7203", "from": "2026-08-18", "to": "2026-08-20"}
    assert session.requests[1][2]["pagination_key"] == "k1"


COVERAGE_MSG = {"message": "Your subscription covers the following dates: 2024-08-21 ~ 2026-08-21. "
                           "If you want more data, please check other plans"}


def test_jquants_clamps_to_plan_coverage_and_remembers_it():
    src, session = make_jq([
        FakeResponse(400, COVERAGE_MSG),
        FakeResponse(200, {"data": [jq_row("2026-08-21", 3132.0)]}),
    ])
    df = src.fetch_daily("7203.JP", date(2026, 8, 10), date(2026, 11, 13))
    assert len(df) == 1
    assert session.requests[1][2] == {"code": "7203", "from": "2026-08-10", "to": "2026-08-21"}

    # 2回目以降は対象期間を覚えているので、期間外だけの要求は通信せず空を返す
    assert len(src.fetch_daily("7203.JP", date(2026, 9, 1), date(2026, 11, 13))) == 0
    assert len(session.requests) == 2


def test_jquants_raises_on_other_errors_and_missing_key():
    src, _ = make_jq([FakeResponse(403, {"message": "forbidden"})])
    with pytest.raises(JQuantsError):
        src.fetch_daily("7203.JP", None, None)
    with pytest.raises(JQuantsError):
        JQuantsSource("")


def test_jquants_waits_and_retries_on_429():
    slept = []
    session = FakeSession([FakeResponse(429, {}), FakeResponse(200, {"data": [jq_row("2026-08-20", 1.0)]})])
    src = JQuantsSource("k", session=session, sleep=slept.append)
    assert len(src.fetch_daily("7203.JP", None, None)) == 1
    assert 120 in slept


# ---- yfinance -------------------------------------------------------------
NY = ZoneInfo("America/New_York")


def yahoo_frame():
    """Yahooの形: 価格は分割調整済み。2026-03-04 に 1:4 の分割（Stock Splits=4.0）。"""
    idx = pd.DatetimeIndex(["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]).tz_localize(NY)
    return pd.DataFrame(
        {"Open": [100.0, 101.0, 102.0, 103.0], "High": [100.0, 101.0, 102.0, 103.0],
         "Low": [100.0, 101.0, 102.0, 103.0], "Close": [100.0, 101.0, 102.0, 103.0],
         "Adj Close": [99.0, 100.0, 101.0, 102.0], "Volume": [4000.0, 4000.0, 1000.0, 1000.0],
         "Dividends": [0.0] * 4, "Stock Splits": [0.0, 0.0, 4.0, 0.0]},
        index=idx,
    )


def make_yf(now, seen=None):
    def download(ticker, start):
        if seen is not None:
            seen.append((ticker, start))
        return yahoo_frame()

    return YFinanceSource(download=download, now=lambda: now)


def test_yfinance_undoes_split_for_raw_prices_and_keeps_split_adjusted_close():
    src = make_yf(datetime(2026, 3, 6, 18, 0, tzinfo=NY))
    df = src.fetch_daily("AAPL.US", None, None)

    assert list(df.columns) == COLUMNS
    assert list(df["date"]) == ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
    assert df.loc[0, ["market", "currency"]].tolist() == ["US", "USD"]
    # 分割前の日: 実際の値段は4倍、出来高は1/4だった
    assert list(df["close"]) == [400.0, 404.0, 102.0, 103.0]
    assert list(df["volume"]) == [1000.0, 1000.0, 1000.0, 1000.0]
    # adj_close は分割のみ調整（配当込みの Adj Close は使わない）
    assert list(df["adj_close"]) == [100.0, 101.0, 102.0, 103.0]


def test_yfinance_drops_unfinished_bar_and_applies_date_range():
    # NY時間 3/5 の取引時間中に実行 → 3/5 の足は未確定なので捨てる
    seen = []
    src = make_yf(datetime(2026, 3, 5, 11, 0, tzinfo=NY), seen)
    df = src.fetch_daily("BRK.B.US", date(2026, 3, 3), date(2026, 3, 5))
    assert list(df["date"]) == ["2026-03-03", "2026-03-04"]
    assert seen == [("BRK-B", date(2026, 3, 3))]  # Yahooの銘柄表記に変換
    # end で切っても、後の分割は反映されている
    df2 = make_yf(datetime(2026, 3, 6, 18, 0, tzinfo=NY)).fetch_daily("AAPL.US", None, date(2026, 3, 3))
    assert list(df2["close"]) == [400.0, 404.0]
