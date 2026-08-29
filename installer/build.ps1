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
    [string]$McpExe  = "$PSScriptRoot\..\dist_onefile_upx\sitekeeper-mcp.exe",
    [string]$Icon    = "$PSScriptRoot\..\icon.ico",
    [string]$License = "$PSScriptRoot\..\LICENSE",
    [string]$MakeNsis = "$env:LOCALAPPDATA\tauri\NSIS\makensis.exe"
)
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\..\sign.ps1"

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
# The MCP server. Shipped alongside so Claude has something to run that
# does not need a Python install or a source checkout.
if (-not (Test-Path $McpExe)) { throw "MCP executable not found: $McpExe. Run build_release.ps1 first." }
Copy-Item $McpExe (Join-Path $payload "sitekeeper-mcp.exe") -Force
Copy-Item $Icon (Join-Path $payload "icon.ico")        -Force
Copy-Item $License (Join-Path $PSScriptRoot "LICENSE.txt") -Force

Write-Host "Compiling installer with $MakeNsis ..."
& $MakeNsis /V3 (Join-Path $PSScriptRoot "Sitekeeper.nsi")
if ($LASTEXITCODE -ne 0) { throw "makensis failed ($LASTEXITCODE)" }

$out = Join-Path $PSScriptRoot "Sitekeeper-$version-Setup.exe"

# The setup is what gets double-clicked, so this is the signature that
# decides whether the elevation prompt names a publisher or says
# "Unknown publisher".
Invoke-CodeSign $out | Out-Null

# The repo lives on an SMB share whose create mask drops the execute bit, so a
# setup.exe written there cannot be launched from it - "Access is denied", from
# a file that looks perfectly normal - until it is granted read+execute. The
# recursive icacls form reports success but the ACE does not stick; per file
# does. build_release.ps1 does the same for the app exe it produces.
if ($out -match '^[A-Za-z]:' -and (Get-Item $out).PSDrive.DisplayRoot) {
    & icacls $out /grant "Everyone:(RX)" | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Granted RX on the installer (network share)" }
}

Write-Host "Built: $out ($([math]::Round((Get-Item $out).Length/1MB)) MB)"
