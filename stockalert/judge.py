"""Claude 判断層（M4）。

シグナルが出た銘柄だけを Claude API に渡し、判断案を JSON で受け取る。
  1. ニュース調査（任意）: Claude の Web 検索ツールで直近のニュースを調べさせる。
  2. 判断: 数値データ + ニュース調査結果を渡し、決まった形の JSON で判断案を返させる。
Claude は発注しない。出力は人間が読むための「案」。

費用を抑えるため、既定のモデルは最も安い claude-haiku-5-5（環境変数 CLAUDE_MODEL で変更可）。
API の使い方は Anthropic 公式 SDK の資料に従っている:
  - 思考の深さは output_config.effort で指定（low にして出力量を抑える）
  - 構造化出力: output_config.format = {"type": "json_schema", "schema": ...}
  - Web 検索: tools = [{"type": "web_search_20250305", "name": "web_search"}]（全モデルで使える基本版）
"""
from __future__ import annotations

import json
import os
import sqlite3

from .indicators import compute_indicators, load_bars

MODEL = os.environ.get("CLAUDE_MODEL") or "claude-haiku-5-5"
MAX_SEARCHES = 2
YES_NO = {"type": "string", "enum": ["yes", "no", "unknown"]}

# 1回の実行で使った量（費用の確認用にログへ出す）
usage_total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0}

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "entry": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "stop_loss": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "take_profit": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "invalidation": {"type": "string"},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "earnings_soon": YES_NO,
        "bad_news": YES_NO,
        "news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "impact": {"type": "string"}},
                "required": ["title", "url", "impact"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["symbol", "action", "reasons", "risks", "entry", "stop_loss", "take_profit",
                 "invalidation", "confidence", "summary", "earnings_soon", "bad_news", "news"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = """あなたは個人投資家の売買判断を補助するアナリストです。機械的なルール（モメンタム戦略）が出した
シグナル1件について、渡されたデータを吟味し、判断案を日本語で作ります。最終判断と発注は人間が行います。

前提:
- このルールは過去検証でベンチマークに勝てておらず、優位性は確認できていません。シグナルを無批判に
  追認せず、データが弱い・悪材料がある場合は hold（見送り）を選んでください。
- 読み手は投資の初心者で、スマホの通知で読みます。短く書いてください。

出力の決まり:
- summary: 判断の理由を1文（60字以内）。通知にはこれだけが表示されます。
- reasons / risks: それぞれ最大2項目、各1文。数値は渡された「データ」にある値だけを使う。
- earnings_soon: 基準日から7日以内に決算発表がある（またはあった直後）と調査結果から分かるなら yes、
  ないと分かるなら no、分からなければ unknown。
- bad_news: 調査結果に悪材料（不祥事、業績の下方修正、訴訟・規制、上場廃止など）があれば yes、
  なければ no、調査結果が無ければ unknown。
- news: 「ニュース調査結果」に載っているものだけを URL 付きで最大2件。無ければ空配列。
- action: 買いシグナルなら buy か hold、売りシグナルなら sell か hold。
- entry / stop_loss / take_profit: データの通貨・調整済み価格で。根拠のある水準が無ければ null。
- invalidation: この判断が誤りだったと分かる条件を1文で。
- confidence: 0〜1。優位性が未確認であることを踏まえ控えめに。"""

NEWS_SYSTEM = """あなたは株式ニュースの調査員です。指定された銘柄について Web 検索で直近（おおむね過去30日）の
ニュースを調べ、売買判断に影響しうる事実だけを日本語で箇条書きにします（最大4項目）。
確認すること: 次回または直近の決算発表日、業績予想の修正、不祥事・訴訟・規制、買収や上場廃止。
各項目は「日付・見出し・URL・要点1文」。URL は検索結果に実在するものだけ。
見つからなければ「重要なニュースは見つからなかった」とだけ書く。売買の推奨はしない。"""


def rule_checks(ind: dict, benchmark: dict) -> dict:
    """数値だけで機械的に決まる Yes/No 項目（Claude に聞く必要がないもの）。"""
    def gt(a, b):
        return None if a is None or b is None else bool(a > b)

    return {
        "long_trend_up": None if None in (ind["close"], ind["sma50"], ind["sma200"])
        else bool(ind["close"] > ind["sma200"] and ind["sma50"] > ind["sma200"]),  # 長期は上昇トレンド
        "short_trend_up": gt(ind["close"], ind["sma20"]),                           # 短期も上向き
        "not_overheated": None if ind["rsi14"] is None else bool(ind["rsi14"] < 70),  # 買われすぎでない
        "month_positive": gt(ind["ret_1m_pct"], 0),                                 # 直近1か月がプラス
        "market_up": bool(benchmark["above_sma200"]),                               # 市場全体が上昇基調
    }


def build_packet(conn: sqlite3.Connection, sig: dict, kind: str, market_ctx: dict) -> dict:
    """Claude に渡すデータ。ここに入れた数値だけが根拠として引用できる。"""
    ind = compute_indicators(load_bars(conn, sig["symbol"]))
    ind = ind[ind["date"] <= market_ctx["date"]]
    last = ind.iloc[-1]

    def num(v, nd=2):
        return None if v != v else round(float(v), nd)  # NaN -> None

    recent = [
        {"date": r.date, "open": num(r.adj_open), "high": num(r.adj_high), "low": num(r.adj_low),
         "close": num(r.adj_close), "volume": num(r.volume, 0)}
        for r in ind.tail(10).itertuples()
    ]
    indicators = {
        "close": num(last["adj_close"]), "sma20": num(last["sma20"]), "sma50": num(last["sma50"]),
        "sma200": num(last["sma200"]), "rsi14": num(last["rsi14"], 1), "atr14": num(last["atr14"]),
        "atr_pct_of_close": num(last["atr14"] / last["adj_close"] * 100, 2),
        "high20": num(last["high20"]), "avg_turnover20": num(last["avg_turnover20"], 0),
        "ret_1m_pct": num(last["ret_1m"] * 100, 1), "ret_3m_pct": num(last["ret_3m"] * 100, 1),
        "ret_6m_pct": num(last["ret_6m"] * 100, 1), "ret_12m_pct": num(last["ret_12m"] * 100, 1),
        "mom_6_1_pct": num(last["mom_6_1"] * 100, 1),
    }
    return {
        "signal": {"kind": kind, **{k: v for k, v in sig.items() if k not in ("name",)}},
        "name": sig["name"], "date": market_ctx["date"], "currency": market_ctx["currency"],
        "indicators": indicators,
        "checks": rule_checks(indicators, market_ctx["benchmark"]),
        "recent_10_days": recent,
        "benchmark": market_ctx["benchmark"],
        "rule": "買い: 終値>SMA200 かつ SMA50>SMA200 の銘柄を6か月騰落率（直近1か月除く）で順位付けし上位10。"
                "売り: 終値<SMA200、30位より下、または 取得価格−2×ATR の損切り。",
    }


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")


def _create(client, **kwargs):
    """共通の呼び出し。使用量を記録し、pause_turn（サーバー側ツールの途中停止）は続きを要求する。"""
    messages = list(kwargs.pop("messages"))
    for _ in range(3):
        resp = client.messages.create(model=MODEL, max_tokens=4000, messages=messages, **kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            usage_total["calls"] += 1
            usage_total["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            usage_total["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
            server = getattr(usage, "server_tool_use", None)
            usage_total["web_searches"] += getattr(server, "web_search_requests", 0) or 0
        if resp.stop_reason != "pause_turn":
            return resp
        messages = messages + [{"role": "assistant", "content": resp.content}]
    return resp


def research_news(client, name: str, symbol: str, date: str) -> str:
    resp = _create(
        client, system=NEWS_SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}],
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": f"銘柄: {name}（{symbol}）\n基準日: {date}\nこの銘柄の直近のニュースを調べてください。"}],
    )
    if resp.stop_reason == "refusal":
        return ""
    return _text(resp).strip()


def judge(client, packet: dict, news: str | None) -> dict:
    """判断案を返す。API が断った・壊れた JSON を返した場合は例外を出す（呼び出し側でルールのみの通知に切替）。"""
    user = "## データ\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    user += "\n\n## ニュース調査結果\n" + (news if news else "（調査なし）")
    resp = _create(
        client, system=JUDGE_SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}},
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"Claude の応答が完了しませんでした: stop_reason={resp.stop_reason}")
    proposal = json.loads(_text(resp))
    proposal = validate_proposal(proposal, packet)
    proposal["checks"] = packet["checks"]  # 機械判定の Yes/No も一緒に記録・通知する
    return proposal


def validate_proposal(p: dict, packet: dict) -> dict:
    """形の最終確認（スキーマで保証されない点を含む）。"""
    missing = [k for k in PROPOSAL_SCHEMA["required"] if k not in p]
    if missing:
        raise ValueError(f"判断案に項目がありません: {missing}")
    allowed = {"buy": ("buy", "hold"), "sell": ("sell", "hold")}[packet["signal"]["kind"]]
    if p["action"] not in allowed:
        raise ValueError(f"シグナル {packet['signal']['kind']} に対して action={p['action']} は不正")
    p["symbol"] = packet["signal"]["symbol"]
    p["confidence"] = min(1.0, max(0.0, float(p["confidence"])))
    return p


def make_client():
    """ANTHROPIC_API_KEY があればクライアントを返す。なければ None（Claude なしで動かす）。"""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()  # 貼り付け時に混じった改行・空白を除く
    if not key:
        return None
    if any(c.isspace() for c in key):
        # キー以外の文字列（コマンド例など）を貼り付けた場合。値そのものは表示しない。
        raise ValueError("ANTHROPIC_API_KEY に空白や改行が含まれています。キーの文字列だけを登録してください")
    import anthropic

    return anthropic.Anthropic(api_key=key)
