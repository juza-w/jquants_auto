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
DISCLAIMER = "※ 過去検証で優位性が確認できていないルールの参考情報です。発注は自分の判断で。"


def money(v, currency: str) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}円" if currency == "JPY" else f"${v:,.2f}"


def format_header(sig: dict) -> str:
    bm = sig["benchmark"]
    trend = "SMA200より上（上昇基調）" if bm["above_sma200"] else "SMA200より下（下落基調）"
    lines = [
        f"**【{sig['market']}】{sig['date']} 終値時点のシグナル**",
        f"買い {len(sig['buys'])}件 / 売り {len(sig['sells'])}件 / 本日の損切り到達 {len(sig['stopped_today'])}件",
        f"市場全体（{bm['symbol']}）: {trend}、3か月 {bm['ret_3m']:+.1%}",
        DISCLAIMER,
    ]
    for s in sig["stopped_today"]:
        lines.append(f"・損切り到達: {s['name']}（{s['symbol']}）損益 {s['pnl_pct']:+.1%}")
    return "\n".join(lines)


def format_proposal(sig_item: dict, kind: str, proposal: dict | None, currency: str, error: str | None = None) -> str:
    head = f"**{'🟢 買い' if kind == 'buy' else '🔴 売り'}シグナル: {sig_item['name']}（{sig_item['symbol']}）**"
    if kind == "buy":
        rule = (f"ルール: モメンタム順位 {sig_item['rank']}位 / 終値 {money(sig_item['close'], currency)} / "
                f"目安 {sig_item['shares']:,.0f}株 / 損切り目安 {money(sig_item['stop_ref'], currency)}"
                f"（翌営業日の始値で買う想定）")
    else:
        rule = (f"ルール: {sig_item['reason_text']} / 終値 {money(sig_item['close'], currency)} / "
                f"モデル上の損益 {sig_item['pnl_pct']:+.1%}（{sig_item['entry_date']} 取得）")
    lines = [head, rule]
    if proposal is None:
        lines.append(f"Claude の判断案: なし（{error or 'Claude 判断は無効'}）")
        return "\n".join(lines)
    lines.append(f"**Claude の判断案: {ACTION_JA[proposal['action']]}**（確信度 {proposal['confidence']:.0%}）")
    lines += [f"根拠: " + " / ".join(proposal["reasons"])]
    lines += [f"懸念: " + " / ".join(proposal["risks"])]
    lines.append(f"エントリー {money(proposal['entry'], currency)} / 損切り {money(proposal['stop_loss'], currency)} / "
                 f"利確 {money(proposal['take_profit'], currency)}")
    lines.append(f"判断が誤りと分かる条件: {proposal['invalidation']}")
    for n in proposal["news"][:3]:
        lines.append(f"📰 {n['title']} — {n['impact']} <{n['url']}>")
    return "\n".join(lines)


def format_holdings(sig: dict) -> str:
    lines = [f"**モデルポートフォリオ（{sig['model_start']} 開始の仮想運用。実際の保有とは別）**",
             f"評価額 {money(sig['model_equity'], sig['currency'])}（開始時 {money(sig['model_initial_cash'], sig['currency'])}）"]
    for h in sig["holdings"]:
        mark = " ←売り予定" if h["sell_pending"] else ""
        lines.append(f"・{h['name']}（{h['symbol']}）{h['pnl_pct']:+.1%} 損切り {money(h['stop'], sig['currency'])}{mark}")
    if not sig["holdings"]:
        lines.append("・保有なし")
    return "\n".join(lines)


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
    url = webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "")
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
