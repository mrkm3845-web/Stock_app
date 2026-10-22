# CHANGELOG（変更履歴）

> スクリーナー・バックテスト・スコア・AIプロンプト・パラメータ・ワークフロー・研究スクリプトの
> **挙動・仕様・パラメータ**の変更を、**日付（JST）ごと**に記録します。新しいものが上です。
> botの自動データ更新（`[skip ci]`）や誤字修正は含めません。
> 記録項目: **commit / 種別 / 変更 / 根拠・検証 / 影響・ロールバック / 関連**。
> 2026-10以前の概要は [`PROJECT_STATUS.md`](PROJECT_STATUS.md) の「12. 主要な変更履歴（概要）」を参照。

---

## 2026-10-23

### 運用マニュアル（`docs/ops.html`）を追加（この変更）
- **commit**: この変更（運用マニュアル）
- **種別**: ドキュメント / UI
- **変更**:
  - `docs/ops.html` を追加。「裏で自動で動くこと」と「自分が手動でやること」を1ページに整理。
    - 1日の時刻表（04:17 / 06:17 / 09:00 / 09:05〜15:05 / 15:30 / 17:17 / 19:17 / 20:17 / 21:17 / 土09:00 / 第1土21:00）
    - 自動6本（`daily_main8` / `daily_main8_ai` / `plan_check_intraday` / `weekly_review` / `run_walkforward` / `refresh_data_ai`）の内容・出力・失敗時
    - 手動（朝のOCO確認、売買後の台帳更新、夜・週末の作戦ノート、データ更新、PCコマンド、月次確認）
    - 画面の役割分担（要約／実行／記録）、通知の意味、制約、ファイル早見表、Q&A、用語集
  - `index.html` / `strategy.html` / `weekend.html` / `guide.html` に「運用マニュアル」リンクを追加。
  - `docs/strategy.html` と `docs/guide.html` に「日中の監視は**分析用ではなく執行の見張り番**」「Actionsの遅延で**1〜2時間遅れの後追い検知**」「**通知が来ない≠安全**（OCOが本体）」を明記。
  - `docs/weekend.html` に「このノートは作成時点の記録。最新の水準は今日の作戦で確認」を追記。
  - `PROJECT_STATUS.md` に `ops.html`・日次監視の位置づけ・**11月中旬の効果測定タスク**を追加。`AGENTS.md` のトリガー監視に同じ注意を追加。
- **根拠・検証**:
  - 各ワークフローの `cron` と実行ステップ、`tools/*.py` のCLI引数を実装から確認して記載。
  - GitHub Actions API で scheduled 実行を集計（`daily_main8` n=44）。**遅延は中央値+100分・最大+223分、予定の約6割しか発火していない**ことを確認し、マニュアルの制約に反映。
  - `docs/strategy/events.json` に intraday 由来の検知がまだ0件であることも確認（効果測定の判断材料）。
  - HTMLタグの対応検証と、Chromeヘッドレスでの描画（上部・下部・末尾）を確認。
- **影響・ロールバック**: 挙動変更なし（ページ追加と文言のみ）。cron・スクリプト・台帳は未変更。戻す場合は `docs/ops.html` と各リンク・追記を `git revert`。
- **関連**: `docs/ops.html` / `docs/index.html` / `docs/strategy.html` / `docs/weekend.html` / `docs/guide.html` / `PROJECT_STATUS.md` / `AGENTS.md`

---

## 2026-10-22

### 作戦ノートの銘柄表記を「銘柄名（コード）」に統一（この変更）
- **commit**: この変更（銘柄表記ルール）
- **種別**: ドキュメント / プロンプト
- **変更**:
  - `AGENTS.md` の話し方・言語ルールに「銘柄はコードだけで書かず、必ず『銘柄名（コード）』にする」を追加。
  - `prompts/strategy_note.md` の公開ページルールにも同じ表記ルールを追加。
  - 最新ノート `docs/weekend/2026-10-22-1815.json` と `docs/weekend/index.json` のコードだけの表記を「銘柄名（コード）」へ修正。
- **根拠・検証**: コードだけでは銘柄が分かりにくいとの指摘。JSONの構文検証と表示確認を実施。
- **影響・ロールバック**: 今後の作戦ノート・会話の表記が変わる。台帳の `code` フィールドは従来どおりコードのみ。戻す場合は該当ルールとノート表記を `git revert`。
- **関連**: `AGENTS.md` / `prompts/strategy_note.md` / `docs/weekend/2026-10-22-1815.json` / `docs/weekend/index.json`

### 作戦ノート作成前のデータ鮮度チェック（この変更）
- **commit**: この変更（データ鮮度チェック）
- **種別**: 運用 / ワークフロー / ドキュメント
- **変更**:
  - `tools/refresh_data_if_stale.py` を追加。JPXカレンダーで期待する取引日を求め、`docs/history` の日付・`recommendations.json` の日付・AI価格と最新終値のズレを確認する。
  - 古ければ `main8.py` を実行。AIキーがあれば `--ai --force-ai`、なければテクニカルのみ更新し、AIが古いままなら警告する。
  - `prompts/strategy_note.md` と `.opencode/command/strategy-note.md` の作成前に鮮度チェックを追加。
  - `refresh_data_ai.yml` を追加。スマホからもスクリーナー＋AIを手動更新できる。
  - `docs/strategy.html` に「データ更新（AI）」ボタンを追加。
  - `check_strategy_plan.py` の履歴フォールバック時、`data_asof` に `history` ではなく実際の取引日を入れるよう修正。
- **根拠・検証**: 手動実行は平日夜・週末が中心で、AIが前日価格のままになる問題（例: 8541のAI 3,255 vs 最新 3,455）を防ぐ。`tools/refresh_data_if_stale.py --check` で期待日2026-10-22、履歴2026-10-21、AI古い・価格ズレ3件を検出。
- **影響・ロールバック**: 鮮度チェックと更新に数分かかる場合がある。AIキーがないローカルではテクニカルのみ。戻す場合は追加スクリプト・ワークフロー・プロンプトの手順を `git revert`。
- **関連**: `tools/refresh_data_if_stale.py` / `prompts/strategy_note.md` / `.opencode/command/strategy-note.md` / `.github/workflows/refresh_data_ai.yml` / `docs/strategy.html` / `docs/guide.html` / `AGENTS.md` / `PROJECT_STATUS.md`

### 作戦トリガー監視を60分間隔に変更（この変更）
- **commit**: この変更（60分間隔）
- **種別**: ワークフロー / ドキュメント
- **変更**:
  - `plan_check_intraday.yml` の cron を `*/30 0-6 * * 1-5` から `5 0-6 * * 1-5` に変更。**平日09:05〜15:05 JSTの1日7回**にした。
  - `docs/strategy.html` に「手動チェック」リンクを追加（GitHub Actions の Run workflow へ）。
  - `AGENTS.md` / `PROJECT_STATUS.md` / `docs/guide.html` の表記を60分に統一。
- **根拠・検証**: 30分間隔は細かすぎ、GitHub Actions の遅延もあり、日中の大まかな接近/到達の把握に絞る。ローカルで `tools/check_strategy_plan.py` の動作は確認済み。
- **影響・ロールバック**: 通知は最大60分遅れる。損切りは証券会社のOCOを必ず併用。戻す場合は cron を `*/30 0-6 * * 1-5` に戻す。
- **関連**: `.github/workflows/plan_check_intraday.yml` / `docs/strategy.html` / `AGENTS.md` / `PROJECT_STATUS.md` / `docs/guide.html`

### 作戦台帳・トリガー監視・今日の作戦（この変更）
- **commit**: この変更（作戦台帳とトリガー監視）
- **種別**: 運用 / UI / ワークフロー
- **変更**:
  - `docs/strategy/active.json`（作戦台帳）、`status.json`（現在状態）、`events.json`（トリガー履歴）を追加。保有は**銘柄コードとOCO水準のみ**で、数量・損益額は持たない。
  - `tools/check_strategy_plan.py` を追加。価格トリガー（押し目/ブレイク/損切/利確）、条件トリガー（地合い/セクター/決算）、期限切れを判定し、状態変化だけDiscord通知する。
  - `docs/strategy.html`（今日の作戦）を追加。スクリーナー上部に「今日の作戦」カードと、一覧の「作戦」バッジを追加。
  - `plan_check_intraday.yml` を追加。**平日9:00〜15:30 JSTのみ30分間隔**でチェック（後に60分間隔へ変更）。`daily_main8.yml`（17時台）と `daily_main8_ai.yml`（AI実行後）にも引け後チェックを追加。
  - `prompts/strategy_note.md` と `tools/prepare_strategy_note.py` を追加。opencode以外（Claude Code / Codex / Cursor / Gemini CLI）でも同じ手順で作戦ノート・台帳を作れるようにした。`.opencode/command/strategy-note.md` は汎用プロンプトを読む薄いラッパーに変更。
- **根拠・検証**: `python tools/prepare_strategy_note.py`、`python tools/check_strategy_plan.py --mode eod --force`、`strategy.html` と `index.html` のヘッドレス描画を確認。2026-10-22時点で1518の損切接近（2,192まで0.4%）、4746の買い入口接近（2,000まで1.0%）を検出。
- **影響・ロールバック**: 既存ページの挙動は維持。通知は状態変化のみ。自動発注はしない。戻す場合は追加ファイル・ワークフロー・リンクを `git revert`。
- **関連**: `docs/strategy/*` / `docs/strategy.html` / `tools/check_strategy_plan.py` / `tools/prepare_strategy_note.py` / `prompts/strategy_note.md` / `.github/workflows/plan_check_intraday.yml` / `docs/index.html` / `AGENTS.md`

### 作戦ノートの一発生成（/strategy-note）（この変更）
- **commit**: この変更（作戦ノート自動化）
- **種別**: 運用 / ドキュメント / UI
- **変更**:
  - `.opencode/command/strategy-note.md` を追加。最新の `tradehistory(JP)_*.csv` と市場データから、起動時点の作戦ノートを生成し、`docs/weekend/{timestamp}.json` と `docs/weekend/index.json` を更新して push する。
  - `tools/prepare_strategy_note.ps1` を追加。最新CSVの自動検出、FIFOでの保有・実現損益、地合い・セクター・最新ピック・週次・プランを `tools/out/strategy_context.json` に出力する。
  - 作戦ノートをタイムスタンプ式（`id` / `created_at` / `data_date` / `trade_history_last_date` / `trigger`）に拡張。`docs/weekend.html` は作成日時で切り替え可能。
  - 初回ノートとして `docs/weekend/2026-10-22-1512.json` を追加（10-21基準、保有3銘柄のOCO整理）。
  - `.gitignore` に `tradehistory*.csv` と `tools/out/` を追加。
  - `AGENTS.md` に「あいまいなら必ず質問」「作戦ノートは `/strategy-note`」を追記。
  - `docs/guide.html` に作戦ノートの使い方を追加。
- **根拠・検証**: `powershell -NoProfile -ExecutionPolicy Bypass -File tools/prepare_strategy_note.ps1` で最新CSV（`tradehistory(JP)_20261022.csv`）を読み、保有3銘柄・地合い・最新ピックをJSON出力。JSON構文チェックとローカルHTTP配信を確認。
- **影響・ロールバック**: 既存ページの挙動は維持。戻す場合はコマンド・スクリプト・ページ変更を `git revert`。
- **関連**: `.opencode/command/strategy-note.md` / `tools/prepare_strategy_note.ps1` / `docs/weekend.html` / `docs/weekend/*` / `docs/guide.html` / `AGENTS.md` / `.gitignore`

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
