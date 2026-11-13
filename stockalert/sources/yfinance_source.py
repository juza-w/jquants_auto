"""米国株: yfinance（Yahoo Finance の非公式ライブラリ）の実装。将来 moomoo に差し替える。

実レスポンスで確認した形（auto_adjust=False, actions=True）:
  列: Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits
  インデックス: America/New_York のタイムゾーン付き日時

Yahoo の値の意味（J-Quants と違うので、ここで揃える）:
  - Open/High/Low/Close/Volume は「分割調整済み」（配当は未調整）
  - Adj Close は「分割 + 配当」調整済み
共通形式の adj_close は「分割のみ調整」と決めているので Yahoo の Close を入れ、
調整前の値は Stock Splits（その日に効いた分割比率。4.0 なら 1株→4株）から逆算する。
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from .base import COLUMNS, DataSource, empty_frame, split_symbol

NY = ZoneInfo("America/New_York")


def _default_download(ticker: str, start: date) -> pd.DataFrame:
    import yfinance as yf

    return yf.Ticker(ticker).history(
        start=start.isoformat(), interval="1d", auto_adjust=False, actions=True
    )


class YFinanceSource(DataSource):
    market = "US"
    timezone = "America/New_York"

    def __init__(self, history_start: date = date(2010, 1, 1), download=_default_download, now=None):
        self._history_start = history_start
        self._download = download
        self._now = now or (lambda: datetime.now(NY))

    def fetch_daily(self, symbol: str, start: date | None, end: date | None) -> pd.DataFrame:
        code, market = split_symbol(symbol)
        if market != "US":
            raise ValueError(f"YFinanceSource は .US 専用です: {symbol}")
        ticker = code.replace(".", "-")  # Yahoo は BRK.B を BRK-B と書く

        # 調整前の値を逆算するには「その日より後の分割」が全部必要なので、
        # end に関係なく必ず最新まで取得し、最後に end で切る。
        raw = self._download(ticker, start or self._history_start)
        if raw is None or len(raw) == 0:
            return empty_frame()
        raw = raw.sort_index()

        # その日より後に起きた分割比率の積（＝調整係数の逆数）
        splits = raw["Stock Splits"].where(raw["Stock Splits"] > 0, 1.0)
        later = splits[::-1].cumprod()[::-1].shift(-1).fillna(1.0)

        df = pd.DataFrame(
            {
                "symbol": symbol,
                "market": "US",
                "currency": "USD",
                "date": raw.index.strftime("%Y-%m-%d"),  # NYの現地日付のまま
                "open": (raw["Open"] * later).to_numpy(),
                "high": (raw["High"] * later).to_numpy(),
                "low": (raw["Low"] * later).to_numpy(),
                "close": (raw["Close"] * later).to_numpy(),
                "volume": (raw["Volume"] / later).to_numpy(),
                "adj_close": raw["Close"].to_numpy(),
            },
            columns=COLUMNS,
        )

        # 取引時間中に実行すると「今日の途中経過」の足が返る。確定前の足は捨てる
        # （終値が確定していないデータでシグナルを作らないため）。
        now = self._now()
        if now.time() < dtime(16, 30):
            df = df[df["date"] != now.date().isoformat()]
        if start:
            df = df[df["date"] >= start.isoformat()]
        if end:
            df = df[df["date"] <= end.isoformat()]
        return df.reset_index(drop=True)
