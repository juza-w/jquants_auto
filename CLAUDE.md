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

M1〜M3 実装済み、pytest 32件成功。GitHub: juza-w/jquants_auto（main）。M4〜M6 未着手。構成:

- `stockalert/sources/base.py` DataSource インターフェースと共通列
- `stockalert/sources/jquants.py` 日本株 / `yfinance_source.py` 米国株（将来 moomoo に差し替え）
- `stockalert/db.py` SQLite upsert / `updater.py` 増分更新と分割検知 / `update.py` 実行入口
- `stockalert/indicators.py` 指標 / `strategy.py` 戦略のルール（現在 v2）/ `backtest.py` 検証 / `scripts/grid_search.py` 作る用期間だけの比較
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

## 未解決（開発者に確認が必要）

1. 日本株は v2 も不採用相当。通知は米国株だけにするか、日本株は「参考」として出すか。
2. 実際の資金額と証券会社の手数料（バックテストの仮定値を直す）。
3. 通知先（仕様は Discord webhook。メール・LINE の希望もあり）。
4. どこで定期実行するか（Windows のタスクスケジューラ / GitHub Actions など）。

## 残作業

- M4: シグナルが出た銘柄だけ Claude API に渡し、JSON
  `symbol, action(buy/sell/hold), reasons[], risks[], entry, stop_loss, take_profit, invalidation, confidence`
  を返させる。根拠は渡したデータからのみ引用させる。API の仕様は推測せず確認する。キーは `.env` の `ANTHROPIC_API_KEY`。
- M5: Discord webhook 通知。全判断案とその後の値動きを `proposals` テーブルに記録。
- M6: 日本株は東証の引け後（J-Quants 更新の16:30以降）、米国株は NY の引け後。取引所の現地時間基準
  （夏時間の切替日は国で違う）。
