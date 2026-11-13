# 引き継ぎ書（Claude Code 用）

このリポジトリで作業する AI への指示。まず README.md を読むこと。

## 開発者と進め方

- 開発者は株式投資の知識がほぼない。実装しながら投資の概念も学びたい。
- 各ステップで「何を・なぜ」実装したかを日本語で短く説明し、関わる投資の概念も説明する。
- 一度に大量のファイルを作らない。マイルストーンごとに区切り、完了条件を満たしたら止まって確認を求める。
- 開発者が自分で書きたいと言った部分は、仕様とヒントだけ出してコードは書かない。
- 開発環境は Windows、Python 3.11+、requests / pandas / SQLite / python-dotenv / pytest。

## 絶対に守ること

- 証券会社への発注・ログイン・スクレイピングのコードは書かない。
- API キーは `.env` に置き、コード・ログ・コミットに含めない（`.gitignore` に `.env` と `data/`）。
- 先読みバイアスを避ける: ある日のシグナルは、その日の終値までに分かる情報だけで作る。
- API のフィールド名やエンドポイントは推測で書かない。公式リファレンスか実レスポンスで確認する。
- バックテストの検証用期間ではパラメータを調整しない。日本と米国は別々に評価する。

## 現状（2026-11-13）

M1〜M6 実装済み、pytest 41件成功。GitHub: juza-w/jquants_auto（main、記録は state ブランチ）。
M4〜M6 も GitHub Actions 上で実API（Claude・Web検索・Discord）の動作確認済み（2026-11-13、基準日 2026-11-09 の日本株5件）。構成:

- `stockalert/sources/base.py` DataSource インターフェースと共通列
- `stockalert/sources/jquants.py` 日本株 / `yfinance_source.py` 米国株（将来 moomoo に差し替え）
- `stockalert/db.py` SQLite upsert / `updater.py` 増分更新と分割検知 / `update.py` 実行入口
- `stockalert/indicators.py` 指標 / `strategy.py` 戦略のルール（現在 v2）/ `backtest.py` 検証 / `scripts/grid_search.py` 作る用期間だけの比較
- `stockalert/signals.py` 今日のシグナル（バックテストのエンジンを最新日まで走らせ、残った注文を使う）
- `stockalert/judge.py` Claude 判断層 / `notify.py` Discord / `proposals.py` 記録 / `run.py` 毎日の実行入口
- `.github/workflows/daily-alert.yml` 定期実行。Secrets: JQUANTS_API_KEY, ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL
- `watchlist.txt` 日本 TOPIX100 + 米国50 + ベンチマーク（1306.JP, SPY.US）

## 確認済みの事実

- J-Quants V2 `GET https://api.jquants.com/v2/equities/bars/daily`、ヘッダー `x-api-key`。
  パラメータ `code` / `date` / `from` / `to` / `pagination_key`。レスポンス `{"data": [...], "pagination_key"}`。
  行のキー: `Date, Code(5桁), O, H, L, C, UL, LL, Vo, Va, AdjFactor, AdjO, AdjH, AdjL, AdjC, AdjVo, MktCap, ExRT`。
  取引なしの日は四本値・Vo・Va が揃って null。調整は分割・併合・ライツイシューのみ（配当なし）。
- プラン対象外の期間を指定すると 400 で `Your subscription covers the following dates: A ~ B`。
- 開発者は Light プラン（5年・遅延なし・60req/分、TOPIX指数あり）。株価は毎営業日 16:30 頃更新。
  429 には Retry-After がなく、1〜2分待つのが公式推奨。
- `GET /v2/equities/master` の行: `Date, Code, CoName, CoNameEn, S17, S17Nm, S33, S33Nm, ScaleCat, Mkt, MktNm, Mrgn, MrgnNm, ProdCat`。
- 公式リファレンス: `https://jpx-jquants.com/ja/spec/<ページ名>`（eq-bars-daily, rate-limits, pagination, data-update, data-spec, mkt-cal, idx-bars-daily-topix）。
- Discord Execute Webhook: POST <URL>?wait=true、JSON `{"content": ...}`、2000文字まで（公式ドキュメントで確認）。
- Claude API（anthropic SDK 2.8 の型で確認）: `client.beta.messages.create(model="claude-opus-5-5", betas=["server-side-fallback-2026-07-01"],
  fallbacks="default", output_config={"effort":..., "format": {"type":"json_schema","schema":...}})`、
  Web検索は `tools=[{"type":"web_search_20260209","name":"web_search","max_uses":3}]`。実APIで動作確認済み。
  クラウドの開発環境からは Claude API を呼べない（キーを渡せない）ので、確認は GitHub Actions で行う。
- yfinance 1.8 `Ticker.history(auto_adjust=False, actions=True)`: 列 `Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits`、
  インデックスは America/New_York。Close は分割調整済み。

## 決めたこと

- `adj_close` は分割のみ調整（配当なし）で日米を統一。
- 分割検知は取得元に依存しない方法（取り直した範囲の adj_close が保存値と0.1%超ずれたら全取得）。
- 売買ルールは `strategy.py` に集約し、バックテストと本番シグナルで同じ関数を使う。
- 買い条件に「終値 >= 手仕舞い用の移動平均」を追加（手仕舞い条件に既に当たる銘柄を買わないため）。
- 戦略は開発者から一任され v2 に変更（SMA200で手仕舞い、上位10で買い30位まで保有、10銘柄）。
  検証用期間は v2 で1回使用済み。**これ以上、検証用期間を見ながらルールを変えない。**
  結果: 日本は検証用でBM未満（不採用）、米国は僅差でBM超え（生存者バイアスあり）。
- バックテストは終値で判断 → 翌営業日の始値で約定。コスト・資金は `backtest.py` の `MARKETS`（仮定値）。

## 未解決

1. 秘密情報の扱い: リポジトリは public。エラー本文をログ・通知・記録に出さない（過去に API キーの断片を
   出してしまった。`state` ブランチの過去コミット fcf6b4f に断片が残るが、そのキーは無効化済みの想定）。
   Secret にはキーの文字列1行だけを登録する。
2. ベンチマークに勝てると検証できた戦略がない（README）。v2 は判断日を1日ずらすだけで検証結果が崩れた。
   次の改良は proposals の実績を見てから。検証用期間をこれ以上使わない。
3. 実際の資金額と証券会社の手数料（`backtest.py` の `MARKETS` は仮定値）。
4. 通知は開発者の実際の保有を知らない（モデルポートフォリオ基準）。実保有を入力する仕組みは未実装。
5. 入れ替え判断日を「週の最初の営業日」に変更済み（本番で翌営業日を知らずに判定するため）。
6. 初回の実運用テストでは Claude の判断が5件すべて「見送り」（決算直前・短期の勢い低下が理由）。
   慎重すぎるかどうかは proposals の実績（見送った銘柄のその後）で判断する。
