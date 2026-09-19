# 日本株スイング戦略 バックテスト（ローリングウォークフォワード検証）

スクリーナー（Stock_app/main8.py）と同じ**スコア式**を再現し、過去データから
成績が崩れにくいパラメータを探索・検証するためのバックテストです。
結果はガード付きで `strategy_params.json` へ自動反映されます。

---

## 1. ファイル構成

> Stock_app に統合済み（1リポジトリ）。共通モジュールとパラメータはリポジトリ直下で共有します。

| ファイル | 役割 |
| :--- | :--- |
| `back_tester/backtest_rolling_walkforward.py` | 本体。スコア式のウォークフォワード＋株価帯別エグジット最適化＋選定エッジ検証 |
| `back_tester/apply_optimal_params.py` | 結果JSONを読み、ガード付きで `docs/strategy_params.json` を更新 |
| `back_tester/research_signals.py` | 候補シグナル研究（Phase 1）。指標ごとの分位スプレッドを測る |
| `common/config.py` | `docs/strategy_params.json` のローダー（リポジトリ直下） |
| `common/features.py` | 特徴量・スコア計算（スクリーナーと同一式） |
| `docs/strategy_params.json` | 単一情報源（シグナル閾値・株価帯・警告・スコア重み・AI設定） |
| `.github/workflows/run_walkforward.yml` | 月次実行＋ガード反映＋コミット（`docs/strategy_params.json` を直接更新） |
| `back_tester/results/` | 結果JSON / CSV / Markdown・候補シグナル研究 |

> 旧スクリプト `backtest_scanner.py` / `backtest_scanner_v2.py` / `backtest_volume_deepdive.py` は
> ウォークフォワードに統合されたため削除済み。

---

## 2. 検証内容

`backtest_rolling_walkforward.py` は、実運用（main8.py）と同じロジックを再現して検証します。

- **スコア式（Phase 2 トレンド合成）**: 52週高値位置(30) / 200日線乖離(25) / 200日線傾き(25) / 120日リターン(20)。旧式の GC・出来高増加率・週足は研究で逆効果と判明したため廃止。
- **エントリー**: 各営業日にスコア上位K銘柄を選び、**翌営業日の寄り**で約定（スリッページ・手数料込み）。
- **エグジット**: 固定TP/SL と ATRトレーリング を比較。株価帯ごとに最適化。
- **分割**: 学習12ヶ月 / 検証3ヶ月 / スライド3ヶ月（`walk_end` は実行日の前月末でローリング）。
- **指標**: 資金制約（同時保有上限・固定比率サイズ）を加味した年率リターン / 最大DD / シャープ / PF / 勝率。
- **ベンチマーク**: TOPIX ETF (`1306.T`) の同期間バイ&ホールド。

### 設計方針（バイアス対策）

- **先読み排除**: シグナルは当日引け確定値のみ使用。エントリーは翌日寄り。
- **保守的約定**: 同日にTP/SL両方到達した場合は損切り優先（`same_day_tp_sl="loss"`）。
- **ファンダメンタル除外**: PER/PBR等は過去時点の開示値が取れず先読みバイアスになるため、価格・出来高・移動平均・売買代金のみ使用。
- **過剰適合対策**: 時系列分離（train/test）＋最低取引数＋ガード付き反映（近傍安定性・前回比劣化なし）。

### 選定エッジ検証（Phase 0・新規）

「スコアに選定エッジがあるか」を、実エグジットの成績とは独立に測るための分析を追加しました。結果は `backtest_walkforward_result.json` の `quantile_analysis` / `selection_comparison` / `weight_ablation` / `atr_trail_worst_trades` に格納されます。

- **スコア分位分析（`quantile_analysis`）**: 各営業日、適格銘柄をスコアで横断的に分位（デシル）し、各分位の**将来リターン**（翌日寄り→H日後引け、TP/SLなし）を集計。分位と平均リターンの Spearman 相関・上位−下位スプレッド・プラスだったフォールド率で「単調に効いているか」を判定する。
- **選定比較（`selection_comparison`）**: スコア上位K・ランダム・下位K の3モードで同一エグジットの成績を比較し、上位がランダム/下位を上回るかを確認する。
- **重み寄与分解（`weight_ablation`）**: スコア成分を1つずつゼロにして（leave-one-out）、上位−下位スプレッドへの寄与を測る。効いていない成分の特定に使う。
- **テール監査（`atr_trail_worst_trades`）**: ATRトレーリングの最悪取引を列挙し、ギャップダウン等の大きな単発損失の原因を確認する。

---

## 3. 実行方法

### ローカル

```bash
uv run --no-project --python 3.11 --with pandas --with numpy --with requests --with yfinance --with openpyxl --with xlrd python backtest_rolling_walkforward.py
```

テスト時は `CONFIG["max_stocks_sample"]`（例: `100`）や `CONFIG["walk_end"]`（例: `"2023-12-31"`）で絞れる。

### CI（自動）

`run_walkforward.yml` が**毎月 第1土曜 21:00 JST** に実行されます。

1. バックテスト実行（データは増分キャッシュ、`walk_end` は前月末）
2. `apply_optimal_params.py` でガード付き反映
3. `results/` と `strategy_params.json` をコミット

手動で即時実行したい場合: GitHub の Actions タブ →「Run Walkforward Backtest」→「Run workflow」。

---

## 4. 結果の見方

- `results/backtest_walkforward_report.md`: 人間向けレポート（上位条件・株価帯別エグジット・選定エッジ検証）。
- `results/backtest_walkforward_result.json`: 機械可読（`apply_optimal_params.py` の入力）。`quantile_analysis` / `selection_comparison` / `weight_ablation` / `atr_trail_worst_trades` を含む。
- `results/backtest_walkforward_result.csv`: シグナル条件ランキング。
- `results/backtest_tier_exit.csv`: 株価帯別の最適エグジット。

主要指標: **PF（プロフィットファクター）**、**期待値（1取引平均）**、**年率リターン**、**最大DD**。

選定エッジの判定は、**分位分析の「上位−下位スプレッド」「Spearman相関」** と **選定比較（上位 vs ランダム vs 下位）** を優先して確認する（実エグジットの成績はエグジット設計の影響を混ぜるため）。

---

## 5. 反映フロー（ガード付き）

`apply_optimal_params.py` がガードをすべて満たした対象のみ `strategy_params.json` を更新します。

| 反映対象 | 内容 |
| :--- | :--- |
| `signals` | スコア閾値（gc_window / val_ratio_min / avg_val_min_k）。シグナルグリッドの OOS 最良条件 |
| `price_tiers` | 株価帯ごとの tp_pct / sl_pct / atr_sl_mult / max_hold_days。株価帯別最適化の OOS 最良条件 |

**ガード条件**（各対象ごとに独立判定）:
- 最低取引数（100件）以上
- OOS PF ≧ 1.15
- OOS 期待値 > 0
- OOS 年率リターン > ベンチマーク（TOPIX ETF）
- OOS 最大DD ≦ 50%
- （signals のみ）近傍安定性: 同エグジットでプラス条件が2つ以上
- 前回反映済みより有意に劣化していない（`results/applied_state.json` と比較）

不通過の対象は更新されず、結果レポートのみコミットされます。安全側の挙動です。

---

## 6. 運用方法（推奨ルーチン）

### 毎月（自動）
1. 第1土曜に CI が実行され、ガード通過時のみ `signals` / `price_tiers` が **`docs/strategy_params.json` に直接反映**・コミットされる（統合により手動同期は不要）。
2. 結果は `back_tester/results/backtest_walkforward_report.md` と候補シグナル研究 `research_signals.*`、GitHub Actions のログで確認。
3. 反映されたパラメータは、Stock_app の次回スケジュール実行（平日1日5回 / AI 20:17 JST）で**自動的に有効化**される。
4. **重要**: ガードは絶対条件（PF・期待値・ベンチマーク超え・DD上限・近傍安定性・前回比劣化なし）を満たした場合のみ反映する。現状はエッジ未確認のため、更新が起きないことがある（安全側）。

### 毎四半期（人間レビュー）
- レポートを見て戦略ルール自体の見直し。大きな変更は人間が判断。
- 判断材料は `results/`（特に `research_signals.csv` の指標ランキング）。

### ロールバック
- 不調時は `git revert` で `docs/strategy_params.json` を前バージョンへ即時復旧。

---

## 7. 既知の制約・注意

- **現状は選定エッジ未確認**: 直近の検証（2022-01〜2026-08）では最良条件でも OOS PF ≈ 1.0・期待値 ≈ 0 で、ベンチマーク（TOPIX ETF）に劣後している。まず `quantile_analysis` / `selection_comparison` でエッジの有無を確認してから、スコア設計・重み・エグジットの改善に進む。
- **毎日 top-K を常時フル投資**: 現行は常に上位5銘柄へ投資し、レジームフィルタ（下降相場で買わない）や現金待機がない。これが最大DD・連敗の主因。
- **プリセットは未検証**: Stock_app の index.html にある「3大実証厳選」等の PF 値は削除済みの旧バックテスト由来で、現行検証では再現未確認（参考値）。
- **サバイバーシップバイアス**: 現在のJPX上場銘柄のみ対象。過去に上場廃止・合併した銘柄は欠落するため、成績は実運用より楽に出る傾向。
- **多重検定**: 条件数を増やすほど偶然の好成績が出やすい。ガード（近傍安定性・前回比）で緩和。
- **Stage2（AI）は未検証**: 本検証は技術スコアのランキング力と機械的エグジットのみ。実運用の AI 判断・実手仕舞いは対象外。
- **atr_trail の尾リスク**: ATRトレーリングはギャップダウン時に始値で手仕舞うため、固定TP/SLより大きな単発損失が出ることがある。
- **ポートフォリオ指標は実現損益ベース**（保有中の時価評価は行わない）。
