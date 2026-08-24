<#
.SYNOPSIS
    Freeze the Python backend into the sidecar binary the Tauri shell spawns.

.DESCRIPTION
    Tauri's externalBin expects a file named with the Rust target triple, so the
    frozen exe is copied to

        frontend/src-tauri/binaries/mysqlrunner-backend-<triple>.exe

    PyInstaller's work and dist directories are kept on local disk: they involve
    a great many small writes, which is painfully slow over SMB, and the exe it
    produces has to be runnable (see dev-setup.ps1 for why that matters here).
    The finished binary is copied back to the share and granted RX.

.PARAMETER BuildRoot
    Local scratch directory for the PyInstaller work/dist trees.

.PARAMETER Triple
    Rust target triple. Defaults to the 64-bit MSVC target.

.EXAMPLE
    .\build-sidecar.ps1
#>
[CmdletBinding()]
param(
    [string]$BuildRoot = "$env:LOCALAPPDATA\Sitekeeper-build\sidecar",
    [string]$Triple = "x86_64-pc-windows-msvc",
    [string]$Python = "C:\Python313\python.exe"
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$work = Join-Path $BuildRoot 'work'
$dist = Join-Path $BuildRoot 'dist'
$target = Join-Path $repo 'frontend\src-tauri\binaries'

function Say($message) { Write-Host "  $message" }

Write-Host "`nBuilding the backend sidecar" -ForegroundColor Cyan
Write-Host ("=" * 46)
Say "python : $Python"
Say "triple : $Triple"
Say "scratch: $BuildRoot"

foreach ($dir in @($work, $dist, $target)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
}

# A backend left running from an earlier build (or from the desktop app) keeps a
# handle on the exe, and PyInstaller then fails with Access is denied.
$running = Get-Process -Name mysqlrunner-backend -ErrorAction SilentlyContinue
if ($running) {
    Say "stopping $($running.Count) running backend process(es) first"
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# ---------------------------------------------------------------- 1. freeze
Write-Host "`n[1/3] pyinstaller" -ForegroundColor Yellow
Push-Location $repo
try {
    & $Python -m PyInstaller --noconfirm --workpath $work --distpath $dist "backend\mysqlrunner-backend.spec"
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

$built = Join-Path $dist 'mysqlrunner-backend.exe'
if (-not (Test-Path $built)) { throw "expected $built but it is not there" }
$sizeMb = [math]::Round((Get-Item $built).Length / 1MB, 1)
Say "built ($sizeMb MB)"

# ---------------------------------------------------------------- 2. smoke test
Write-Host "`n[2/3] smoke test" -ForegroundColor Yellow
$port = 8799
$token = 'sidecar-smoke-token'
# Point the child at a throwaway profile and keep it away from the credential
# store: a smoke test has no business touching the real vault. (The frozen exe
# bundles its own dependencies, so redirecting APPDATA cannot break its imports.)
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) "mysqlrunner-smoke-$(Get-Random)"
New-Item -ItemType Directory -Force $sandbox | Out-Null
$previousAppData = $env:APPDATA
$env:APPDATA = $sandbox
$env:MYSQLRUNNER_NO_KEYRING = '1'
$env:MYSQLRUNNER_PORT = $port
$env:MYSQLRUNNER_TOKEN = $token
# Capture the child's streams: the exe is windowed, so without this a startup
# failure is completely silent and the only symptom is a timeout.
$childOut = Join-Path $sandbox 'stdout.log'
$childErr = Join-Path $sandbox 'stderr.log'
$proc = Start-Process -FilePath $built -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $childOut -RedirectStandardError $childErr
try {
    $ok = $false
    foreach ($attempt in 1..40) {
        Start-Sleep -Milliseconds 500
        try {
            $response = Invoke-RestMethod "http://127.0.0.1:$port/health" -Headers @{ 'X-Sitekeeper-Token' = $token } -TimeoutSec 2
            if ($response.status -eq 'ok') { $ok = $true; break }
        } catch { }
    }
    if (-not $ok) {
        Write-Host "  the backend never answered; its own output follows" -ForegroundColor Red
        foreach ($log in @($childErr, $childOut)) {
            if ((Test-Path $log) -and (Get-Item $log).Length) {
                Write-Host "  --- $(Split-Path $log -Leaf) ---"
                Get-Content $log -Tail 30 | ForEach-Object { Write-Host "    $_" }
            }
        }
        throw "the frozen backend never answered /health"
    }
    Say "/health answered - the frozen backend runs"
} finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    # Belt and braces: make sure nothing survives to lock the exe next time.
    Get-Process -Name mysqlrunner-backend -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500
    $env:APPDATA = $previousAppData
    Remove-Item Env:MYSQLRUNNER_NO_KEYRING -ErrorAction SilentlyContinue
    if ($ok) {
        Remove-Item $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "  logs kept in $sandbox"
    }
}

# ---------------------------------------------------------------- 3. publish
Write-Host "`n[3/3] publish to src-tauri\binaries" -ForegroundColor Yellow
$final = Join-Path $target "mysqlrunner-backend-$Triple.exe"
Copy-Item $built $final -Force
# The share strips the execute bit, and Tauri has to be able to spawn this.
& icacls $final /grant "Everyone:(RX)" /C /Q *> $null
if ((& icacls $final) -match 'Everyone:\(RX\)') {
    Say "copied and made executable"
} else {
    Write-Warning "copied, but could not grant RX - the shell may fail to spawn it"
}
Say $final

Write-Host "`nDone." -ForegroundColor Green
Write-Host "`n  Now:  cd frontend; npm run tauri dev`n"
