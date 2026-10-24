# CHANGELOG（変更履歴）

> スクリーナー・バックテスト・スコア・AIプロンプト・パラメータ・ワークフロー・研究スクリプトの
> **挙動・仕様・パラメータ**の変更を、**日付（JST）ごと**に記録します。新しいものが上です。
> botの自動データ更新（`[skip ci]`）や誤字修正は含めません。
> 記録項目: **commit / 種別 / 変更 / 根拠・検証 / 影響・ロールバック / 関連**。
> 2026-10以前の概要は [`PROJECT_STATUS.md`](PROJECT_STATUS.md) の「12. 主要な変更履歴（概要）」を参照。

---

## 2026-10-24

### 3銘柄固定・DDブレーキ・保有期間40日の選択肢を追加（この変更）
- **commit**: この変更（DDブレーキとリスク上限）
- **種別**: 運用 / バックテスト / UI / ドキュメント
- **変更**:
  - `docs/strategy/active.json`: `policy.risk_per_trade_pct` を **1.0 → 0.8**、`max_risk_per_trade_jpy: 5000` を追加。`rules` に**DDブレーキ**を追加（「ピークから-10%で新規停止／-15%で全現金化（60万なら-6万/-9万）」「再開はピーク回復後、または2週間後に地合いが買いやすいへ戻ってから。再開時は半分のサイズ」「1取引のリスクは5,000円以内」。既存の「1万円以内」も5,000円に修正）。**銘柄数は3のまま**。
  - `docs/strategy.html`: ヘッダーに**DDチェックの1行**を追加（-10%で新規停止／-15%で全現金化／1取引のリスク上限）。
  - `back_tester/backtest_rolling_walkforward.py`: `TIER_EXIT_GRID` の `max_hold_days` に **40** を追加（7/14/20/40）。次回の月次で株価帯ごとに選ばせる。
  - `back_tester/weekly_review.py`: `compute_weekly_feedback` が **`plan.json`（来週の作戦）と `ledger.json` を週として読んでいた**のを修正し、週の重複も除去。これにより来週プランが直近N週の窓を1週分消費していた問題と、**W43が2回**出る問題を解消。
- **根拠・検証**:
  - 保有期間の比較（4,495取引・15フォールド・fixed）: 5日 **-2.5%/年**、10日 +9.0%、20日 +12.4%、**40日 +15.1%（DD52.6%）**、60日 +15.1%。週次はマイナスで、40日でほぼ頭打ち。
  - 同時保有数: 3銘柄 **-14.6%/年・DD62%**、4銘柄 -0.6%・56%、5銘柄 +8.9%・53%、8銘柄 +26.4%・43%。資金（60〜90万）を踏まえ**3銘柄固定**とし、代わりに**1取引リスクを5,000円へ引き下げ、DDブレーキを追加**。
  - DDの実額: 60万で-10%＝-6万、-15%＝-9万、-50%＝-30万。50%DDの回復には+100%必要。
  - フィードバック再計算（W38〜W43・90件）: baseline **-5.20%**、補正は low **-0.72** / moderate **+2.32**（W43が二重に数えられ、W38が窓から落ちていた状態を修正）。
- **影響・ロールバック**:
  - 新規の1取引リスク上限が**5,000円**になり、DD -10%で**新規停止**、-15%で**全現金化**。
  - 銘柄数は3のまま（変更なし）。40日は次回の月次で帯ごとに採用判断。
  - `docs/strategy_params.json` の `weekly_feedback` が再計算される（スコア補正がやや弱まる）。
  - 戻す場合は該当の変更を `git revert`。
- **関連**: `docs/strategy/active.json` / `docs/strategy.html` / `back_tester/backtest_rolling_walkforward.py` / `back_tester/weekly_review.py` / `docs/strategy_params.json` / `docs/weekly/feedback.json` / `PROJECT_STATUS.md`

### 保有期間の比較（5/10/20/40/60営業日）を月次バックテストに追加（この変更）
- **commit**: この変更（保有期間の検証）
- **種別**: バックテスト / 検証
- **変更**:
  - `back_tester/backtest_rolling_walkforward.py` に `HOLDING_PERIODS = [5, 10, 20, 40, 60]` と `holding_period_effect()` を追加。
  - **利確・損切・ATR倍率はそのまま**にし、`price_tiers[].max_hold_days` だけを 5/10/20/40/60 に差し替えて、同じウォークフォワードのテスト期間で成績を比べる（地合いフィルタは外して保有期間だけを比較）。
  - 出力に**件数・勝率・PF・期待値・年率・最大DD・最大負け**の表を追加（標準出力・結果JSONの `holding_period_effect`・Markdownレポート）。
- **根拠・検証**:
  - 「週次（5日）・月次（20日）・四半期（60日）のどれが良いか」を感覚ではなく数字で決めるため。直近の検証ではスコアv3の期待値が5日 +0.66% / 20日 +3.23% で、保有を伸ばす方が優位だった。
  - ダミー取引でのスモークテストで、`max_hold_days` の上書き・統計計算・**元の `params` を書き換えないこと**を確認。
- **影響・ロールバック**: 月次バックテストの実行時間が少し増える（5通り×フォールド）。**設定は自動で変わらない**（表を出すだけ。採用判断は手動）。戻す場合は追加関数と出力を `git revert`。
- **関連**: `back_tester/backtest_rolling_walkforward.py` / `.github/workflows/run_walkforward.yml` / `PROJECT_STATUS.md`

### 地合い・セクターの広がりを全銘柄で計算し、「リスクオン」の表示を平易化（この変更）
- **commit**: この変更（地合いの測り方と表示）
- **種別**: 判定 / UI / ドキュメント
- **変更**:
  - `tools/check_strategy_plan.py::breadth_for` と `tools/prepare_strategy_note.py::compute_breadth_and_sectors` で、**`excluded`（週足が13週線より下）の除外をやめ、全銘柄（価格>0）で広がりを計算**するようにした。n と `low_sample` を返し、**10銘柄未満は「参考外」**。`sector_ok` / `bank_sector_ok` は10銘柄未満を**条件NG**にする。
  - `docs/strategy.html`: 「リスクオン」→**「買いやすい」**、「リスクオフ」→**「慎重に」**。サブに `リスクオン・1306.T 420.6 ＞ 200日線 398.7（+5.4%）／割れたら新規停止` と**不等号・乖離・意味**を表示。各カードに**銘柄数**（10未満は「参考外」）を併記。
  - `docs/index.html`: ヘッダーのバッジを「地合い: 買いやすい／慎重に」に変更（ツールチップに数字）。
  - `docs/guide.html`: **「地合い」の節を追加**。リスクオンは危険ではないこと、1306.Tと200日線、3層（全体・広がり・セクター）＋決算の見方。
  - `docs/ops.html`: 用語集の「地合い」を修正（リスクオン＝危険ではない／全銘柄で計算／10銘柄未満は参考外）。
- **根拠・検証**:
  - 週足下向きを除いてから「25日線の上比率」を測ると、上昇中の銘柄だけを集めることになり上振れする。銀行は77銘柄中75銘柄が除外され、**2銘柄で0%**になっていた。
  - 変更前後の実測（2026-10-23データ）:

    | 対象 | 変更前（除外あり） | 変更後（全銘柄） |
    | :--- | :--- | :--- |
    | 全体・25日線上 | 75.9%（1,108） | **33.9%（2,913）** |
    | 全体・200日線上 | 86.1% | **52.0%** |
    | 銀行・25日線上 | 0.0%（**2**） | **0.0%（77）** |
    | 情報・通信業 | 83.0%（194） | **54.9%（328）** |
    | 電気機器 | 59.3%（59） | **18.2%（203）** |
    | 海運業 | 33.3%（6） | **18.2%（11）** |

  - `check_strategy_plan.py --mode eod --force` を実行し、**カシオ計算機（6952）が「買い入口2,340に到達したが条件NG（電気機器の25日線上18.2%（203銘柄））」**になることを確認。東計電算（4746）は情報・通信業54.9%で条件クリアのまま。
  - HTTP配信で `strategy.html` / `index.html` / `guide.html` の描画を確認。
- **影響・ロールバック**:
  - **カシオ計算機（6952）の買い条件がNGに変わります**（電気機器セクターが弱い）。銘柄選びの結論が1つ変わるため、`docs/strategy/status.json` / `events.json` も更新。
  - `sector_ok` の閾値30%は**据え置き**。全銘柄ベースでは厳しめになる可能性があるため、次回の作戦ノートで実データを見て再校正する。
  - `excluded` 自体は**残す**（買う候補を絞る用途では正しい）。変えたのは広がりの測り方だけ。
  - 戻す場合は該当の集計と表示を `git revert`。
- **関連**: `tools/check_strategy_plan.py` / `tools/prepare_strategy_note.py` / `docs/strategy.html` / `docs/index.html` / `docs/guide.html` / `docs/ops.html` / `docs/strategy/status.json` / `docs/strategy/events.json` / `AGENTS.md` / `PROJECT_STATUS.md`

### 作戦を「確定終値」で作る保証・更新不要スキップ・Discord通知（この変更）
- **commit**: この変更（確定終値・通知・ナビ整理）
- **種別**: ワークフロー / 運用 / UI / ドキュメント
- **変更**:
  - **`tools/plan_staleness.py` を追加**（読み取りのみ）。直近の**確定終値**（15:30より前なら前営業日）と取引履歴の最終約定日で、台帳を作り直す必要があるかを判定する。`0 FRESH` / `2 STALE_PRICE` / `3 STALE_TRADES` / `4 PARTIAL`（当日の途中データ）/ `5 NO_PLAN` / `6 NO_DATA`。
  - **`main8.py` の対象日を「15:30より前は前営業日」に変更**（`get_target_date_str`）。日中に走っても当日の途中の足を確定値として保存しない（遅延した朝のジョブがザラ場中に走る場合の対策）。営業日判定で祝日・週末もさかのぼる。
  - `prompts/strategy_note.md`: 手順の冒頭に「作り直す必要があるかの判定」を追加。**FRESHなら何も作らず終了**、**古いまま／途中データなら停止**（古い価格で台帳を作らない）。`data_date` は期待取引日、`trade_history_last_date` はCSVの最終約定日を必須に。`updated_at` を追加。
  - `tools/check_strategy_plan.py`: `--notify-plan` を追加（作戦更新の通知文）。通知の銘柄表記を **「銘柄名（コード）」** に統一。
  - **`notify_plan_update.yml` を追加**。`docs/strategy/active.json` が push されたら Discord に【作戦更新】を送る（`--mode eod --force --no-write --notify-plan`）。「更新不要」では送らない。
  - **`weekly_review.py` に Discord 通知**を追加（最新週の1通：約定件数・勝率・平均・TOPIX超過・ベスト/ワースト・来週の要点）。`weekly_review.yml` に `DISCORD_WEBHOOK_URL` を追加。
  - **8ページのヘッダーを統一**（スクリーナー / 今日の作戦 / 取引記録 / 振り返り / 作戦ノート / その他）。今日の作戦の「手動チェック」「データ更新（AI）」「再読み込み」は**「操作」**に集約（JS不要の `<details>`）。
  - `docs/ops.html` に**「どのページをいつ見るか」早見表**、確定終値の扱い、判定の分岐、通知4種（AI推奨/トリガー/作戦更新/週次）を追記。
- **根拠・検証**:
  - `plan_staleness.py` の実測: 2026-10-24時点で `STALE_PRICE`（期待 10/23 / 履歴 10/22）、`--date 2026-10-22T10:00` で `PARTIAL` を確認。
  - `check_strategy_plan.py --mode eod --force --no-write --notify-plan` が exit 0 で動作し、通知文（plan_id・基準日・更新時刻・保有/監視/見送り件数・今日やること）を確認。
  - `py_compile` で変更したPythonの構文を確認。8ページのHTMLタグ対応検証とChromeヘッドレス描画（ナビ・「その他」「操作」の開閉）を確認。
- **影響・ロールバック**:
  - **日中に `main8.py` を実行しても当日の途中値は入らなくなります**（スクリーナーは前日終値のまま）。日中の現在値は「今日の作戦」が見ます。
  - 土日・祝日に手動更新すると、直近の営業日を対象にします（欠損した回を埋められます）。
  - 作戦の push ごとにDiscord通知が飛びます（1日に複数回更新すれば複数回）。
  - 戻す場合は `git revert`。`main8.py` の対象日は `(now.hour, now.minute) >= (15, 30)` を `now.hour >= 9` に戻す。
- **関連**: `tools/plan_staleness.py` / `main8.py` / `tools/check_strategy_plan.py` / `prompts/strategy_note.md` / `.github/workflows/notify_plan_update.yml` / `.github/workflows/weekly_review.yml` / `back_tester/weekly_review.py` / `docs/*.html` / `AGENTS.md` / `PROJECT_STATUS.md`

---

## 2026-10-23

### 作戦台帳を10/23の約定に合わせて更新（この変更）
- **commit**: この変更（台帳更新 10/23）
- **種別**: 作戦台帳 / データ
- **変更**:
  - `docs/strategy/active.json` の `positions` を **エアトリ（6191）100株のみ**に更新。
  - 三井松島HD（1518）＝OCO 2,190円で損切り、ヤマハ発動機（7272）＝引け1,772.7円で手仕舞いを `closed` に記録（実現損 -9,500円 / -9,130円、合計 -18,630円）。
  - カシオ計算機（6952）は10/23に20日高値2,333円を上抜け（引け2,357円）。追わず、**2,340円の押し目再テスト・損切2,255円**に付け替え。
  - 川崎汽船（9107）は `watch_only` / 優先度Cへ。**終値で25日線3,362円を回復するまで買わない**、決算11/4のため10/30以降は新規停止。
  - `rules` に「新規は1銘柄だけ・1取引リスク1万円以内」「OCOは証券会社側に必ず置く」を追記。
- **根拠・検証**:
  - 10/23の実約定（証券会社の約定照会）と、yfinance 5分足の終値・高安（9107 3,329円 / 6952 2,357円）、`docs/history/latest.json.gz` の25日線・20日高値を突き合わせ。
  - 海運は日本郵船 -2.2%、商船三井 -2.7%、川崎汽船 -2.3%（TOPIX連動ETF -1.16%）で、個別材料ではなくセクター全体安と判断。
  - カシオは 2,357円・損切2,122円だとリワード/リスク1.2倍。押し目2,340円・損切2,255円なら約3.0倍。
  - `python -m json.tool docs/strategy/active.json` で構文確認済み。
- **影響・ロールバック**: 売買判断の基準が変わる（新規は1銘柄、9107は買わない）。bot（`check_strategy_plan.py`）は次回実行から新台帳を読む。戻す場合は `git revert`。
- **関連**: `docs/strategy/active.json` / `docs/strategy.html`（次回監視で自動更新）

---

### 運用マニュアル（`docs/ops.html`）を追加
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
