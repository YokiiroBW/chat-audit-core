param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Spec = Join-Path $ProjectRoot "collector\packaging\chat_audit_qq_collector.spec"

& $Python -m PyInstaller --clean --noconfirm --distpath (Join-Path $ProjectRoot "dist") --workpath (Join-Path $ProjectRoot "build\collector") $Spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed"
}

if ($Installer) {
    $Compiler = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if (-not $Compiler) {
        throw "Inno Setup 6 (ISCC.exe) is required to build the installer"
    }
    & $Compiler.Source (Join-Path $ProjectRoot "collector\packaging\installer.iss")
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup build failed"
    }
}
