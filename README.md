# Stock_app — 日本株スクリーナー（スコア + AI分析 + バックテスト）

日本株（プライム／スタンダード 約3,100銘柄）を毎日スキャンし、**技術スコアで候補を絞り、AI（Gemini / DeepSeek 切替可）が順位付け**します。AI入力には **Google News RSS の材料**を注入。AIが `recommend` と判定した銘柄のみを**推奨**として提示し、無理に件数は埋めません（該当が無い日は「本日は推奨なし」）。バックテストによる検証と、`docs/strategy_params.json` への**ガード付き自動反映**までを単一リポジトリで行います。

- ビューア: `https://mrkm3845-web.github.io/Stock_app/`
- **まず読む**: [`PROJECT_STATUS.md`](PROJECT_STATUS.md)（いま何が実装済みか）＋ [`back_tester/README.md`](back_tester/README.md)（検証の仕組み）
- コミュニケーションは**日本語のみ**（[`AGENTS.md`](AGENTS.md)）。

---

## 主な構成

| 場所 | 内容 |
| :--- | :--- |
| [`main8.py`](main8.py) | 現行スクリーナー（Stage1 技術スコア → Stage2 AI 順位付け） |
| [`common/`](common/) | 共通モジュール（特徴量・スコア・パラメータ読込） |
| [`docs/`](docs/) | フロント（`index.html` / `journal.html` / `guide.html`）と出力JSON・`strategy_params.json` |
| [`back_tester/`](back_tester/) | ローリングウォークフォワード検証・シグナル研究・自動反映 |
| [`.github/workflows/`](.github/workflows/) | 技術スクリーニング（平日5回）／AI分析（20:17 JST）／バックテスト（月次）／Discord通知（AI実行時） |

---

## スコア（Phase 2: トレンド合成）

| 成分 | 重み |
| :--- | ---: |
| 52週高値からの位置 | 30 |
| 200日線からの乖離 | 25 |
| 200日線の傾き（20営業日） | 25 |
| 120日リターン | 20 |

旧成分（日足GC・出来高増加率・週足トレンド）は研究で逆効果のため廃止しています。詳細は [`PROJECT_STATUS.md`](PROJECT_STATUS.md) §3。

---

## クイックスタート

```bash
# スクリーナー（技術のみ）
uv run --no-project --python 3.11 --with pandas --with numpy --with requests --with yfinance --with openpyxl --with xlrd python main8.py

# スクリーナー（AI分析）
uv run --no-project --python 3.11 --with pandas --with numpy --with requests --with yfinance --with openpyxl --with xlrd python main8.py --ai

# バックテスト
uv run --no-project --python 3.11 --with pandas --with numpy --with requests --with yfinance --with openpyxl --with xlrd python back_tester/backtest_rolling_walkforward.py
```

実行方法・出力・既知の制約・残タスクは [`PROJECT_STATUS.md`](PROJECT_STATUS.md) を参照してください。
