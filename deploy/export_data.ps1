<#
.SYNOPSIS
    Bundle the bot's runtime state (env files, SQLite DBs, logs) into one
    tar.gz for transfer to the Linux VM.

.DESCRIPTION
    Code is NOT included - the VM clones it from GitHub. Only the files that
    git ignores and the bots need at runtime are packed:
        .env, .env.test, db/*.db (+ -wal/-shm), logs/, backtest/historic_data/*.duckdb

    Refuses to run while a bot process is still running from this checkout,
    because copying a live SQLite file can capture a half-written state.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\export_data.ps1
    powershell -ExecutionPolicy Bypass -File deploy\export_data.ps1 -SkipArchivedLogs
#>
param(
    [string]$Output = (Join-Path $HOME "calendar-bot-data.tar.gz"),
    # Skip logs/paper, logs/test and "logs/old logs" (old servy stderr captures, ~500 MB)
    [switch]$SkipArchivedLogs
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

# --- Refuse if a bot is still writing to the DBs -----------------------------
$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
    Where-Object { $_.CommandLine -match "\bbot\b" }
if ($running) {
    $running | ForEach-Object { Write-Host "  PID $($_.ProcessId): $($_.CommandLine)" }
    throw "A bot process is still running. Stop it (and its service) before exporting."
}

# --- Collect the items that exist ---------------------------------------------
$items = @()
foreach ($f in @(".env", ".env.test")) {
    if (Test-Path (Join-Path $repo $f)) { $items += $f } else { Write-Warning "missing $f" }
}
$items += Get-ChildItem (Join-Path $repo "db") -File |
    Where-Object { $_.Name -match "\.db(-wal|-shm)?$" } |
    ForEach-Object { "db/$($_.Name)" }
$items += Get-ChildItem (Join-Path $repo "backtest/historic_data") -File -Filter "*.duckdb*" -ErrorAction SilentlyContinue |
    ForEach-Object { "backtest/historic_data/$($_.Name)" }

if ($SkipArchivedLogs) {
    $items += Get-ChildItem (Join-Path $repo "logs") -File | ForEach-Object { "logs/$($_.Name)" }
} else {
    $items += "logs"
}

Write-Host "Packing into $Output :"
$items | ForEach-Object { Write-Host "  $_" }

# Windows 10+ ships bsdtar as tar.exe
& tar.exe -czf $Output -C $repo @items
if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }

$size = "{0:N1} MB" -f ((Get-Item $Output).Length / 1MB)
$hash = (Get-FileHash $Output -Algorithm SHA256).Hash.ToLower()
Write-Host ""
Write-Host "Done: $Output ($size)"
Write-Host "sha256: $hash"
Write-Host "Copy it to the VM, e.g.:  scp `"$Output`" <user>@<vm-host>:~/"
