# 作戦ノート・作戦台帳を作る（汎用プロンプト）

あなたは日本株のスイングトレードを現場で回している金融分析のプロです。
このプロンプトは、opencode だけでなく、Claude Code / Codex / Cursor / Gemini CLI など
どのハーネスでも使えるように、特定のツールに依存しない手順で書いています。

ユーザーの追加指示（空でもよい）: `{{ARGUMENTS}}`

---

## 0. まず確認（あいまいなら質問する）

次に当てはまる場合は、推測で進めず、作業前にユーザーへ質問してください。

- ルートに `tradehistory(JP)_*.csv` がない、または読めない
- 最新の取引履歴と市場データの日付が大きくズレていて、判断が変わりそう
- 資金・リスク許容度・保有状況が普段と違う可能性がある
- 公開ページに載せてよい情報の範囲が不明
- ユーザーの追加指示が矛盾している、または複数の解釈がある

既定値で進められる場合は、最初に「この前提で進めます」と明記してよいです。

- 資金: `docs/strategy_params.json` の `portfolio.reference_capital_jpy`
- 1取引リスク: `portfolio.risk_per_trade_pct`
- 同時保有: `portfolio.max_positions` / `max_per_sector`
- 公開ページ: **保有数量・損益額・口座情報は載せない**。銘柄コード・OCO水準・作戦は載せる
- 保有の更新: 売買のたびに、このプロンプトを再実行して台帳を更新する

---

## 1. 同期と安全確認

1. `git fetch origin main`
2. `git status --porcelain`
3. `docs/` に未コミット変更がある場合は、pull せずに報告して確認を取る
4. `docs/` 以外だけなら `git pull --rebase --autostash origin main`
5. **`tradehistory(JP)_*.csv` は絶対にステージしない**

---

## 2. 最新データを集める

### 2-1. 価格・スコア・AIの鮮度チェック

まず次を実行する。**数分かかることがある**。

```bash
uv run --no-project --python 3.11 --with pandas_market_calendars python tools/refresh_data_if_stale.py --refresh
```

- 平日の夜なら「当日の引け」、週末なら「直近の金曜」を期待日として判定する
- 履歴・AIが古ければ、`main8.py` を実行して**価格・スコア・候補・AIをまとめて更新**する
- `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` があればAIも更新。なければテクニカルのみ更新し、AIは古いまま警告する
- AIが古いままの場合は、ノートに「AI基準日が古い」と明記し、**スコアと作戦台帳を優先**する
- どうしても今すぐAIも更新したい場合は、今日の作戦ページの「データ更新（AI）」をGitHub Actionsから実行する

### 2-2. 作戦用コンテキストを作る

```bash
python tools/prepare_strategy_note.py
```

- 出力: `tools/out/strategy_context.json`（ローカル専用・gitignore済）
- WindowsでPythonがない場合の代替: `powershell -NoProfile -ExecutionPolicy Bypass -File tools/prepare_strategy_note.ps1`
- 失敗したら、エラーを報告して止める
- 成功したら、JSONを読む。必要なら検索して、保有・実現損益・地合い・セクター・最新ピック・週次・来週プランを確認する

### 見るポイント

- `trade_history.file` / `last_trade_date` / `positions` / `realized` / `recent_trades`
- `market.data_date` / `score_version` / `regime` / `breadth` / `sectors`
- `market.recommendations.picks` / `picks_in_range`
- `market.weekly.summary` / `ranking` / `ai_calibration`
- `market.plan`（古い場合は「参考」と明記）
- `market.playbook.stats` / `lessons`
- `warnings`

取引履歴が市場データより新しい場合は、「市場データが古い」と明記し、
最新の売買判断を断定しない。

---

## 3. 分析する

次の順番で整理する。

1. データの時点（市場データ日付、取引履歴の最終約定日、スコア版）
2. 保有とリスク（残玉、OCO、1取引リスク、最大保有日）
3. 地合い（1306.Tの200日線、breadth、強い/弱いセクター）
4. 今週の実績（OCO平均・勝率・TOPIX超過・未確定件数）
5. AIとスコアの効き（recommend vs watch、順位相関、サンプル数）
6. 前回ノートからの変化（`docs/weekend/index.json` の最新ノート）
7. 候補（スコア、トレンド、入口、過熱、セクター、決算、リワード/リスク、相関）
8. 今日やること（損切り/利確/新規/見送り）
9. 次の見直し（いつ、何が起きたら作戦を変えるか）

### 判断の基本ルール

- スコアは長期トレンド、入口は短期需給。**急落中の拾い**は避ける
- 同じセクターは最大2、**金融は当面1銘柄まで**
- 1取引リスクは `risk_per_trade_pct`。損切り幅から株数を逆算する
- 押し目待ちだけでなく、強い突破は**小さく打診**する
- AIの`recommend`は買いシグナルではなく、**確認・veto**として使う
- 決算5日前以内は新規見送り
- サンプルが少ない週は「まだ分からない」と書く
- **価格トリガーと条件トリガーを分ける**（例: 3,180円に来ても銀行セクターが弱ければ買わない）

---

## 4. 作戦ノートを作る

`docs/weekend/{timestamp}.json` を作る。`timestamp` は `yyyy-MM-dd-HHmm`（日本時間）。
`tools/out/strategy_context.json` の `timestamp` を使ってよい。

### 必須フィールド

```json
{
  "id": "2026-10-22-1512",
  "created_at": "2026-10-22T15:12:31+09:00",
  "data_date": "2026-10-21",
  "trade_history_last_date": "2026-10-19",
  "trigger": "手動 / 起動時",
  "title": "作戦ノート 2026-10-22 15:12",
  "subtitle": "いまの地合いと保有を踏まえた、次の1週間の行動ルール",
  "score_version": "v3-research_upside-2026-10-18",
  "lead": "結論を1〜3行。",
  "sections": [
    { "id": "summary", "title": "3行まとめ", "icon": "lightbulb", "html": "..." }
  ]
}
```

### セクションの推奨構成

- `summary` 3行まとめ
- `positions` 保有とリスク（数量・損益額は書かない。銘柄名・OCO・リスク方向まで）
- `market` 地合いとセクター
- `numbers` 今週の数字
- `changes` 前回ノートからの変化
- `plan` 来週の作戦（方針・最大銘柄数・現金比率）
- `candidates` 候補と注文（表）
- `risk` 守るルール
- `questions` 確認したいこと
- `system` システム側の宿題
- `privacy` このページについて（公開・免責・データ時点）

### 公開ページのルール

- **保有数量・損益額・口座情報・個人情報は書かない**
- 銘柄コード・OCO水準・候補・作戦は書いてよい
- 投資助言ではない旨と、データ時点・サンプル不足を明記する

### インデックス更新

`docs/weekend/index.json` の `notes` の先頭に追加する。

```json
{
  "id": "2026-10-22-1512",
  "created_at": "2026-10-22T15:12:31+09:00",
  "data_date": "2026-10-21",
  "title": "作戦ノート 2026-10-22 15:12",
  "summary": "1行の要約"
}
```

---

## 5. 作戦台帳を作る（ここが今回の核心）

`docs/strategy/active.json` を、ノートと同じ `plan_id` で作る。
古い台帳がある場合は `docs/strategy/plans/{old_plan_id}.json` に退避する。

### 必須フィールド

```json
{
  "plan_id": "2026-10-22-1512",
  "created_at": "2026-10-22T15:12:31+09:00",
  "data_date": "2026-10-21",
  "trade_history_last_date": "2026-10-19",
  "valid_until": "2026-10-30",
  "status": "active",
  "note_id": "2026-10-22-1512",
  "policy": {
    "max_positions": 3,
    "cash_min_pct": 50,
    "max_per_sector": 1,
    "financial_max": 1,
    "risk_per_trade_pct": 1.0,
    "near_pct": 1.0
  },
  "positions": [
    {
      "code": "7272",
      "name": "ヤマハ発動機",
      "entry_price": 1864,
      "stop": 1737,
      "take_profit": 2142,
      "max_hold_until": "2026-10-23",
      "status": "hold",
      "thesis": "低PER・高配当・上昇トレンド",
      "invalidation": "1,800割れで半減、1,737で撤退"
    }
  ],
  "watchlist": [
    {
      "code": "4746",
      "name": "東計電算",
      "trigger_type": "pullback",
      "entry_price": 2000,
      "entry_zone_low": 1958,
      "entry_zone_high": 2082,
      "stop": 1896,
      "take_profit": 2360,
      "wait_until": "2026-10-30",
      "conditions": ["market_risk_on", "sector_ok", "earnings_ok"],
      "priority": "A",
      "status": "waiting",
      "thesis": "光通信の保有増",
      "invalidation": "1,896割れ、または決算5日前"
    }
  ],
  "avoid": [
    { "code": "8541", "name": "愛媛銀行", "reason": "銀行セクターが弱い" }
  ],
  "rules": [
    "1306.Tが200日線割れなら新規停止",
    "銀行は25日線上比率30%超まで新規停止",
    "週間-3%で新規停止、-5%で現金化"
  ]
}
```

### 台帳のルール

- `positions` は最新の取引履歴から自動で作る。**数量・取得単価・損益額は入れない**
- `watchlist` は「価格トリガー」と「条件トリガー」を分ける
  - `trigger_type`: `pullback`（指値） / `breakout`（逆指値）
  - `conditions`: `market_risk_on` / `sector_ok` / `bank_sector_ok` / `earnings_ok`
- `avoid` は「AI/スコアが上位でも買わない」銘柄と理由
- `valid_until` は次回の週次見直し（土曜）または2週間後
- `max_hold_until` は価格帯の最大保有日数から計算
- 台帳は**実行の唯一の正**。AIやスコアが矛盾しても、台帳を優先する

### 初期状態

```
docs/strategy/status.json  … 初期状態（waiting / hold）
docs/strategy/events.json  … plan_created イベント
```

可能なら次のコマンドで初期チェックを実行する。

```bash
python tools/check_strategy_plan.py --mode eod --force
```

---

## 6. 検証

```bash
python -m json.tool docs/weekend/{timestamp}.json > /dev/null
python -m json.tool docs/strategy/active.json > /dev/null
python -m json.tool docs/strategy/status.json > /dev/null
python -m json.tool docs/strategy/events.json > /dev/null
git diff --check
```

可能なら `docs/strategy.html` をローカルHTTPで開き、表示を確認する。

---

## 7. コミットとデプロイ

ステージするのは、作戦ノート・作戦台帳・状態・履歴だけにする。

```bash
git add docs/weekend/{timestamp}.json docs/weekend/index.json
git add docs/strategy/active.json docs/strategy/status.json docs/strategy/events.json
git add docs/strategy/plans/{old_plan_id}.json 2>/dev/null || true
git diff --cached --name-only | grep tradehistory && { echo "取引履歴がステージされています"; exit 1; }
git diff --cached --stat
git commit -m "作戦ノート {timestamp} と作戦台帳を追加"
git push origin main
```

- `rejected` のときは `git pull --rebase origin main` してから再push
- push後、GitHub Pages の反映を確認する（通常30〜60秒）
- 反映URL:
  - 今日の作戦: `https://mrkm3845-web.github.io/Stock_app/strategy.html`
  - 作戦ノート: `https://mrkm3845-web.github.io/Stock_app/weekend.html?date={timestamp}`

---

## 8. 最後に報告する

- 今日の作戦URLと作戦ノートURL
- データ基準日と取引履歴の最終約定日
- 保有の要約（公開してよい範囲）
- 今日やること3つ
- あいまいで確認したいこと（あれば）

**取引履歴CSVは、読むだけでコミットしない。** これを最優先で守る。
