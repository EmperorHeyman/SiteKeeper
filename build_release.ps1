$ErrorActionPreference = "Stop"

. "$PSScriptRoot\sign.ps1"

$venv = Join-Path $env:USERPROFILE ".venvs\mysqlrunner"
$workDir = "build_onefile_upx"
$distDir = "dist_onefile_upx"
$releaseDir = "release"

foreach ($d in @($workDir, "build_mcp", $distDir, $releaseDir)) {
	if (Test-Path $d -ErrorAction SilentlyContinue) {
		try { Remove-Item $d -Recurse -Force } catch { }
	}
}
New-Item -ItemType Directory -Path $releaseDir | Out-Null

$upx = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter upx.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
if (-not $upx) { throw "UPX not found. Install with: winget install --id UPX.UPX" }
$upxDir = Split-Path $upx -Parent

# Build one-file executable from local venv with UPX enabled in spec.
& "$venv\Scripts\pyinstaller.exe" .\Sitekeeper.spec --noconfirm --clean --workpath $workDir --distpath $distDir --upx-dir "$upxDir"

# And the MCP server, as its own console executable in the same folder.
# The GUI build is windowed, so it has no stdout and cannot speak a stdio
# protocol; without this there is no MCP command an installed user can
# run at all. It excludes Qt entirely, so it costs about 13 MB.
& "$venv\Scripts\pyinstaller.exe" .\SitekeeperMCP.spec --noconfirm --clean --workpath build_mcp --distpath $distDir --upx-dir "$upxDir"

$src = Join-Path $PWD "$distDir\Sitekeeper.exe"
$mcp = Join-Path $PWD "$distDir\sitekeeper-mcp.exe"

# Sign before zipping or wrapping. UPX has already run - it would strip
# a signature added earlier - and the installer copies this exact file
# into its payload, so signing here is also what puts a signature on the
# application that ends up installed, not just on the setup.
Invoke-CodeSign $src | Out-Null
Invoke-CodeSign $mcp | Out-Null

# Zip distributable for easy sharing
$zip = Join-Path $PWD "$releaseDir\Sitekeeper-win64.zip"
Compress-Archive -Path $src, $mcp -DestinationPath $zip -CompressionLevel Optimal -Force

# The project lives on an SMB share whose create mask drops the execute bit, so
# a freshly written exe cannot be launched ("Access is denied") until it is
# granted read+execute. Recursive icacls does not stick here; per file does.
# See dev-setup.ps1 for the diagnosis.
foreach ($file in @($src, $mcp)) {
    if ($file -match '^[A-Za-z]:' -and (Get-Item $file).PSDrive.DisplayRoot) {
        & icacls $file /grant "Everyone:(RX)" | Out-Null
    }
}
Write-Host "Granted RX on both exes (network share)"

Write-Host "Built exe:    $src"
Write-Host "Built mcp:    $mcp ($([math]::Round((Get-Item $mcp).Length/1MB)) MB)"
Write-Host "Built zip:    $zip"
