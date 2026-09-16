# プロジェクト現状ステータス（最新）

> このファイルが「いま何が実装済みで、何が残っているか」の最新情報源です。
> 設計方針は [`Stock_app/IMPROVEMENT_PLAN.md`](IMPROVEMENT_PLAN.md) と [`back_tester/BACKTEST_STRATEGY_PLAN.md`](../back_tester/BACKTEST_STRATEGY_PLAN.md) を参照。
> 新しいセッションを始める際は、このファイル＋設計書2本を読めば全体像を把握できます。

---

## 1. リポジトリ構成（重要）

- **2つの独立したGitリポジトリ**がある（単一リポジトリではない）:
  - `Stock_app`（スクリーナー本体・フロント・AI）
  - `back_tester`（バックテスト）
- 共通モジュール `common/` と `strategy_params.json` は**両リポジトリに複製**して自己完結させている（当面手動同期）。将来は共有パッケージ化 or 統合を検討。

---

## 2. 実装済み（完了）

### Stock_app 側
- **共有パラメータ**: [`docs/strategy_params.json`](docs/strategy_params.json)（株価帯・シグナル閾値・警告・スコア重み・`ai` 設定。`ai.enabled=true`）。
- **共通モジュール**: [`common/config.py`](common/config.py)、[`common/features.py`](common/features.py)
  - GC日数・5日平均代金・売買代金増加率・SMA・ATR14・週足/月足トレンド・支持/抵抗・上髭/下髭・レンジ位置・総合スコア・警告判定。
- **新スクリーナー [`main7.py`](main7.py)**:
  - Stage1: OHLCVのみで総合スコア（週足25/GC20/増加率20/代金15/SMA200 20）。週足悪化は除外。適応プール20〜40銘柄。
  - ファンダメンタルはプール分のみ取得。
  - Stage2 AI: プール全銘柄を1位から順位づけし、`overall`＋銘柄別 `rank/verdict/reason/news_note/entry_strategy/entry_price/support/resistance/tp/sl/trailing_plan` をJSONで返す（Gemini / DeepSeek を `ai.provider` で切替）。
  - ニュース: yfinance `.news` で見出し取得（AI実行時のみ・プール分のみ・0.25秒間隔）。
  - 出力: `docs/history7/{date}.json`・`latest.json`、`recommendations.json`（AI）／`recommendations_technical.json`（技術）、`ai_analysis/{date}.json`、`ai_strategy_latest.json`。
  - フラグ: `--ai`（AI実行）、`--force-ai`（手動で強制上書き）。
- **フロント**:
  - [`docs/main7.html`](docs/main7.html): AI総評・おすすめ表（順位/エントリー/利確/損切）・銘柄モーダル（詳細戦略）・プロンプトコピー。
  - [`docs/journal.html`](docs/journal.html): 各取引行の「AI戦略」ボタンで最新AI戦略を表示。
  - [`docs/index.html`](docs/index.html): main6表示（無変更）＋main7へのリンク追加。
- **ワークフロー**:
  - [`daily_main7.yml`](.github/workflows/daily_main7.yml): 技術スクリーニング 平日1日5回。
  - [`daily_main7_ai.yml`](.github/workflows/daily_main7_ai.yml): **AI実行 20:17 JST**。手動実行時は `force` チェックで上書き可能。
  - [`daily_stock.yml`](.github/workflows/daily_stock.yml): main6（無変更のまま稼働）。
- **AI（Gemini）**: 環境変数 `GEMINI_API_KEY`（GitHub Secrets）で動作。プロバイダ/モデルは `strategy_params.json` の `ai` セクションで切替（既定 `gemini` / `gemini-2.5-flash`）。DeepSeek を使う場合は `DEEPSEEK_API_KEY`。

### back_tester 側
- [`backtest_rolling_walkforward.py`](../back_tester/backtest_rolling_walkforward.py): ウォークフォワード（学習12ヶ月/検証3ヶ月/スライド3ヶ月、2022〜2024）。
  - エグジット: 固定TP/SL vs ATRトレーリングの比較。
  - リスク指標（最大DD・連敗・最大損失）＋ベンチマーク（等加重バイ&ホールド）。
  - 出力: `results/backtest_walkforward_result.json`・`.csv`・`_report.md`、GitHub Step Summary。
- [`run_walkforward.yml`](../back_tester/.github/workflows/run_walkforward.yml): **土曜 21:00 JST** に週次実行＋結果コミット。

---

## 3. 未完了タスク（残り）

| # | 内容 | 状態 |
| --- | --- | --- |
| 5 | 株価帯定義を全スクリプト・UI・Discordで統一 | 未着手 |
| 6 | main6/index.html 側のハードコード閾値の strategy_params.json 一本化 | 未着手（main6は旧式のまま） |
| 10 | バックテストに業種・相場レジームのセグメントを追加 | 未着手 |
| 12 | トレンドフィルタ・相対力の追加（ATR/トレールは済み） | 一部実施 |
| 13 | スリッページ感度・サバイバーシップ注記・決算日除外（ベンチマークは済み） | 一部実施 |
| 14 | ポジションサイジング・同時保有・業種集中上限・最大DD制御 | 未着手 |
| 15 | バックテスト実行時間対策（段階探索・事前計算キャッシュ・ランナー選定） | 未着手 |
| 16 | ジャーナルのエントリー時特徴量スナップショット＋サーバー側/JSON永続化 | 未着手（現在 localStorage） |
| 17 | 再最適化ワークフロー（自動ガード付き strategy_params.json 更新） | **保留**（依頼により後回し） |
| 19 | AIコスト管理（月間予算ガード・トークン計測） | 一部実施（1日1回＋max_callsのみ） |

---

## 4. 既知の制約・注意

- **画像入力なし**: AIはチャート画像を見ない。ローソク足の特徴はOHLC由来の数値（上髭/下髭/支持抵抗など）で近似。
- **ライブニュース**: yfinance `.news`（Yahoo由来）で取得。日本株・小型株は網羅度が低く、無ければ「要確認」表記。有料ニュースAPIへの差し替え余地あり。
- **DeepSeek は Web検索不可**（純LLM）。最新ニュースは自前取得して渡す方式。
- **Python 実行**: ローカルは `uv run --no-project --python 3.11 python <file>` で実行（システムPython未導入）。

---

## 5. 実行方法（クイック）

```bash
# スクリーナー（技術のみ）
uv run --no-project --python 3.11 python Stock_app/main7.py

# スクリーナー（AI分析）
uv run --no-project --python 3.11 python Stock_app/main7.py --ai

# スクリーナー（AIを強制再分析・上書き）
uv run --no-project --python 3.11 python Stock_app/main7.py --ai --force-ai

# バックテスト
uv run --no-project --python 3.11 python back_tester/backtest_rolling_walkforward.py
```

---

## 6. 主要な成果物・閲覧先

- main7 ビューア: `https://mrkm3845-web.github.io/Stock_app/main7.html`
- main6 ビューア: `https://mrkm3845-web.github.io/Stock_app/`（または `index.html`）
- AI戦略インデックス: [`docs/ai_strategy_latest.json`](docs/ai_strategy_latest.json)
- バックテスト結果: [`back_tester/results/backtest_walkforward_report.md`](../back_tester/results/backtest_walkforward_report.md)
