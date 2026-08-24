<#
.SYNOPSIS
    Prepare this NAS-hosted checkout so the Node/Rust toolchain works.

.DESCRIPTION
    The repo lives on an SMB share (\\192.168.1.10\emperor mapped to Z:). Samba
    creates files with mode rw and no execute bit, so Windows sees (R,W) with no
    X and refuses to either launch an .exe or LoadLibrary a .node/.dll from the
    share - which is what breaks "npm install" and "vite build" with
    "Access is denied" / ERR_DLOPEN_FAILED.

    Three things are worth knowing, all established by testing on this share:

      * icacls <file> /grant "Everyone:(RX)" works per file. The recursive /T
        form reports success but the ACE does not stick, so this script grants
        one file at a time. There are only a handful of native binaries in the
        dependency tree, so that is cheap.
      * npm's postinstall scripts run binaries, so the install has to happen
        with --ignore-scripts first; the grant comes after, and nothing in this
        project needs those scripts to have run.
      * Junctions and symlinks cannot be created on the share at all, so
        redirecting node_modules to local disk is not an option. Only the cargo
        target directory can move, via CARGO_TARGET_DIR.

    The durable fix belongs on the NAS. Give the share a create mask that keeps
    the execute bit, in smb.conf:

        create mask    = 0775
        directory mask = 0775

    With that set, plain "npm install" works and -GrantExecute becomes
    unnecessary.

.PARAMETER BuildRoot
    Local directory for the cargo target tree, which is far too write-heavy to
    live on SMB.

.PARAMETER SkipInstall
    Only fix permissions; do not touch node_modules.

.PARAMETER Verify
    Run a production build at the end to prove the toolchain works.

.PARAMETER Persist
    Set CARGO_TARGET_DIR as a user environment variable so new shells inherit it.

.EXAMPLE
    .\dev-setup.ps1 -Verify
    .\dev-setup.ps1 -Persist
#>
[CmdletBinding()]
param(
    [string]$BuildRoot = "$env:LOCALAPPDATA\Sitekeeper-build",
    [switch]$SkipInstall,
    [switch]$Verify,
    [switch]$Persist
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$frontend = Join-Path $repo 'frontend'

function Say($message) { Write-Host "  $message" }

Write-Host "`nSitekeeper dev setup" -ForegroundColor Cyan
Write-Host ("=" * 46)
Say "repo       : $repo"
Say "build root : $BuildRoot"

# ---------------------------------------------------------------- 1. cargo
Write-Host "`n[1/4] cargo target directory" -ForegroundColor Yellow
$cargoTarget = Join-Path $BuildRoot 'cargo-target'
if (-not (Test-Path $cargoTarget)) { New-Item -ItemType Directory -Force $cargoTarget | Out-Null }
$env:CARGO_TARGET_DIR = $cargoTarget
Say "CARGO_TARGET_DIR -> $cargoTarget (this session)"
if ($Persist) {
    [Environment]::SetEnvironmentVariable('CARGO_TARGET_DIR', $cargoTarget, 'User')
    Say "persisted for your user account"
} else {
    Say "pass -Persist to keep it for new shells"
}

# ---------------------------------------------------------------- 2. install
Write-Host "`n[2/4] npm install" -ForegroundColor Yellow
if ($SkipInstall) {
    Say "skipped (-SkipInstall)"
} else {
    Push-Location $frontend
    try {
        # --ignore-scripts because postinstall would try to execute a binary
        # that does not yet carry the execute bit (granted in the next step).
        & npm install --ignore-scripts --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm install failed with exit code $LASTEXITCODE" }
        Say "packages installed"
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------- 3. execute bit
Write-Host "`n[3/4] execute permission on native binaries" -ForegroundColor Yellow
$searchRoots = @(
    (Join-Path $frontend 'node_modules'),
    (Join-Path $frontend 'src-tauri\binaries')
) | Where-Object { Test-Path $_ }

$binaries = @()
foreach ($root in $searchRoots) {
    $binaries += Get-ChildItem $root -Recurse -File -Include *.exe, *.dll, *.node -ErrorAction SilentlyContinue
}

if (-not $binaries.Count) {
    Say "no native binaries found yet"
} else {
    $granted = 0
    foreach ($binary in $binaries) {
        # Per file: the recursive form does not take on this share.
        & icacls $binary.FullName /grant "Everyone:(RX)" /C /Q *> $null
        if ((& icacls $binary.FullName) -match 'Everyone:\(RX\)') { $granted++ }
        else { Write-Warning "could not grant RX on $($binary.Name)" }
    }
    Say "$granted of $($binaries.Count) binaries are now executable"
    $binaries | ForEach-Object { Say "  - $($_.Name)" }
}

# ---------------------------------------------------------------- 4. verify
Write-Host "`n[4/4] verify" -ForegroundColor Yellow
if ($Verify) {
    Push-Location $frontend
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "the frontend build failed" }
        Say "frontend builds from the share"
    } finally {
        Pop-Location
    }
} else {
    Say "skipped; pass -Verify to run a real build"
}

Write-Host "`nReady." -ForegroundColor Green
Write-Host @"

  Qt app    :  python main.py
  Backend   :  python backend\run.py --port 8766
  Frontend  :  cd frontend; npm run dev
  Desktop   :  cd frontend; npm run tauri dev
               To reuse a backend you started yourself instead of spawning the
               sidecar, set these first:
                 `$env:MYSQLRUNNER_DEV_BASE  = 'http://127.0.0.1:8766'
                 `$env:MYSQLRUNNER_DEV_TOKEN = ''

  Re-run this script after any npm install: new files arrive without the
  execute bit until the NAS share gets a create mask (see the script header).

"@
