"""日本株: J-Quants API V2 の実装。

フィールド名は公式リファレンス（/ja/spec/eq-bars-daily）と実レスポンスで確認済み:
  Date, Code, O, H, L, C, Vo, Va, AdjFactor, AdjO, AdjH, AdjL, AdjC, AdjVo, ...
  トップレベルは {"data": [...], "pagination_key": "..."}（続きがある時だけ pagination_key が付く）
"""
from __future__ import annotations

import re
import time
from datetime import date

import pandas as pd
import requests

from .base import COLUMNS, DataSource, empty_frame, split_symbol

BASE_URL = "https://api.jquants.com/v2"

# プランの対象期間外を指定したときの 400 エラー文（実レスポンスで確認）:
# "Your subscription covers the following dates: 2024-08-21 ~ 2026-08-21. ..."
_COVERAGE_RE = re.compile(r"covers the following dates:\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})")


class JQuantsError(RuntimeError):
    pass


class JQuantsSource(DataSource):
    market = "JP"
    timezone = "Asia/Tokyo"

    def __init__(self, api_key: str, requests_per_min: int = 5, session=None, sleep=time.sleep):
        if not api_key:
            raise JQuantsError("JQUANTS_API_KEY が設定されていません（.env を確認）")
        self._session = session or requests.Session()
        self._headers = {"x-api-key": api_key}
        self._min_interval = 60.0 / max(1, requests_per_min)
        self._last_request = 0.0
        self._sleep = sleep
        self._coverage: tuple[date, date] | None = None  # プランで取得できる期間（判明したら覚える）

    # --- HTTP ---------------------------------------------------------
    def _get(self, path: str, params: dict) -> requests.Response:
        """レート制限を守って GET する。429 は公式の推奨どおり2分待って再試行。"""
        for attempt in range(3):
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
            resp = self._session.get(BASE_URL + path, headers=self._headers, params=params, timeout=60)
            if resp.status_code != 429:
                return resp
            self._sleep(120)
        raise JQuantsError("J-Quants のレート制限(429)が解除されません")

    def _clamp(self, start: date | None, end: date | None):
        """プランの対象期間が分かっていれば、その内側に丸める。"""
        if self._coverage:
            lo, hi = self._coverage
            start = lo if start is None else max(start, lo)
            end = hi if end is None else min(end, hi)
        return start, end

    # --- DataSource ---------------------------------------------------
    def fetch_daily(self, symbol: str, start: date | None, end: date | None) -> pd.DataFrame:
        code, market = split_symbol(symbol)
        if market != "JP":
            raise ValueError(f"JQuantsSource は .JP 専用です: {symbol}")

        rows: list[dict] = []
        pagination_key = None
        retried_coverage = False
        while True:
            s, e = self._clamp(start, end)
            if s and e and s > e:
                return empty_frame()  # 欲しい期間がプランの対象外（例: Freeプランの直近12週間）
            params = {"code": code}
            if s:
                params["from"] = s.isoformat()
            if e:
                params["to"] = e.isoformat()
            if pagination_key:
                params["pagination_key"] = pagination_key

            resp = self._get("/equities/bars/daily", params)
            if resp.status_code == 400 and not retried_coverage:
                m = _COVERAGE_RE.search(resp.text)
                if m:  # 対象期間を覚えて、丸めた期間で1回だけやり直す
                    self._coverage = (date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2)))
                    retried_coverage = True
                    continue
            if resp.status_code != 200:
                raise JQuantsError(f"J-Quants エラー {resp.status_code}: {resp.text[:200]}")

            body = resp.json()
            rows.extend(body.get("data", []))
            pagination_key = body.get("pagination_key")
            if not pagination_key:
                break

        if not rows:
            return empty_frame()
        df = pd.DataFrame(
            {
                "symbol": symbol,
                "market": "JP",
                "currency": "JPY",
                "date": [r["Date"] for r in rows],
                "open": [r["O"] for r in rows],
                "high": [r["H"] for r in rows],
                "low": [r["L"] for r in rows],
                "close": [r["C"] for r in rows],
                "volume": [r["Vo"] for r in rows],
                "adj_close": [r["AdjC"] for r in rows],
            },
            columns=COLUMNS,
        )
        return df.sort_values("date").reset_index(drop=True)
