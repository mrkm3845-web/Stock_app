# CHANGELOG（変更履歴）

> スクリーナー・バックテスト・スコア・AIプロンプト・パラメータ・ワークフロー・研究スクリプトの
> **挙動・仕様・パラメータ**の変更を、**日付（JST）ごと**に記録します。新しいものが上です。
> botの自動データ更新（`[skip ci]`）や誤字修正は含めません。
> 記録項目: **commit / 種別 / 変更 / 根拠・検証 / 影響・ロールバック / 関連**。
> 2026-10以前の概要は [`PROJECT_STATUS.md`](PROJECT_STATUS.md) の「12. 主要な変更履歴（概要）」を参照。

---

## 2026-10-18

### 価格レンジ表示を全銘柄化し、ソート固定を解除（この変更）
- **commit**: この変更（レンジ表示の全銘柄化）
- **種別**: UI / スクリーナー
- **変更**:
  - `docs/index.html` の「¥3,000未満」表示を、レンジAIの「推奨＋様子見」だけではなく**価格帯内の全銘柄**に変更。AI判定はレンジAIの評価済み銘柄だけ参考表示する。
  - レンジ表示中に `aiRankOf` を無条件で先に適用していた**強制AI順ソートを削除**。全体/レンジとも「スコア順」「AI推奨順」など選択中のソートを常に尊重し、既定はスコア順。
  - ボタン・注記・AI推奨0件時の文言を「全銘柄表示 / レンジAI評価は参考」に統一。
  - `docs/guide.html` に母集団トグルの意味、スコア順（検証済み）とAI推奨順（参考）の使い分けを追記。
  - `PROJECT_STATUS.md` のフロント仕様（レンジ表示・AI判定フィルタ・`price_filter`）を実装に合わせて更新。
  - `AGENTS.md` に「ツール変更時は関連資料も同じ変更でメンテする」ルールを明記。
- **根拠・検証**: 強制ソートは `docs/index.html` の `isRangeView()` 分岐が原因で、スコア順を選んでも順位が変わらない状態だった。ローカル表示（Edgeヘッドレス）で、`STOCKS=2,906` / 「¥3,000未満」スコア絞り込みなし=961件 / 既定スコア70以上=157件を確認。レンジ表示でAI未評価の銘柄（例: 1379）も表示され、スコア降順=true、AI推奨順の先頭=7272（レンジAI 1位）を確認。
- **影響・ロールバック**: レンジ表示の件数が約10件から価格帯内の全銘柄（既定フィルタ適用時で961件）に増える。AI判定は評価済み銘柄のみ表示。戻す場合は本変更を `git revert`。
- **関連**: `docs/index.html` / `docs/guide.html` / `PROJECT_STATUS.md` / `AGENTS.md`

### 週末作戦ノートページ（この変更）
- **commit**: この変更（週末ノート追加）
- **種別**: ドキュメント / UI
- **変更**:
  - `docs/weekend.html` を追加。`docs/weekend/index.json` と `docs/weekend/{date}.json` を読み込み、週末の分析・来週の作戦・リスク管理をスマホで表示する。
  - 初回ノートとして `docs/weekend/2026-10-18.json` を追加（10-16基準のW43作戦）。
  - `docs/index.html` と `docs/journal.html` のヘッダーに「週末作戦ノート」リンクを追加。
- **根拠・検証**: JSONの構文チェックと、ページの読み込み経路（`weekend/index.json` → `weekend/{date}.json`）を確認。
- **影響・ロールバック**: 既存ページの挙動には影響なし。戻す場合は `docs/weekend.html`・`docs/weekend/`・各ヘッダーのリンクを削除。
- **関連**: `docs/weekend.html` / `docs/weekend/*` / `docs/index.html` / `docs/journal.html`

### AGENTS.mdをやさしい表現に刷新（この変更）
- **commit**: この変更（AGENTS刷新）
- **種別**: ドキュメント / 運用
- **変更**:
  - 必須ルール（日本語のみ・同期・CHANGELOG・ワークフロー・個人データ非公開）は維持しつつ、話し方・手順・週末ノートの書き方を平易な日本語に整理。
  - ペルソナを「現場で回している金融分析のプロ」に調整し、結論先出し・用語解説・反証併記・行動レベルの提案を明記。
  - 週末ノートの追加手順（`docs/weekend/`）を追記。
- **根拠・検証**: 既存の必須ルールが残っていることを確認。
- **影響・ロールバック**: 運用ルールの表現変更のみ。`git revert` で戻せる。
- **関連**: `AGENTS.md`

### AIプロンプト: 型サマリ＋過去検証メモ（e45d111）
- **commit**: e45d111
- **種別**: AIプロンプト / スクリーナー
- **変更**:
  - 直近3日以内の型（GC(20,60) / DC(5,25) / DC(20,60) / ダブルボトム / レンジ / カップ / フラッグ）を `pattern_summary` として候補に付与し、プロンプトに `型:` 行を追加。
  - システムプロンプト（日次・週次プラン）に**過去検証メモ**を追加（GC(20,60)直後・ダブルボトム・フラッグは平均劣後、DC直後・押し目は相対良好／参考程度）。
  - `PROMPT_VERSION = 2026-10-18-1`。
- **根拠・検証**: `research_patterns.py` / `research_upside.py` の結果。プロンプト生成テストで型行・メモ・版更新を確認。
- **影響・ロールバック**: 次回AI実行から新プロンプト。戻す場合は `git revert e45d111`。
- **関連**: `common/patterns.py` / `main8.py` / `back_tester/weekly_review.py` / `common/version.py`

### スコアv3（3edc76c）
- **commit**: 3edc76c
- **種別**: スコア / パラメータ
- **変更**:
  - 重み: `sma200_slope 30 / pos_52w 25 / liquidity 20 / dist_sma200 15 / ret_120d 10`。
  - `dist_sma200` は +20%で満点・**+40%超は減衰**、`ret_120d` は +60%で満点・**+120%超は減衰**（行き過ぎの減点）。
  - 式を `common/features.score_series_from_feat` に一本化（スクリーナー/BT共有）。
- **根拠・検証**: `back_tester/validate_score.py`（OOS）。5日 上位5期待値 **+0.27% → +0.66%**、大負け率 **13.9% → 8.5%**。20日 **1.27% → 3.23%**、大負け **13.2% → 5.2%**。学習期(〜2024)/検証期(2025〜)の両方で改善。
- **影響・ロールバック**: 候補が大型・高流動性寄りになり、宝くじ的な小型の大勝ちは減る。戻す場合は `git revert 3edc76c`（研究・タグも同時に戻る点に注意）。
- **関連**: `common/features.py` / `back_tester/backtest_rolling_walkforward.py` / `docs/strategy_params.json`

### 研究基盤: 型・右裾・スコア検証（3edc76c）
- **commit**: 3edc76c
- **種別**: バックテスト / 研究
- **変更**:
  - `common/patterns.py`（型の機械検出・先読み排除）。
  - `back_tester/research_patterns.py`（型のイベントスタディ）。
  - `back_tester/research_upside.py`（右裾＋上位K期待値。**オフライン既定・進捗バー・ディスクキャッシュ・`--max-minutes`**）。
  - `back_tester/validate_score.py`（v2 vs v3 の高速比較）。
- **根拠・検証**: 型はGC(20,60)/ダブルボトム/フラッグが平均劣後、DC/カップ/押し目継続が相対良好。右裾は `sma200_slope` と流動性が頑健、`atr_pct` はリフト大だが期待値マイナス。
- **影響・ロールバック**: 研究専用（本番スコア・paramsへの自動反映なし）。不要ならファイル削除で戻せる。
- **関連**: `back_tester/results/research_patterns.*` / `research_upside.*`

### 実験タグ: score_version / experiment_id（3edc76c）
- **commit**: 3edc76c
- **種別**: スコア / 週次答え合わせ
- **変更**:
  - `common/version.py` に `SCORE_VERSION = v3-research_upside-2026-10-18` / `SCORE_EXPERIMENT = research_upside_v3_2026-10`。
  - `docs/picks/{date}.json`・`docs/history/meta.json`・`docs/recommendations*.json`・週次JSON/MD・`weekly.html` に刻印。
- **根拠・検証**: 10-16のAI手動実行とW42レビュー再生成で、タグがJSON・MD・画面に出ることを確認（データ更新コミットは `5b2ade0` / `4e44700`）。
- **影響・ロールバック**: 週次で「どの週がv3か」を追跡可能。不要なら表示のみ外せる。
- **関連**: `main8.py` / `back_tester/weekly_review.py` / `docs/weekly.html`

### AIワークフローに date 入力（330ff1b）
- **commit**: 330ff1b
- **種別**: ワークフロー / 運用
- **変更**:
  - `daily_main8_ai.yml` の `workflow_dispatch` に `date`（YYYY-MM-DD）入力を追加。
  - `main8.py` に `--offline` / `--date`（テスト用・既定OFF）を追加。
- **根拠・検証**: 非営業日でも直近取引日を指定してAIを手動実行できる。10-16指定で実行成功。
- **影響・ロールバック**: 既定は空欄/従来通り。戻す場合は `git revert 330ff1b`（main8側は 3edc76c に含まれる）。
- **関連**: `.github/workflows/daily_main8_ai.yml` / `main8.py`

### ドキュメント整理（e45d111）
- **commit**: e45d111
- **種別**: ドキュメント
- **変更**: `PROJECT_STATUS.md` 冒頭に現状サマリ、`IMPROVEMENT_PLAN.md` に現行差分、`AGENTS.md` に必読順、`back_tester/README.md` に研究スクリプトを追記。
- **関連**: `AGENTS.md` / `PROJECT_STATUS.md` / `IMPROVEMENT_PLAN.md` / `back_tester/README.md`

## 2026-10-15

### AGENTS: 分析依頼時のリモート同期ルール（6bd806a）
- **commit**: 6bd806a
- **種別**: 運用 / ドキュメント
- **変更**: 分析・推奨・答え合わせ等では、内容判断で `git fetch origin main` → 必要なら `git pull --rebase --autostash` し、参照時点を明示するルールを追加。
- **影響・ロールバック**: 運用ルールのみ。`git revert 6bd806a`。

## 2026-10以前（概要）

日付付きの詳細は残っていないため、概要のみ [`PROJECT_STATUS.md`](PROJECT_STATUS.md) の「12. 主要な変更履歴（概要）」に集約しています。主なもの:
- 単一リポジトリ統合、Gemini→DeepSeek切替、ニュース源（Google News RSS）
- スコア再設計（Phase 0→1→2）
- 高値掴み防止（`entry_guard` / `entry_type`）
- 週次答え合わせ（OCO・持ち越し採点・`weekly_feedback`・来週の作戦）
- 履歴gzip化、価格レンジ第2推奨、フロント再設計

---

## 書き方（テンプレート）

```md
## YYYY-MM-DD

### 変更の要約（commit）
- **commit**: xxxxxxx
- **種別**: スコア / スクリーナー / バックテスト / プロンプト / パラメータ / ワークフロー / 研究 / ドキュメント
- **変更**: 何をどう変えたか（数値・閾値・式・ファイル）
- **根拠・検証**: OOS結果・検証コマンド・サンプル数
- **影響・ロールバック**: 期待される影響、戻し方（git revert 等）
- **関連**: 主要ファイル
```
