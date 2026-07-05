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

Write-Host "Built $ExePath"
