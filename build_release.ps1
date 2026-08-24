$ErrorActionPreference = "Stop"

$venv = Join-Path $env:USERPROFILE ".venvs\mysqlrunner"
$workDir = "build_onefile_upx"
$distDir = "dist_onefile_upx"
$releaseDir = "release"

foreach ($d in @($workDir, $distDir, $releaseDir)) {
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

# Zip distributable for easy sharing
$src = Join-Path $PWD "$distDir\Sitekeeper.exe"
$zip = Join-Path $PWD "$releaseDir\Sitekeeper-win64.zip"
Compress-Archive -Path $src -DestinationPath $zip -CompressionLevel Optimal -Force

# The project lives on an SMB share whose create mask drops the execute bit, so
# a freshly written exe cannot be launched ("Access is denied") until it is
# granted read+execute. Recursive icacls does not stick here; per file does.
# See dev-setup.ps1 for the diagnosis.
if ($src -match '^[A-Za-z]:' -and (Get-Item $src).PSDrive.DisplayRoot) {
    & icacls $src /grant "Everyone:(RX)" | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Granted RX on the exe (network share)" }
}

Write-Host "Built exe:    $src"
Write-Host "Built zip:    $zip"
