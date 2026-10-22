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
2. `python tools/prepare_strategy_note.py`
3. `tools/out/strategy_context.json` を読む
4. あいまいなら質問する（AGENTS.md の質問ルール）
5. `docs/weekend/{timestamp}.json` と `docs/strategy/active.json` を作る
6. `docs/weekend/index.json` と `docs/strategy/status.json` / `events.json` を更新
7. JSON検証、`git add`（取引履歴は除外）、コミット、push
8. 今日の作戦URLと作戦ノートURLを報告

**`tradehistory(JP)_*.csv` は絶対にコミットしない。**
