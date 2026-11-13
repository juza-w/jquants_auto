"""通知（M5）: Discord webhook。

Discord 公式ドキュメント（Execute Webhook）で確認した仕様:
  POST <webhook URL>  JSON {"content": "本文"}  本文は2000文字まで。
  ?wait=true を付けると、保存に失敗したときエラーが返る。
Webhook URL は秘密情報（知っている人は誰でも投稿できる）。.env / GitHub Secrets に置く。
"""
from __future__ import annotations

import os
import time

import requests

LIMIT = 1900  # 2000文字制限に余裕を持たせる

ACTION_JA = {"buy": "買い", "sell": "売り", "hold": "見送り"}
DISCLAIMER = "※参考情報（優位性は未検証）。発注は自分の判断で。"

# 数値から機械的に決まる項目（Yes が良い状態）
RULE_CHECKS = [
    ("long_trend_up", "長期は上昇トレンド"),
    ("short_trend_up", "短期も上向き"),
    ("not_overheated", "過熱していない"),
    ("month_positive", "直近1か月プラス"),
    ("market_up", "市場全体が上昇基調"),
]
# Claude がニュースから答える項目（Yes が注意すべき状態）
NEWS_CHECKS = [("earnings_soon", "決算が近い"), ("bad_news", "悪材料ニュースあり")]


def money(v, currency: str) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}円" if currency == "JPY" else f"${v:,.2f}"


def yes_no(value, yes_is_good: bool = True) -> str:
    """True/False/None（または "yes"/"no"/"unknown"）を Yes / No / 不明 に。良い状態に ✅、注意に ⚠️。"""
    if value in (None, "unknown"):
        return "❓ 不明"
    yes = value in (True, "yes")
    return f"{'✅' if yes == yes_is_good else '⚠️'} {'Yes' if yes else 'No'}"


def format_header(sig: dict) -> str:
    bm = sig["benchmark"]
    lines = [
        f"**【{sig['market']}】{sig['date']} 終値** 買い{len(sig['buys'])} / 売り{len(sig['sells'])} / "
        f"損切り到達{len(sig['stopped_today'])}",
        DISCLAIMER,
    ]
    for s in sig["stopped_today"]:
        lines.append(f"・損切り到達: {s['name']}（{s['symbol']}）{s['pnl_pct']:+.1%}")
    return "\n".join(lines)


def format_proposal(sig_item: dict, kind: str, proposal: dict | None, currency: str,
                    error: str | None = None, checks: dict | None = None) -> str:
    icon, label = ("🟢", "買い候補") if kind == "buy" else ("🔴", "売り候補")
    if proposal is not None:
        verdict = f"Claude: **{ACTION_JA[proposal['action']]}**（{proposal['confidence']:.0%}）"
    else:
        verdict = "Claude: 判断なし"
    lines = [f"{icon} **{label}: {sig_item['name']}（{sig_item['symbol']}）** → {verdict}"]
    if proposal is not None:
        lines.append(proposal["summary"])
    elif error:
        lines.append(f"（{error}）")
    if kind == "buy":
        lines.append(f"終値 {money(sig_item['close'], currency)} / {sig_item['shares']:,.0f}株 / "
                     f"損切り {money(sig_item['stop_ref'], currency)}")
    else:
        lines.append(f"{sig_item['reason_text']} / 終値 {money(sig_item['close'], currency)} / "
                     f"損益 {sig_item['pnl_pct']:+.1%}")
    checks = checks or (proposal or {}).get("checks") or {}
    if kind == "buy":
        lines += [f"{text}: {yes_no(checks.get(key))}" for key, text in RULE_CHECKS if key in checks]
    if proposal is not None:
        lines += [f"{text}: {yes_no(proposal[key], yes_is_good=False)}" for key, text in NEWS_CHECKS]
        for n in proposal["news"][:1]:
            lines.append(f"📰 {n['title'][:60]} <{n['url']}>")
    return "\n".join(lines)


def format_holdings(sig: dict) -> str:
    items = [f"{h['name']} {h['pnl_pct']:+.1%}{'（売り予定）' if h['sell_pending'] else ''}" for h in sig["holdings"]]
    return (f"**仮想運用の保有**（実際の保有とは別）評価額 {money(sig['model_equity'], sig['currency'])}\n"
            + (" / ".join(items) if items else "保有なし"))


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """長い本文を行単位で分割する（1行が長すぎる場合は切る）。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send(messages: list[str], webhook_url: str | None = None, post=requests.post, sleep=time.sleep) -> bool:
    """Discord に送る。URL が無ければ標準出力に出すだけ（False を返す）。"""
    url = (webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()
    chunks = [c for m in messages for c in split_message(m)]
    if not url:
        print("\n\n".join(chunks))
        return False
    for chunk in chunks:
        resp = post(url, params={"wait": "true"}, json={"content": chunk}, timeout=30)
        if resp.status_code == 429:  # レート制限: 少し待って1回だけやり直す
            sleep(5)
            resp = post(url, params={"wait": "true"}, json={"content": chunk}, timeout=30)
        if resp.status_code >= 300:
            # URL（秘密）をログに出さないよう、ステータスだけ報告する
            raise RuntimeError(f"Discord への送信に失敗しました: HTTP {resp.status_code}")
        sleep(0.5)
    return True
