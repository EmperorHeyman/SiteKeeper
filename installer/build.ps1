<#
    Build the Sitekeeper NSIS installer.

    Stages the payload (the fixed one-file exe + icon + license) next to the
    .nsi script and invokes makensis. Produces:
        installer\Sitekeeper-<version>-Setup.exe

    The version comes from APP_VERSION in Sitekeeper.nsi - one place to change,
    which is what stops the installer claiming a different version from the exe
    it wraps.

    Usage:  powershell -ExecutionPolicy Bypass -File installer\build.ps1
#>
param(
    [string]$Exe     = "$PSScriptRoot\..\dist_onefile_upx\Sitekeeper.exe",
    [string]$Icon    = "$PSScriptRoot\..\icon.ico",
    [string]$License = "$PSScriptRoot\..\LICENSE",
    [string]$MakeNsis = "$env:LOCALAPPDATA\tauri\NSIS\makensis.exe"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $MakeNsis)) {
    # fall back to PATH / common locations
    $cmd = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($cmd) { $MakeNsis = $cmd.Source }
    elseif (Test-Path "$env:ProgramFiles (x86)\NSIS\makensis.exe") { $MakeNsis = "$env:ProgramFiles (x86)\NSIS\makensis.exe" }
    else { throw "makensis.exe not found. Install NSIS 3.x or pass -MakeNsis <path>." }
}

$nsi = Join-Path $PSScriptRoot "Sitekeeper.nsi"
$version = (Select-String -Path $nsi -Pattern '^\s*!define\s+APP_VERSION\s+"([^"]+)"' |
    Select-Object -First 1).Matches.Groups[1].Value
if (-not $version) { throw "APP_VERSION not found in $nsi" }

$payload = Join-Path $PSScriptRoot "payload"
New-Item -ItemType Directory -Force $payload | Out-Null
Copy-Item $Exe  (Join-Path $payload "Sitekeeper.exe") -Force
Copy-Item $Icon (Join-Path $payload "icon.ico")        -Force
Copy-Item $License (Join-Path $PSScriptRoot "LICENSE.txt") -Force

Write-Host "Compiling installer with $MakeNsis ..."
& $MakeNsis /V3 (Join-Path $PSScriptRoot "Sitekeeper.nsi")
if ($LASTEXITCODE -ne 0) { throw "makensis failed ($LASTEXITCODE)" }

$out = Join-Path $PSScriptRoot "Sitekeeper-$version-Setup.exe"
Write-Host "Built: $out ($([math]::Round((Get-Item $out).Length/1MB)) MB)"
