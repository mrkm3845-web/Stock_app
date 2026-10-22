---
description: 最新の取引履歴と市場データから、その時点の作戦ノートを一発で作る
agent: build
---

# 作戦ノートを一発で作る

ユーザーの追加指示（空でもよい）: `$ARGUMENTS`

このコマンドは、**起動したタイミング**の最新データと最新の取引履歴を読み、
「今どう動くか」を作戦ノートにまとめて、GitHub Pages にデプロイするためのものです。
週末に限らず、平日の引け後・前場前・急落時など、どのタイミングでも使えます。

## 0. まず確認（あいまいなら質問する）

次のどれかに当てはまる場合は、**推測で進めず、作業前にユーザーへ質問**してください。

- ルートに `tradehistory(JP)_*.csv` がない、または読めない
- 最新の取引履歴と市場データの日付が大きくズレていて、判断が変わりそう
- 資金・リスク許容度・保有状況が普段と違う可能性がある
- 公開ページに載せてよい情報の範囲が不明
- ユーザーの追加指示が矛盾している、または複数の解釈がある

一方、次の既定値で進められる場合は、最初に「この前提で進めます」と明記して進めてよいです。

- 資金: `docs/strategy_params.json` の `portfolio.reference_capital_jpy`
- 1取引リスク: `portfolio.risk_per_trade_pct`
- 同時保有: `portfolio.max_positions` / `max_per_sector`
- 公開ページ: 保有数量・損益額・口座情報は載せない（銘柄名・OCO水準・作戦は載せる）

## 1. 同期と安全確認

1. `git fetch origin main`
2. `git status --porcelain` を確認
3. `docs/` に未コミット変更がある場合は、pull せずに報告して確認を取る
4. `docs/` 以外だけなら `git pull --rebase --autostash origin main`
5. **`tradehistory(JP)_*.csv` は絶対にステージしない**

## 2. 最新データを集める

次のコマンドを実行してください。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/prepare_strategy_note.ps1
```

- 出力: `tools/out/strategy_context.json`（ローカル専用・gitignore済）
- 失敗したら、エラーを報告して止める（取引履歴の形式違い・文字コード・ファイル欠損など）
- 成功したら、出力されたJSONを読む。必要なら `tools/out/strategy_context.json` を検索して、
  保有・実現損益・地合い・セクター・最新ピック・週次・来週プランを確認する。

### 見るポイント

- `trade_history.file` / `last_trade_date` / `positions` / `realized` / `recent_trades`
- `market.data_date` / `score_version` / `regime` / `breadth` / `sectors`
- `market.recommendations.picks` / `picks_in_range`
- `market.weekly.summary` / `ranking` / `ai_calibration`
- `market.plan`（古い場合は「参考」と明記）
- `market.playbook.stats` / `lessons`
- `warnings`

**重要**: 取引履歴が市場データより新しい場合（例: 取引は10/19、市場データは10/16）は、
「市場データが古い」と明記し、無理に最新の売買判断を断定しない。

## 3. 分析する

次の順番で、簡潔に整理してください。

1. **データの時点**: 市場データの日付、取引履歴の最終約定日、スコア版
2. **保有とリスク**: 残玉、平均取得単価、現在値、含み損益、OCO、1取引リスク
3. **地合い**: 1306.Tの200日線、breadth、強い/弱いセクター
4. **今週の実績**: 週次OCOの平均・勝率・TOPIX超過・未確定件数
5. **AIとスコアの効き**: recommend vs watch、順位相関、サンプル数
6. **前回ノートからの変化**: `docs/weekend/index.json` の最新ノートを読み、保有・作戦・リスクの変化を1〜3行で
7. **候補**: スコア、トレンド、入口、過熱、セクター、決算、リワード/リスク、相関で絞る
8. **今日やること**: 損切り/利確/新規/見送りを、条件付きで具体的に
9. **次の見直し**: いつ、何が起きたら作戦を変えるか

### 判断の基本ルール

- スコアは長期トレンド、入口は短期需給。**急落中の拾い**は避ける。
- 同じセクターは最大2、**金融は当面1銘柄まで**。
- 1取引リスクは `risk_per_trade_pct`。損切り幅から株数を逆算する。
- 押し目待ちだけでなく、強い突破は**小さく打診**する（全部見送らない）。
- AIの`recommend`は買いシグナルではなく、**確認・ veto**として使う。
- 決算5日前以内は新規見送り。データがnullでも油断しない。
- サンプルが少ない週は「まだ分からない」と書く。

## 4. 作戦ノートを作る

`docs/weekend/{timestamp}.json` を作成してください。
`timestamp` は `yyyy-MM-dd-HHmm`（日本時間）。`tools/out/strategy_context.json` の `timestamp` を使ってよいです。

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
- `numbers` 今週の数字（週次・スコア・AI）
- `changes` 前回ノートからの変化
- `plan` 来週の作戦（方針・最大銘柄数・現金比率）
- `candidates` 候補と注文（表）
- `risk` 守るルール（損切り・セクター・地合いトリガー）
- `questions` 確認したいこと（あれば）
- `system` システム側の宿題
- `privacy` このページについて（公開・免責・データ時点）

### 公開ページのルール

- **保有数量・損益額・口座情報・個人情報は書かない。**
- 銘柄名・コード・OCO水準・候補・作戦は書いてよい。
- 「保有中」と書く場合は、数量ではなく「保有中」まで。
- 投資助言ではない旨と、データ時点・サンプル不足を明記する。

### インデックス更新

`docs/weekend/index.json` の `notes` の**先頭**に次を追加してください。

```json
{
  "id": "2026-10-22-1512",
  "created_at": "2026-10-22T15:12:31+09:00",
  "data_date": "2026-10-21",
  "title": "作戦ノート 2026-10-22 15:12",
  "summary": "1行の要約"
}
```

## 5. 検証

次を実行してください。

```powershell
Get-Content "docs/weekend/{timestamp}.json" -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
Get-Content "docs/weekend/index.json" -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
git diff --check
```

可能ならローカルHTTPサーバーで `weekend.html` を開き、ノートが切り替わることを確認する。

## 6. コミットとデプロイ

**ステージするのは作戦ノートとインデックスだけ**にしてください（この実行でコード変更をしていない場合）。

```powershell
git add "docs/weekend/{timestamp}.json" "docs/weekend/index.json"
$staged = git diff --cached --name-only
if ($staged -match "tradehistory") { throw "取引履歴がステージされています。中止します。" }
git diff --cached --stat
git commit -m "作戦ノート {timestamp} を追加"
git push origin main
```

- `rejected` のときは `git pull --rebase origin main` してから再push。
- push後、GitHub Pages の反映を確認する（通常30〜60秒）。
- 反映URL: `https://mrkm3845-web.github.io/Stock_app/weekend.html?date={timestamp}`

## 7. 最後に報告する

- 作戦ノートのURL
- データ基準日と取引履歴の最終約定日
- 保有の要約（公開してよい範囲）
- 今日やること3つ
- あいまいで確認したいこと（あれば）

**取引履歴CSVは、読むだけでコミットしない。** これを最優先で守ってください。
