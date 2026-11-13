"""毎日の実行入口（M6 から呼ばれる）:  python -m stockalert.run --market JP

流れ: データ更新 → シグナル → Claude 判断案 → 記録 → 通知 → 過去の判断案のその後を更新
同じ基準日について2回通知しない（休場日や再実行で重複させない）。
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from . import db, judge, notify, proposals
from .signals import compute_signals
from .sources.base import split_symbol
from .update import DB_PATH, ROOT, WATCHLIST, build_sources
from .updater import update_all

STATE_PATH = ROOT / "state" / "alerts.db"
MAX_FAILED_RATIO = 0.2  # これより多くの銘柄で取得に失敗したら、信頼できないので通知せず止める


def load_names(path) -> dict[str, str]:
    """watchlist.txt から 銘柄 -> 表示名（# の後ろのコメント。なければ銘柄コード）。"""
    names = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        code, _, comment = line.partition("#")
        code = code.strip()
        if code:
            names[code] = comment.strip() or code.rsplit(".", 1)[0]
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", required=True, choices=["JP", "US"])
    parser.add_argument("--no-update", action="store_true", help="データ更新を飛ばす")
    parser.add_argument("--no-claude", action="store_true", help="Claude 判断を使わない")
    parser.add_argument("--no-news", action="store_true", help="ニュース検索を使わない")
    parser.add_argument("--dry-run", action="store_true", help="通知も記録もせず、内容を表示するだけ")
    parser.add_argument("--as-of", help="この日までのデータでシグナルを作る（動作確認用。YYYY-MM-DD）")
    parser.add_argument("--force", action="store_true", help="通知済みの基準日でももう一度実行する")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    market = args.market
    names = {s: n for s, n in load_names(WATCHLIST).items() if split_symbol(s)[1] == market}
    conn = db.connect(DB_PATH)

    if not args.no_update:
        results = update_all(conn, build_sources({market}), list(names))
        failed = [r for r in results if r.error]
        for r in failed:
            print(f"[NG] {r.symbol}: {r.error}")
        print(f"データ更新: {len(results) - len(failed)}/{len(results)} 銘柄成功")
        if len(failed) > len(results) * MAX_FAILED_RATIO:
            msg = f"⚠️【{market}】データ取得の失敗が多いため本日のシグナルを中止しました（{len(failed)}/{len(results)} 銘柄）"
            if not args.dry_run:
                notify.send([msg])
            print(msg)
            return 1

    sig = compute_signals(conn, market, names, as_of=args.as_of)
    state = proposals.connect(":memory:" if args.dry_run else STATE_PATH)
    if not args.dry_run:
        print(f"その後の値動きを更新: {proposals.update_outcomes(state, conn)} 件")
        if proposals.already_run(state, market, sig["date"]) and not args.force:
            print(f"{market} {sig['date']} は通知済み（休場日または再実行）。何もしません。")
            return 0

    client = None if args.no_claude else judge.make_client()
    use_news = not args.no_news and os.environ.get("NEWS_SEARCH", "1") != "0"
    messages = [notify.format_header(sig)]
    for kind, items in (("sell", sig["sells"]), ("buy", sig["buys"])):
        for item in items:
            proposal, error = None, None
            if client is None:
                error = "Claude 判断は無効（API キー未設定または --no-claude）"
            else:
                try:
                    packet = judge.build_packet(conn, item, kind, sig)
                    news = judge.research_news(client, item["name"], item["symbol"], sig["date"]) if use_news else None
                    proposal = judge.judge(client, packet, news)
                except Exception as exc:  # noqa: BLE001 - Claude が失敗してもルールのシグナルは届ける
                    error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    print(f"[Claude NG] {item['symbol']}: {error}")
            if not args.dry_run:
                proposals.record(state, market, sig["date"], kind, item, proposal,
                                 judge.MODEL if proposal else None, error)
            messages.append(notify.format_proposal(item, kind, proposal, sig["currency"], error))
    messages.append(notify.format_holdings(sig))

    if args.dry_run:
        notify.send(messages, webhook_url="")
        return 0
    sent = notify.send(messages)
    if not sent:
        print("（DISCORD_WEBHOOK_URL が未設定のため、通知は画面表示のみ）")
    proposals.mark_run(state, market, sig["date"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
