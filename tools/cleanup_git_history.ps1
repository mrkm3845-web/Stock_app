<#
.SYNOPSIS
  Stock_app の Git 履歴を掃除して .git の容量を解放する（年1回のメンテ用）。

.DESCRIPTION
  削除コミットでは過去の大きなファイルは .git に残るため容量は減りません。
  このスクリプトは git filter-repo で docs/history と data/stocks.db を
  「全履歴」から取り除き、リポジトリを軽くします。

  影響: コミットSHAが変わります。実行後は git push --force origin main で反映。
  必ずミラーcloneのバックアップを作成します（-SkipBackup で省略可）。

.PARAMETER WhatIf
  何も書き換えず、対象と現在のサイズだけ表示する。

.PARAMETER SkipBackup
  バックアップ（ミラーclone）を作成しない（非推奨）。

.PARAMETER BackupDir
  バックアップ先ディレクトリ（既定: リポジトリの親ディレクトリ）。

.PARAMETER Push
  書き換え後に git push --force origin main まで実行する（確認あり）。

.EXAMPLE
  .\tools\cleanup_git_history.ps1 -WhatIf
  .\tools\cleanup_git_history.ps1
  git push --force origin main
#>
[CmdletBinding()]
param(
    [switch]$WhatIf,
    [switch]$SkipBackup,
    [string]$BackupDir,
    [switch]$Push
)

$ErrorActionPreference = 'Stop'

function Get-GitSizeMB {
    param([string]$RepoRoot)
    $gitDir = Join-Path $RepoRoot '.git'
    if (-not (Test-Path $gitDir)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $gitDir -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    if (-not $sum) { return 0 }
    return [math]::Round($sum / 1MB, 2)
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

if (-not (Test-Path (Join-Path $repoRoot '.git'))) {
    Write-Error "Gitリポジトリが見つかりません: $repoRoot"
    exit 1
}
if (-not $BackupDir) { $BackupDir = Split-Path -Parent $repoRoot }

$targets = @('docs/history', 'data/stocks.db')

Write-Host "==== Git履歴 掃除 ====" -ForegroundColor Cyan
Write-Host "リポジトリ     : $repoRoot"
Write-Host "削除対象(履歴) : $($targets -join ', ')"
Write-Host (".git サイズ    : {0} MB" -f (Get-GitSizeMB $repoRoot))

# 未コミット変更の確認
Push-Location $repoRoot
try {
    $dirty = (& git status --porcelain)
    if ($dirty) {
        Write-Warning "未コミットの変更があります。filter-repo は失敗する可能性があります。先に commit / stash してください。"
    }
} finally { Pop-Location }

if ($WhatIf) {
    Write-Host "[WhatIf] ここで終了します（何も変更していません）。" -ForegroundColor Yellow
    exit 0
}

# バックアップ（ミラーclone）
if (-not $SkipBackup) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupPath = Join-Path $BackupDir "Stock_app_backup_$stamp.git"
    Write-Host "バックアップ作成: $backupPath" -ForegroundColor Green
    & git clone --mirror $repoRoot $backupPath
    if ($LASTEXITCODE -ne 0) { Write-Error "バックアップ作成に失敗しました。中止します。"; exit 1 }
}

# filter-repo の実行方法を決定
$uv = Get-Command uv -ErrorAction SilentlyContinue
$gitfr = Get-Command git-filter-repo -ErrorAction SilentlyContinue

$frArgs = @('--force')
foreach ($t in $targets) { $frArgs += @('--path', $t); }
$frArgs += '--invert-paths'

Push-Location $repoRoot
try {
    if ($uv) {
        Write-Host "git filter-repo を uv 経由で実行します..." -ForegroundColor Green
        & uv tool run git-filter-repo @frArgs
    } elseif ($gitfr) {
        Write-Host "git filter-repo を実行します..." -ForegroundColor Green
        & git filter-repo @frArgs
    } else {
        Write-Error "git filter-repo が見つかりません。次でインストールしてください: `n  uv tool install git-filter-repo`n（または pip install git-filter-repo）"
        exit 1
    }
    if ($LASTEXITCODE -ne 0) { Write-Error "filter-repo が失敗しました。バックアップから復元できます: $backupPath"; exit 1 }

    # filter-repo は origin を外すため再設定
    $remote = (& git remote get-url origin 2>$null)
    if (-not $remote) {
        & git remote add origin "https://github.com/mrkm3845-web/Stock_app.git"
        Write-Host "origin を再設定しました。" -ForegroundColor Green
    }

    # GC
    Write-Host "GC 実行中..." -ForegroundColor Green
    & git reflog expire --expire=now --all | Out-Null
    & git gc --prune=now --aggressive | Out-Null
} finally { Pop-Location }

Write-Host ("`n.git サイズ(後): {0} MB" -f (Get-GitSizeMB $repoRoot)) -ForegroundColor Cyan

if ($Push) {
    Write-Host "`nリモートへ force push します。よろしいですか？ (y/N)" -ForegroundColor Yellow
    $ans = Read-Host
    if ($ans -eq 'y' -or $ans -eq 'Y') {
        Push-Location $repoRoot
        try { & git push --force origin main } finally { Pop-Location }
    } else {
        Write-Host "push をスキップしました。後で `git push --force origin main` を実行してください。" -ForegroundColor Yellow
    }
} else {
    Write-Host "`n次: 内容を確認して 'git push --force origin main' で反映してください。" -ForegroundColor Cyan
}
