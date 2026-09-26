# Gitリポジトリ容量の「掃除」手順書（年1回）

> 対象: `Stock_app`（public リポジトリ）。**アカウントやリポジトリを作り直す話ではありません。**
> 同じリポジトリのまま、`.git`（履歴）から古い大きなファイルを取り除いて容量を解放します。

## なぜ必要か（削除では解放されない）
- ファイルを削除してコミットしても、**過去のコミットには残る**ため `.git` は小さくなりません。
- 一方、**GitHub Pages の配信サイズ**（作業ツリー `docs/`）は削除で減ります。こちらは `main8.py` の
  `history_keep_days`（既定90）による自動 pruning で抑制済みです。
- 増え続けるのは **`.git` の履歴**（`docs/history/*.json` は約3MB/営業日 → 年 約750MB、`data/stocks.db` の旧版も）。
  → これを **git の履歴ごと書き換えて消す**のが「掃除」です。

## 影響・注意（必ず読む）
- **コミットID（SHA）が変わります**。force push になります。
- 実行後は、他のPCのローカルコピーは **再clone** が必要です（古いローカルは履歴が食い違う）。
- 実行前に **ミラーcloneでバックアップ** を取ります（スクリプトが自動で作成）。
- GitHub の **Actionsキャッシュは別枠**（リポジトリごと10GB、7日で自動削除）。掃除後も必要なら Actions→Caches から手動削除可。
- GitHub Pages の配信内容（`main` の作業ツリー）には影響しません。

## 前提
- Windows + PowerShell（スクリプト使用）／ `git` 導入済み ／ Python実行に `uv`（推奨）
- `git filter-repo` を使用（uv経由で都度実行可）

## 方法A: スクリプトを使う（推奨）
リポジトリ直下で:

```powershell
# 事前確認のみ（何も書き換えない）
.\tools\cleanup_git_history.ps1 -WhatIf

# 実行（バックアップ作成→履歴書き換え→GC。pushはしない）
.\tools\cleanup_git_history.ps1

# 問題なければ force push（履歴をリモートへ反映）
git push --force origin main
```

スクリプトは次を行います:
1. 現在の `.git` サイズを表示
2. ミラーcloneでバックアップ（既定: `..\Stock_app_backup_<timestamp>.git`）
3. `git filter-repo` で `docs/history` と `data/stocks.db` を**全履歴から削除**
4. `origin` を再設定
5. GC（`git reflog expire` + `git gc --prune=now`）
6. 実行後の `.git` サイズを表示

## 方法B: 手動で行う
```powershell
# 1) バックアップ（ミラー）
git clone --mirror https://github.com/mrkm3845-web/Stock_app.git ..\Stock_app_backup.git

# 2) 履歴から対象パスを除去（カレントをリポジトリ直下にして）
uvx git-filter-repo --force `
  --path docs/history --invert-paths `
  --path data/stocks.db --invert-paths

# 3) リモート再設定（filter-repo は origin を外すため）
git remote add origin https://github.com/mrkm3845-web/Stock_app.git

# 4) GC
git reflog expire --expire=now --all
git gc --prune=now --aggressive

# 5) 反映
git push --force origin main
```

## 掃除後の確認
- GitHub のリポジトリサイズ（Settings / API の `size`）が減少していること。
- `git clone` し直して、`docs/` と各HTMLが正常に表示されること。
- Actions の次回実行が成功すること。

## 頻度の目安
- 目安: **年1回**。ただし `.git` は約750MB/年 増えるため、余裕を見るなら **半年に1回**。
- さらなる抑制策（任意・未実装）:
  - 日次 `docs/history` の **gzip 化**（増加を ~1/10 に）
  - 日次履歴の **コミット停止**（外部保管。フロントの過去日ビューは失われる）
