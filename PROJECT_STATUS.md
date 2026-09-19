# プロジェクト現状ステータス（最新）

> このファイルが「いま何が実装済みで、何が残っているか」の最新情報源です。
> 設計方針は [`Stock_app/IMPROVEMENT_PLAN.md`](IMPROVEMENT_PLAN.md) と [`back_tester/BACKTEST_STRATEGY_PLAN.md`](../back_tester/BACKTEST_STRATEGY_PLAN.md) を参照。
> 新しいセッションを始める際は、このファイル＋設計書2本を読めば全体像を把握できます。

---

## 1. リポジトリ構成（重要）

- **単一リポジトリ（`Stock_app`）に統合済み**。バックテストは [`back_tester/`](back_tester/) 配下にある。
- 共通モジュール `common/` と `strategy_params.json`（`docs/`）は**リポジトリ直下で単一管理**（複製なし）。
- バックテストの結果（`price_tiers` / `signals`）は、ガード通過時に **`docs/strategy_params.json` へ自動反映**され、スクリーナーが次回実行で自動的に読む（手動同期は不要）。
- 旧 `back_tester` リポジトリは**当面バックアップとして残置**（検証後にアーカイブ予定）。

---

## 2. 実装済み（完了）

### Stock_app 側
- **共有パラメータ**: [`docs/strategy_params.json`](docs/strategy_params.json)（株価帯・シグナル閾値・警告・スコア重み・`ai` 設定。`ai.enabled=true`）。
- **共通モジュール**: [`common/config.py`](common/config.py)、[`common/features.py`](common/features.py)
  - GC日数・5日平均代金・売買代金増加率・SMA・ATR14・週足/月足トレンド・支持/抵抗・上髭/下髭・レンジ位置・総合スコア・警告判定。
- **統合スクリーナー [`main8.py`](main8.py)**（main6 + main7 を一本化）:
  - 全銘柄（プライム+スタンダード）を一括スキャンし、**ファンダメンタル網羅（main6）と技術スコア（main7）を同一レコードに統合**。
  - 価格データは増分キャッシュ（`data/price_cache`）で **250日分**取得（SMA200・月足トレンドを機能させスコアを0〜100で計算）。
  - Stage2 AI: 高スコア上位プール（〜40銘柄）を1位から順位づけし、`overall`＋銘柄別 `rank/verdict/reason/news_note/entry_strategy/entry_price/support/resistance/tp/sl/trailing_plan` をJSONで返す（Gemini / DeepSeek を `ai.provider` で切替）。
  - ニュース: yfinance `.news` で見出し取得（AI実行時のみ・プール分のみ）。
  - 出力: `docs/history/{date}.json`・`latest.json`・`meta.json`、`recommendations.json`（AI）／`recommendations_technical.json`（技術）、`ai_analysis/{date}.json`、`ai_strategy_latest.json`、`data/stocks.db`。
  - フラグ: `--ai`（AI実行）、`--force-ai`（手動で強制上書き）、`--max-stocks`（テスト）。
- **フロント**:
  - [`docs/index.html`](docs/index.html): **統合スクリーナー**。デフォルトでスコア70以上絞り込み＋スコア順ソート（同点は増加率順）。銘柄モーダルにAI戦略（上位AI実行銘柄）を表示し、「最新AI実行」でGeminiへ最新データ+ニュース照会プロンプトを転送。
  - [`docs/journal.html`](docs/journal.html): 各取引行の「AI戦略」ボタンで最新AI戦略を表示。
  - 旧 [`docs/main7.html`](docs/main7.html) と旧 [`docs/index.html`](docs/index_main6_backup.html) は退避。
- **ワークフロー**:
  - [`daily_main8.yml`](.github/workflows/daily_main8.yml): 技術スクリーニング 平日1日5回。
  - [`daily_main8_ai.yml`](.github/workflows/daily_main8_ai.yml): **AI実行 20:17 JST**。手動実行時は `force` チェックで上書き可能。
  - [`run_walkforward.yml`](.github/workflows/run_walkforward.yml): **バックテスト 毎月 第1土曜 21:00 JST**。結果をガード付きで `docs/strategy_params.json` へ自動反映。
  - 旧 `daily_stock.yml` / `daily_main7*.yml` は `.disabled` で無効化。
- **AI（Gemini）**: 環境変数 `GEMINI_API_KEY`（GitHub Secrets）で動作。プロバイダ/モデルは `strategy_params.json` の `ai` セクションで切替（既定 `gemini` / `gemini-2.5-flash`）。DeepSeek を使う場合は `DEEPSEEK_API_KEY`。

### back_tester 側
- [`backtest_rolling_walkforward.py`](../back_tester/backtest_rolling_walkforward.py): スコア式再現のローリングウォークフォワード（学習12ヶ月/検証3ヶ月/スライド3ヶ月、`walk_end` は前月末でローリング）。
  - エントリー: スコア上位K銘柄を翌日寄りで約定（実運用 main8.py と同一スコア式）。
  - エグジット: 固定TP/SL vs ATRトレーリングの比較＋**株価帯別エグジット最適化**。
  - 指標: 資金制約（同時保有上限・固定比率）を加味した年率/最大DD/シャープ/PF。ベンチマークは TOPIX ETF（1306.T）。
  - **選定エッジ検証（Phase 0）**: スコア分位分析・上位vsランダムvs下位比較・重み寄与分解（leave-one-out）・ATRトレーリングのテール監査。
  - 出力: `results/backtest_walkforward_result.json`（`quantile_analysis` / `selection_comparison` / `weight_ablation` を含む）・`.csv`・`backtest_tier_exit.csv`・`_report.md`。
- [`apply_optimal_params.py`](../back_tester/apply_optimal_params.py): 結果を**ガード付き**で `strategy_params.json` の `signals`・`price_tiers` に反映（最低件数/PF/ベンチマーク超え/DD上限/近傍安定性/前回比劣化なし）。
- [`run_walkforward.yml`](../back_tester/.github/workflows/run_walkforward.yml): **毎月 第1土曜 21:00 JST** に実行＋ガード反映＋結果コミット。
- 旧スクリプト `backtest_scanner*.py` / `backtest_volume_deepdive.py` は上記に統合・削除済み。

> ⚠️ **スコア再設計（Phase 2・2026-09-19 完了）**:
> - Phase 1 の研究で有効と確認した**トレンド系指標**（52週高値位置 / 200日線乖離 / 200日線傾き / 120日リターン）でスコアを再構成。逆効果だった GC直後・出来高急増・週足上昇を除外した。
> - 再検証の結果、分位スプレッドは **+0.025% → +0.347%**、Spearman は **0.24 → 0.92** に改善。**上位分位がプラス（+0.16%）／下位分位がマイナス（-0.19%）** となり、スコアが順位付けとして機能するようになった。
> - 最良条件は OOS **PF ≥ 1.15・期待値プラス・年率 +32.8%**（ベンチマーク +5.92%）。ただし最大DDが大きく、ガード（DD・近傍安定性）により `signals` は据え置き。
> - 株価帯エグジットは、ガード通過分（`mid`）が `docs/strategy_params.json` へ**自動反映済み**。
> - **残課題**: 最大DDの低減（レジームフィルタ・現金待機）、ランダム選択に対する優位性の明確化、エグジット（ATR/トレール）の再設計。
>
> 経緯（Phase 0/1）: 旧スコアは「上位ほど上がる」関係を示せず、上位選択がランダムに劣後。成分では「200日線より上」以外の寄与が小さく、出来高急増・GC直後・週足上昇は逆効果（分位スプレッドがマイナス）だった。研究結果は [`back_tester/results/research_signals.json`](back_tester/results/research_signals.json)。

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
| 17 | 再最適化ワークフロー（自動ガード付き strategy_params.json 更新） | 完了（signals/price_tiers、ガード付き、月次） |
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
uv run --no-project --python 3.11 --with pandas --with numpy --with requests --with yfinance --with openpyxl --with xlrd python Stock_app/main8.py

# スクリーナー（AI分析）
uv run --no-project --python 3.11 --with pandas --with numpy --with requests --with yfinance --with openpyxl --with xlrd python Stock_app/main8.py --ai

# スクリーナー（AIを強制再分析・上書き）
uv run --no-project --python 3.11 --with pandas --with numpy --with requests --with yfinance --with openpyxl --with xlrd python Stock_app/main8.py --ai --force-ai

# バックテスト
uv run --no-project --python 3.11 python back_tester/backtest_rolling_walkforward.py
```

---

## 6. 主要な成果物・閲覧先

- 統合ビューア: `https://mrkm3845-web.github.io/Stock_app/`（`index.html`）
- AI戦略インデックス: [`docs/ai_strategy_latest.json`](docs/ai_strategy_latest.json)
- バックテスト結果: [`back_tester/results/backtest_walkforward_report.md`](../back_tester/results/backtest_walkforward_report.md)
