"""判断層・通知・記録のテスト。Claude API と Discord は偽物に差し替える。"""
import json
from types import SimpleNamespace

import pytest

from stockalert import db, judge, notify, proposals

BUY = {"symbol": "7203.JP", "name": "トヨタ自動車", "shares": 200.0, "close": 3000.0,
       "atr14": 70.0, "stop_ref": 2860.0, "rank": 2}
SELL = {"symbol": "6758.JP", "name": "ソニーグループ", "reason": "below_sma", "reason_text": "終値がSMA200を下回った",
        "shares": 100.0, "entry_date": "2026-01-05", "close": 2800.0, "pnl_pct": -0.05}
PROPOSAL = {"symbol": "7203.JP", "action": "buy", "reasons": ["終値3000はSMA200の2700より上"], "risks": ["RSIが75と高い"],
            "entry": 3000.0, "stop_loss": 2860.0, "take_profit": None, "invalidation": "終値が2860を下回る",
            "confidence": 0.4, "news": [{"title": "決算発表", "url": "https://example.com/a", "impact": "中立"}]}


# ---- Claude 判断層 ----------------------------------------------------------
def text_resp(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def packet(kind="buy"):
    return {"signal": {"kind": kind, "symbol": "7203.JP"}, "name": "トヨタ自動車", "indicators": {"close": 3000.0}}


def test_judge_sends_data_and_schema_and_parses_json():
    client = FakeClient([text_resp(json.dumps(PROPOSAL, ensure_ascii=False))])
    out = judge.judge(client, packet(), news="2026-11-10 決算発表 https://example.com/a")

    assert out["action"] == "buy" and out["take_profit"] is None
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": judge.PROPOSAL_SCHEMA}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "tools" not in call  # 判断の呼び出しでは検索させない（渡したデータだけで判断）
    user = call["messages"][0]["content"]
    assert '"close": 3000.0' in user and "https://example.com/a" in user


def test_judge_rejects_wrong_action_refusal_and_clamps_confidence():
    wrong = dict(PROPOSAL, action="sell")  # 買いシグナルに対して「売り」は不正
    with pytest.raises(ValueError):
        judge.judge(FakeClient([text_resp(json.dumps(wrong))]), packet(), None)
    with pytest.raises(RuntimeError):
        judge.judge(FakeClient([text_resp("", stop_reason="refusal")]), packet(), None)
    high = dict(PROPOSAL, confidence=1.7, symbol="WRONG")
    out = judge.judge(FakeClient([text_resp(json.dumps(high))]), packet(), None)
    assert out["confidence"] == 1.0 and out["symbol"] == "7203.JP"


def test_research_news_uses_web_search_and_resumes_paused_turn():
    client = FakeClient([text_resp("", stop_reason="pause_turn"), text_resp("重要なニュースは見つからなかった")])
    out = judge.research_news(client, "トヨタ自動車", "7203.JP", "2026-11-13")
    assert out == "重要なニュースは見つからなかった"
    assert client.calls[0]["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    assert client.calls[1]["messages"][-1]["role"] == "assistant"  # 途中停止した応答を付けて続きを要求


def test_proposal_schema_is_strict():
    s = judge.PROPOSAL_SCHEMA
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    assert {"symbol", "action", "reasons", "risks", "entry", "stop_loss", "take_profit",
            "invalidation", "confidence"} <= set(s["properties"])


def test_make_client_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert judge.make_client() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-test-key\n")  # 前後の空白・改行は取り除く
    assert judge.make_client().api_key == "sk-test-key"


# ---- 通知 -------------------------------------------------------------------
def test_format_proposal_with_and_without_claude():
    text = notify.format_proposal(BUY, "buy", PROPOSAL, "JPY")
    assert "トヨタ自動車（7203.JP）" in text and "200株" in text and "2,860円" in text
    assert "Claude の判断案: 買い" in text and "確信度 40%" in text and "利確 —" in text
    assert "<https://example.com/a>" in text
    text = notify.format_proposal(SELL, "sell", None, "JPY", error="APIエラー")
    assert "売りシグナル" in text and "-5.0%" in text and "なし（APIエラー）" in text
    assert notify.money(123.456, "USD") == "$123.46"


def test_split_message_respects_limit():
    text = "\n".join(["あ" * 50] * 100)  # 約5000文字
    chunks = notify.split_message(text, limit=1900)
    assert all(len(c) <= 1900 for c in chunks) and "\n".join(chunks) == text
    assert [len(c) for c in notify.split_message("x" * 4000, limit=1900)] == [1900, 1900, 200]


def test_send_posts_to_discord_and_hides_url_on_error(capsys):
    posted = []

    def post(url, params=None, json=None, timeout=None):
        posted.append((url, params, json))
        return SimpleNamespace(status_code=200)

    assert notify.send(["こんにちは"], webhook_url="https://discord.example/secret", post=post, sleep=lambda s: None)
    assert posted == [("https://discord.example/secret", {"wait": "true"}, {"content": "こんにちは"})]

    with pytest.raises(RuntimeError) as err:
        notify.send(["x"], webhook_url="https://discord.example/secret",
                    post=lambda *a, **k: SimpleNamespace(status_code=404), sleep=lambda s: None)
    assert "secret" not in str(err.value)  # 秘密のURLをエラー文に出さない

    assert notify.send(["表示のみ"], webhook_url="") is False  # URLなしなら画面に出すだけ
    assert "表示のみ" in capsys.readouterr().out


# ---- 記録 -------------------------------------------------------------------
def test_proposals_are_recorded_once_and_outcomes_filled_later():
    state = proposals.connect(":memory:")
    bars = db.connect(":memory:")
    assert not proposals.already_run(state, "JP", "2026-01-05")
    proposals.record(state, "JP", "2026-01-05", "buy", BUY, PROPOSAL, "claude-opus-5-5", None)
    proposals.record(state, "JP", "2026-01-05", "buy", BUY, PROPOSAL, "claude-opus-5-5", None)  # 再実行
    proposals.mark_run(state, "JP", "2026-01-05")
    assert proposals.already_run(state, "JP", "2026-01-05")
    assert state.execute("SELECT COUNT(*), MAX(action), MAX(confidence) FROM proposals").fetchone() == (1, "buy", 0.4)

    assert proposals.update_outcomes(state, bars) == 0  # まだ翌営業日のデータがない

    # 翌営業日以降の株価: 始値100 → 5営業日目の終値110。途中で取引なしの日（Null）がある
    rows = [("2026-01-05", 99, 99), ("2026-01-06", 100, 101), ("2026-01-07", None, None), ("2026-01-08", 102, 103),
            ("2026-01-09", 104, 105), ("2026-01-12", 106, 107), ("2026-01-13", 108, 110)]
    for d, o, c in rows:
        bars.execute("INSERT INTO daily_bars VALUES ('7203.JP','JP','JPY',?,?,?,?,?,?,?)", (d, o, o, o, c, 1000.0, c))
    assert proposals.update_outcomes(state, bars) == 1
    next_open, r5, r20 = state.execute("SELECT next_open, ret_5d, ret_20d FROM proposals").fetchone()
    assert next_open == 100 and r5 == pytest.approx(0.10) and r20 is None  # 20営業日後はまだ来ていない
