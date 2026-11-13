"""データ取得の共通インターフェース。

保存・計算側はこのファイルだけを知っていればよい。
J-Quants か yfinance か、といった違いは各実装の中で吸収する。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

# daily_bars テーブルと同じ列。すべての DataSource はこの形の DataFrame を返す。
COLUMNS = [
    "symbol",     # 市場付き銘柄コード 例: 7203.JP / AAPL.US
    "market",     # JP / US
    "currency",   # JPY / USD
    "date",       # 取引所の現地日付 YYYY-MM-DD（UTCに変換しない）
    "open", "high", "low", "close",  # 調整前（その日に実際に付いた値段）
    "volume",     # 調整前の出来高
    "adj_close",  # 分割・併合のみ調整した終値（配当は調整しない）
]


def split_symbol(symbol: str) -> tuple[str, str]:
    """'7203.JP' -> ('7203', 'JP')、'BRK.B.US' -> ('BRK.B', 'US')。"""
    code, sep, market = symbol.rpartition(".")
    if not sep or not code or market not in ("JP", "US"):
        raise ValueError(f"銘柄コードは '7203.JP' や 'AAPL.US' の形式にしてください: {symbol!r}")
    return code, market


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


class DataSource(ABC):
    """日足データの取得元。"""

    market: str    # この取得元が担当する市場（JP / US）
    timezone: str  # 取引所のタイムゾーン（「今日」を決めるのに使う）

    @abstractmethod
    def fetch_daily(self, symbol: str, start: date | None, end: date | None) -> pd.DataFrame:
        """symbol の日足を start〜end（両端含む）で返す。

        start=None は「取得できる一番古い日から」、end=None は「最新まで」。
        戻り値は COLUMNS の列を持ち、date の昇順。取引のなかった日は
        価格・出来高が None/NaN の行として返ってよい（行がなくてもよい）。
        """
