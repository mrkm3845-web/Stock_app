# 日本株スイング戦略 バックテスト（ローリングウォークフォワード検証）

スクリーナー（Stock_app/main8.py）と同じ**スコア式**を再現し、過去データから
成績が崩れにくいパラメータを探索・検証するためのバックテストです。
結果はガード付きで `docs/strategy_params.json` へ自動反映されます。

---

## 1. ファイル構成

> Stock_app に統合済み（1リポジトリ）。共通モジュールとパラメータはリポジトリ直下で共有します。

| ファイル | 役割 |
| :--- | :--- |
| `back_tester/backtest_rolling_walkforward.py` | 本体。スコア式のウォークフォワード＋株価帯別エグジット最適化＋選定エッジ検証 |
| `back_tester/apply_optimal_params.py` | 結果JSONを読み、ガード付きで `docs/strategy_params.json` を更新 |
| `back_tester/research_signals.py` | 候補シグナル研究（Phase 1）。指標ごとの分位スプレッドを測る（トレンド系＋過熱系 `ret_5d` / `dist_sma5` / `rsi14` / `gap_pct` / `run_up_days`、業種相対強度 `sector_rs_20d` を含む） |
| `back_tester/weekly_review.py` | **週次答え合わせ**。実運用で提示した推奨・技術上位を1週間（月〜金）単位で採点（下記 8章） |
| `common/config.py` | `docs/strategy_params.json` のローダー（リポジトリ直下） |
| `common/features.py` | 特徴量・スコア計算（スクリーナーと同一式） |
| `docs/strategy_params.json` | 単一情報源（シグナル閾値・株価帯・警告・スコア重み・AI設定・週次設定） |
| `.github/workflows/run_walkforward.yml` | 月次実行＋ガード反映＋コミット（`docs/strategy_params.json` を直接更新） |
| `.github/workflows/weekly_review.yml` | 週次（土曜09:00 JST）答え合わせ実行＋コミット |
| `back_tester/results/` | 結果JSON / CSV / Markdown・候補シグナル研究・週次レポート |

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
- **地合い指標の比較（`regime_effect`）**: なし / 指数MA（1306.T vs SMA200） / breadth（上昇銘柄比率）でスコア上位Kの成績を比較。本期間はいずれのフィルタも改善せず（既定OFF）。
- **同時保有数の比較（`position_sizing_effect`）**: `max_positions` 3/4/5/8 の年率・最大DDを比較し、資金配分ガイドの根拠にする。
- **成行 vs 押し目指値（`entry_style_comparison`）**: スコア上位Kの選定に対し「翌日成行」と「シグナル日終値から N×ATR 下の買い指値（待機 M 日、未到達なら見送り）」の OOS 成績を比較。指値の深さ・待機日数は**学習期間で選択し検証期間で評価**する。さらに**過熱度（low / moderate / high）別**にグリッドを計算し、高値掴み回避の効果を区分ごとに測る。

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
| `signals` | スコア閾値（gc_window / val_ratio_min / avg_val_min_k）。※Phase 2 のトレンド合成スコアでは未使用（出力互換のため保持） |
| `price_tiers` | 株価帯ごとの tp_pct / sl_pct / atr_sl_mult / max_hold_days。株価帯別最適化の OOS 最良条件 |
| `entry_guard` | 押し目エントリーの深さ・待機日数を**過熱度別**に反映。moderate→`pullback_atr_shallow`/`pullback_wait_days`、high(strong+extreme)→`pullback_atr_deep`/`probe_wait_days`。`entry_style_comparison` で各区分が成行より OOS 改善した場合のみ |

**ガード条件**（各対象ごとに独立判定）:
- 最低取引数（100件）以上
- OOS PF ≧ 1.15
- OOS 期待値 > 0
- OOS 年率リターン > ベンチマーク（TOPIX ETF）
- OOS 最大DD ≦ 50%
- （signals のみ）近傍安定性: 同エグジットでプラス条件が2つ以上
- （entry_guard のみ・区分ごと）約定率 ≧ 0.2 / 成行比で PF・期待値が改善
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

- **選定エッジ（Phase 2 で改善）**: 旧スコアは分位スプレッド +0.025% / Spearman 0.24（≒無効、上位選択がランダムに劣後）。Phase 2 のトレンド合成スコアで **+0.347% / 0.92** に改善（上位分位 +0.16% / 下位分位 −0.19%）。ただし上位の PF（1.04）はランダム（1.14）と同等で、**リターンでは勝つがリスク調整後は同等**。
- **常時フル投資（同時保有K）**: 常に上位Kへ投資し現金待機がない。地合いフィルタ（指数MA・breadth）は検証の結果**改善せず不採用**。同時保有数は `position_sizing_effect` を参照（本期間は4が最良だが既定は5）。
- **プリセットは未検証**: Stock_app の index.html にある「3大実証厳選」等の PF 値は削除済みの旧バックテスト由来で、現行検証では再現未確認（参考値）。
- **業種相対強度は不採用**: `sector_rs_20d`（同業種平均からの20日リターン超過）は分位スプレッド **−0.162%** / Spearman **−0.139** / プラス期間率 0.286（2026-09-20 検証）。GC直後・出来高増加率・週足上昇と同様、現行期間では逆方向のためスコアに採用しない。
- **サバイバーシップバイアス**: 現在のJPX上場銘柄のみ対象。過去に上場廃止・合併した銘柄は欠落するため、成績は実運用より楽に出る傾向。
- **多重検定**: 条件数を増やすほど偶然の好成績が出やすい。ガード（近傍安定性・前回比）で緩和。
- **Stage2（AI）は未検証**: 本検証は技術スコアのランキング力と機械的エグジットのみ。実運用の AI 判断は **8章の週次答え合わせ**で実績を観測・校正する（本バックテストの対象外）。
- **atr_trail の尾リスク**: ATRトレーリングはギャップダウン時に始値で手仕舞うため、固定TP/SLより大きな単発損失が出ることがある。
- **ポートフォリオ指標は実現損益ベース**（保有中の時価評価は行わない）。

---

## 8. 週次答え合わせ（`weekly_review.py`）

月次のウォークフォワードが「**未来向けにパラメータが頑健か**」を合成履歴で検証するのに対し、週次レビューは「**実際に出した推奨がどうなったか**」を過去向けに採点する。両者は**並行・補完**（置換ではない）。

### 8-1. 採点ルール（合意済み）
- **対象**: AI推奨（`recommend`）と、技術スコア上位候補の両方。
- **エントリー**: `entry_plan` 通り。
  - `breakout_chase` / `probe_only`: 買いストップ。翌日以降 `entry_wait_days` 内に `entry_price` へ到達で約定（窓開けは始値）。現在値近辺なら翌日寄成扱い。
  - `pullback_wait`: `entry_zone_high` への押し目指値。未到達は**見送り**（勝率の分母から除外し、約定率を別集計）。
- **手仕舞い**: 約定日を含む週の**金曜11:30（前場引け）に成行**。「昼」は取引不可のため前場引けで代用。
- **コスト**: 手数料0.05%＋スリッページ0.1%（既存バックテストと同率）。上昇＝赤／下落＝青。
- **参考**: アプリのルール出口（TP/SL/最大保有日数、同日両到達は損切り優先）を適用した結果も併記。
- **ベンチマーク**: `1306.T`（TOPIX ETF）の同区間リターン。

### 8-2. 出力指標
- 約定率・勝率・平均/中央リターン・TOPIX超過・`recommend` vs `watch`。
- **順位の効き**: スコアとリターンの Spearman、上位群/下位群のスプレッド（満点で並ぶ場合は順位付け不能として表示）。
- **見送りの機会損益**: 押し目未到達などで約定しなかった銘柄を「翌営業日寄りの成行」で追随していた場合の損益（機会損失か回避か）。
- **上位N件レビュー**: その日の推奨順位（判定→スコア→AI順位）の上位N件（既定 1/3/5）だけを買った場合の成績。全部は買えない実情に合わせた実行可能性の検証。
- 入口（`entry_type`）別・過熱度別・業種別の実績、および**今週の気づき**（テンプレ文）。

### 8-3. 入力と出力
- 入力: `docs/picks/{date}.json`（main8 の日次スナップショット）。無い日は `docs/history/{date}.json` ＋ `docs/ai_analysis/{date}.json` から**復元**（レポート上「復元」表示）。
- **非営業日（土日祝・取引所休場）は除外**：ベンチマーク（`1306.T`）の実際の営業日を基準に、シグナル日が営業日でなければ集計から外す。`main8.py` 側も JPX 取引所カレンダーで非営業日をスキップする。
- 出力: `docs/weekly/{YYYY-Www}.json` / `latest.json` / `index.json`、`docs/weekly.html`、`back_tester/results/weekly_review_{week}.md`。

### 8-4. 実行
```bash
# 既定（先週＋今週の2週を採点。金曜シグナルを翌週に確定させる）
python back_tester/weekly_review.py
# 対象週を指定
python back_tester/weekly_review.py --week 2026-W39
# history にある全週を遡及生成
python back_tester/weekly_review.py --all
```
CI は [`weekly_review.yml`](../.github/workflows/weekly_review.yml) が**毎週 土曜 09:00 JST**（`workflow_dispatch` で週指定・全週遡及も可）。

> **持ち越し採点（carryover_weeks=2）**: 金曜シグナルは翌週に約定・手仕舞いするため、土曜時点では未確定
> （＝「未評価」）。毎回**先週分も再採点**して確定させることで、金曜シグナルや週をまたぐ押し目約定の
> 取りこぼしを防ぐ。各週のレポートは自己完結なので二重計上は起きない。

### 8-5. 還元（結果をランキングへ反映）
- **Phase B（実装済み）**: 直近N週の実績から**過熱度別のスコア補正量**を計算し、`docs/strategy_params.json` の `weekly_feedback` に書き込む。`main8.py` は `enabled=true` のとき `score + overheat_delta` で並び順を補正する。
  - 過学習防止: **縮小推定**（`n/(n+k)`）＋**上限クランプ**（`feedback_max_delta`、既定±3）＋**最低サンプル**（`feedback_min_group_trades`、既定8件/群）。群が少ない/偏る間は補正0（観測のみ）。
  - 透明性: `docs/weekly/feedback.json` に補正の根拠（群別の件数・平均・delta）を出力し、画面の「来週の作戦」に表示。
- **Phase C（将来）**: 十分なサンプルが貯まったら、月次バックテストと同様のガード付きで `entry_guard`・スコア重みへ拡張。
- 小さなサンプルで重みを動かさない（過学習回避）ことを優先する。
