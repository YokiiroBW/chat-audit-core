param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$OutputName = "chat-audit-wechat-tray"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python executable not found: $Python"
}

& $Python -m pip install -r .\wechat_tray_adapter\requirements.txt
& $Python -m PyInstaller --noconsole --clean --name $OutputName .\wechat_tray_adapter\__main__.py

$ExePath = Join-Path $RepoRoot "dist\$OutputName\$OutputName.exe"
if (-not (Test-Path -LiteralPath $ExePath)) {
    throw "Build completed but executable was not found: $ExePath"
}

$Version = (& $Python -c "from wechat_tray_adapter.version import __version__; print(__version__)").Trim()
$Hash = Get-FileHash -Algorithm SHA256 -LiteralPath $ExePath
$ManifestPath = Join-Path $RepoRoot "dist\$OutputName\manifest.json"
$Manifest = [ordered]@{
    name = $OutputName
    version = $Version
    exe = "$OutputName.exe"
    sha256 = $Hash.Hash.ToLowerInvariant()
    built_at = (Get-Date).ToUniversalTime().ToString("o")
}
$Manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8

Write-Host "Built $ExePath"
Write-Host "Wrote $ManifestPath"
