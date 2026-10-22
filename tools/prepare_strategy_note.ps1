[CmdletBinding()]
param(
    [string]$OutFile = "tools/out/strategy_context.json",
    [int]$RecentCount = 30
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$warnings = New-Object System.Collections.Generic.List[string]

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        $warnings.Add("ファイルが見つかりません: $Path")
        return $null
    }
    try {
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        $warnings.Add("JSONを読めません: $Path ($($_.Exception.Message))")
        return $null
    }
}

function Read-HistoryFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        $warnings.Add("履歴が見つかりません: $Path")
        return $null
    }
    $fs = [IO.File]::OpenRead((Resolve-Path -LiteralPath $Path))
    try {
        if ($Path -like "*.gz") {
            $gz = New-Object IO.Compression.GzipStream($fs, [IO.Compression.CompressionMode]::Decompress)
            $sr = New-Object IO.StreamReader($gz, [Text.Encoding]::UTF8)
        } else {
            $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)
        }
        try {
            $txt = $sr.ReadToEnd()
        } finally {
            $sr.Close()
        }
        return ($txt | ConvertFrom-Json)
    } catch {
        $warnings.Add("履歴を読めません: $Path ($($_.Exception.Message))")
        return $null
    } finally {
        $fs.Dispose()
    }
}

function ConvertTo-Date {
    param($Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return $null }
    $dt = [datetime]::MinValue
    if ([datetime]::TryParse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$dt)) {
        return $dt
    }
    return $null
}

function ConvertTo-Num {
    param($Value)
    if ($null -eq $Value) { return 0.0 }
    $s = ([string]$Value) -replace ",", ""
    $d = 0.0
    if ([double]::TryParse($s, [Globalization.NumberStyles]::Any, [Globalization.CultureInfo]::InvariantCulture, [ref]$d)) {
        return $d
    }
    return 0.0
}

# 1. 最新の取引履歴（ルート直下）を探す。CSVは絶対にコミットしない。
$csvFile = Get-ChildItem -LiteralPath $root -Filter "tradehistory(JP)_*.csv" -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $csvFile) {
    throw "ルートに tradehistory(JP)_*.csv がありません。取引履歴を置いてから実行してください。"
}

$rows = Import-Csv -LiteralPath $csvFile.FullName -Encoding Default
if (-not $rows -or $rows.Count -eq 0) {
    throw "取引履歴が空です: $($csvFile.Name)"
}

$lots = @{}
$realized = @{}
$names = @{}
$lastTrade = $null

foreach ($r in $rows) {
    $code = [string]$r."銘柄コード"
    if ([string]::IsNullOrWhiteSpace($code)) { continue }
    $name = [string]$r."銘柄名"
    $names[$code] = $name
    $qty = [int](ConvertTo-Num $r."数量［株］")
    $price = ConvertTo-Num $r."単価［円］"
    $side = [string]$r."売買区分"
    $date = ConvertTo-Date $r."約定日"

    if (-not $lots.ContainsKey($code)) {
        $lots[$code] = New-Object System.Collections.Generic.List[object]
    }
    if (-not $realized.ContainsKey($code)) {
        $realized[$code] = [pscustomobject]@{
            code = $code
            name = $name
            qtyBought = 0
            qtySold = 0
            realizedPnl = 0.0
            lastTrade = $date
        }
    }
    $rz = $realized[$code]
    if ($date) {
        if (-not $rz.lastTrade -or $date -gt $rz.lastTrade) { $rz.lastTrade = $date }
        if (-not $lastTrade -or $date -gt $lastTrade) { $lastTrade = $date }
    }

    if ($side -eq "買付") {
        $lots[$code].Add([pscustomobject]@{ qty = $qty; price = $price; date = $date })
        $rz.qtyBought += $qty
    } elseif ($side -eq "売付") {
        $remaining = $qty
        while ($remaining -gt 0 -and $lots[$code].Count -gt 0) {
            $lot = $lots[$code][0]
            $take = [math]::Min($remaining, [int]$lot.qty)
            $rz.realizedPnl += ($price - [double]$lot.price) * $take
            $lot.qty = [int]$lot.qty - $take
            $remaining -= $take
            if ([int]$lot.qty -le 0) { $lots[$code].RemoveAt(0) }
        }
        if ($remaining -gt 0) {
            $warnings.Add("売付が買付を上回っています: $code $name / $remaining株")
        }
        $rz.qtySold += $qty
    }
}

# 2. 最新の履歴（価格・地合い）を読む
$hist = Read-HistoryFile -Path "docs/history/latest.json.gz"
if (-not $hist) { $hist = Read-HistoryFile -Path "docs/history/latest.json" }

$priceMap = @{}
if ($hist) {
    foreach ($h in $hist) { $priceMap[[string]$h.code] = $h }
}

$positions = @()
foreach ($code in $lots.Keys) {
    $openLots = @($lots[$code] | Where-Object { [int]$_.qty -gt 0 })
    if ($openLots.Count -eq 0) { continue }
    $qty = ($openLots | Measure-Object -Property qty -Sum).Sum
    $cost = (($openLots | ForEach-Object { [double]$_.qty * [double]$_.price }) | Measure-Object -Sum).Sum / $qty
    $last = $priceMap[$code]
    $lastPrice = if ($last) { [double]$last.price } else { $null }
    $pnl = if ($lastPrice) { ($lastPrice - $cost) * $qty } else { $null }
    $pnlPct = if ($lastPrice) { ($lastPrice / $cost - 1) * 100 } else { $null }
    $positions += [pscustomobject]@{
        code = $code
        name = $names[$code]
        qty = [int]$qty
        avg_cost = [math]::Round($cost, 2)
        last_price = $lastPrice
        market_value = if ($lastPrice) { [math]::Round($lastPrice * $qty, 0) } else { $null }
        unrealized_pnl = if ($null -ne $pnl) { [math]::Round($pnl, 0) } else { $null }
        unrealized_pct = if ($null -ne $pnlPct) { [math]::Round($pnlPct, 2) } else { $null }
        last_trade_date = if ($realized[$code].lastTrade) { $realized[$code].lastTrade.ToString("yyyy-MM-dd") } else { $null }
    }
}

$realizedList = @($realized.Values |
    Where-Object { $_.qtySold -gt 0 } |
    ForEach-Object {
        [pscustomobject]@{
            code = $_.code
            name = $_.name
            qty_bought = $_.qtyBought
            qty_sold = $_.qtySold
            realized_pnl = [math]::Round($_.realizedPnl, 0)
            last_trade_date = if ($_.lastTrade) { $_.lastTrade.ToString("yyyy-MM-dd") } else { $null }
        }
    } |
    Sort-Object last_trade_date -Descending)

$recentTradeRows = @($rows | Select-Object -Last $RecentCount | ForEach-Object {
    [pscustomobject]@{
        date = [string]$_.'約定日'
        code = [string]$_.'銘柄コード'
        name = [string]$_.'銘柄名'
        side = [string]$_.'売買区分'
        qty = [int](ConvertTo-Num $_.'数量［株］')
        price = ConvertTo-Num $_.'単価［円］'
    }
})

# 3. 市場データ
$rec = Read-JsonFile -Path "docs/recommendations.json"
$dates = Read-JsonFile -Path "docs/history/dates.json"
$meta = Read-JsonFile -Path "docs/history/meta.json"
$params = Read-JsonFile -Path "docs/strategy_params.json"
$weekly = Read-JsonFile -Path "docs/weekly/latest.json"
$plan = Read-JsonFile -Path "docs/weekly/plan.json"
$playbook = Read-JsonFile -Path "docs/ai_playbook.json"
$earnings = Read-JsonFile -Path "docs/earnings.json"

$breadth = $null
$sectors = @()
if ($hist) {
    $valid = @($hist | Where-Object { -not $_.excluded -and [double]$_.price -gt 0 })
    if ($valid.Count -gt 0) {
        $above200 = @($valid | Where-Object { [double]$_.ctx.dist_sma200_pct -gt 0 }).Count
        $above25 = @($valid | Where-Object { [double]$_.ctx.dist_sma25_pct -gt 0 }).Count
        $r5 = @($valid | ForEach-Object { [double]$_.ctx.ret_5d_pct } | Sort-Object)
        $r20 = @($valid | ForEach-Object { [double]$_.ctx.ret_20d_pct } | Sort-Object)
        $breadth = [pscustomobject]@{
            n = $valid.Count
            above_sma200_pct = [math]::Round($above200 / $valid.Count * 100, 1)
            above_sma25_pct = [math]::Round($above25 / $valid.Count * 100, 1)
            avg_ret5_pct = [math]::Round((($r5 | Measure-Object -Average).Average), 2)
            median_ret5_pct = if ($r5.Count -gt 0) { [math]::Round($r5[[int]($r5.Count / 2)], 2) } else { $null }
            avg_ret20_pct = [math]::Round((($r20 | Measure-Object -Average).Average), 2)
            median_ret20_pct = if ($r20.Count -gt 0) { [math]::Round($r20[[int]($r20.Count / 2)], 2) } else { $null }
        }

        $sectors = @($valid | Group-Object sector | Where-Object { $_.Count -ge 5 } | ForEach-Object {
            $g = $_.Group
            $g5 = @($g | ForEach-Object { [double]$_.ctx.ret_5d_pct })
            $g20 = @($g | ForEach-Object { [double]$_.ctx.ret_20d_pct })
            [pscustomobject]@{
                sector = $_.Name
                n = $_.Count
                avg_ret5_pct = [math]::Round((($g5 | Measure-Object -Average).Average), 2)
                avg_ret20_pct = [math]::Round((($g20 | Measure-Object -Average).Average), 2)
                above_sma25_pct = [math]::Round((@($g | Where-Object { [double]$_.ctx.dist_sma25_pct -gt 0 }).Count / $g.Count * 100), 1)
                above_sma200_pct = [math]::Round((@($g | Where-Object { [double]$_.ctx.dist_sma200_pct -gt 0 }).Count / $g.Count * 100), 1)
            }
        } | Sort-Object avg_ret5_pct)
    }
}

$pickFields = @(
    "code", "name", "sector", "price", "score", "verdict", "rank", "entry_type",
    "entry_price", "entry_zone_low", "entry_zone_high", "tp_price", "sl_price",
    "suggested_qty", "overheat_level", "rsi14", "dist_sma25_pct", "pos_52w",
    "earnings_date", "earnings_soon", "pattern_summary"
)
$picks = @()
$picksInRange = @()
if ($rec) {
    $picks = @($rec.picks | Select-Object $pickFields)
    $picksInRange = @($rec.picks_in_range | Select-Object $pickFields)
}

$earningsSoon = @()
if ($earnings -and $earnings.items) {
    $earningsSoon = @($earnings.items.PSObject.Properties |
        Where-Object { $_.Value.soon } |
        ForEach-Object {
            [pscustomobject]@{
                code = $_.Name
                date = $_.Value.date
                days_until = $_.Value.days_until
            }
        } |
        Sort-Object days_until |
        Select-Object -First 30)
}

$market = [pscustomobject]@{
    data_date = if ($rec) { $rec.date } elseif ($dates -and $dates.Count -gt 0) { $dates[0] } else { $null }
    score_version = if ($rec) { $rec.score_version } else { $null }
    regime = if ($meta) { $meta.regime } else { $null }
    portfolio = if ($params) { $params.portfolio } else { $null }
    recommendations = [pscustomobject]@{
        date = if ($rec) { $rec.date } else { $null }
        generated_at = if ($rec) { $rec.generated_at } else { $null }
        overall = if ($rec) { $rec.overall } else { $null }
        picks = $picks
        picks_in_range = $picksInRange
        range = if ($rec) { $rec.range } else { $null }
    }
    breadth = $breadth
    sectors = $sectors
    weekly = if ($weekly) {
        [pscustomobject]@{
            week = $weekly.week
            start = $weekly.start
            end = $weekly.end
            summary = $weekly.summary
            ranking = $weekly.ranking
            ai_calibration = $weekly.ai_calibration
            top_n = $weekly.top_n
            notes = $weekly.notes
            feedback = $weekly.feedback
        }
    } else { $null }
    plan = if ($plan) { $plan.plan } else { $null }
    playbook = if ($playbook) {
        [pscustomobject]@{
            generated_at = $playbook.generated_at
            stats = $playbook.stats
            lessons = $playbook.lessons
            score_ranking = $playbook.score_ranking
        }
    } else { $null }
    earnings_soon = $earningsSoon
}

$now = [DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(9))
$stamp = $now.ToString("yyyy-MM-dd-HHmm")

$out = [pscustomobject]@{
    generated_at = $now.ToString("yyyy-MM-ddTHH:mm:sszzz")
    timestamp = $stamp
    timezone = "JST"
    trade_history = [pscustomobject]@{
        file = $csvFile.Name
        last_trade_date = if ($lastTrade) { $lastTrade.ToString("yyyy-MM-dd") } else { $null }
        rows = $rows.Count
    }
    positions = @($positions | Sort-Object code)
    realized = $realizedList
    total_realized_pnl = [math]::Round((($realizedList | Measure-Object -Property realized_pnl -Sum).Sum), 0)
    recent_trades = $recentTradeRows
    market = $market
    warnings = @($warnings)
    note = "このファイルはローカル専用です。tools/out/ はコミットしません。"
}

$outPath = if ([IO.Path]::IsPathRooted($OutFile)) { $OutFile } else { Join-Path $root $OutFile }
$outDir = Split-Path -Parent $outPath
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$json = $out | ConvertTo-Json -Depth 12
[IO.File]::WriteAllText($outPath, $json, (New-Object Text.UTF8Encoding($false)))

Write-Host "作戦ノート用コンテキスト: $outPath"
Write-Host "取引履歴: $($csvFile.Name) / 最終約定: $($out.trade_history.last_trade_date)"
Write-Host "市場データ: $($market.data_date) / スコア: $($market.score_version)"
Write-Host "保有: $(if ($positions.Count) { ($positions | ForEach-Object { "$($_.code) $($_.name) $($_.qty)株" }) -join ', ' } else { 'なし' })"
if ($warnings.Count -gt 0) {
    Write-Host "注意: $($warnings.Count)件の警告があります（JSONの warnings を確認）"
}
