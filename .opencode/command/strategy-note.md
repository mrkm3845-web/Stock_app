---
description: 最新の取引履歴と市場データから、その時点の作戦ノートと作戦台帳を一発で作る
agent: build
---

# 作戦ノート・作戦台帳を作る

このリポジトリの **`prompts/strategy_note.md`** を読み、その手順に従ってください。
opencode 以外のハーネス（Claude Code / Codex / Cursor / Gemini CLI など）でも、
同じ `prompts/strategy_note.md` と `python tools/prepare_strategy_note.py` を使えます。

ユーザーの追加指示（空でもよい）: `$ARGUMENTS`

## 最低限の実行順

1. `git fetch origin main` と `git status --porcelain`
2. 価格・スコア・AIの鮮度チェック（古ければ更新。数分かかる）
   ```bash
   uv run --no-project --python 3.11 --with pandas_market_calendars python tools/refresh_data_if_stale.py --refresh
   ```
   - AIキーがなくてAIが古いままなら、その旨をノートに明記し、スコアと台帳を優先
   - 今すぐAIも更新したいときは、今日の作戦ページの「データ更新（AI）」を案内
3. `python tools/prepare_strategy_note.py`
4. `tools/out/strategy_context.json` を読む
5. あいまいなら質問する（AGENTS.md の質問ルール）
6. `docs/weekend/{timestamp}.json` と `docs/strategy/active.json` を作る
7. `docs/weekend/index.json` と `docs/strategy/status.json` / `events.json` を更新
8. JSON検証、`git add`（取引履歴は除外）、コミット、push
9. 今日の作戦URLと作戦ノートURLを報告

**`tradehistory(JP)_*.csv` は絶対にコミットしない。**
