param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Spec = Join-Path $ProjectRoot "collector\packaging\chat_audit_qq_collector.spec"
$GuiSpec = Join-Path $ProjectRoot "collector\packaging\chat_audit_qq_collector_gui.spec"

# Some portable Python distributions contain Tcl/Tk but expose it from a path
# that Tcl's native filesystem layer cannot read during PyInstaller analysis.
# Stage the runtime in the current user's temp directory before building.
$PythonRoot = (& $Python -c "import sys; print(sys.base_prefix)").Trim()
$TclSource = Join-Path $PythonRoot "tcl"
if (Test-Path -LiteralPath (Join-Path $TclSource "tcl8.6")) {
    $TclStage = Join-Path $env:TEMP "chat-audit-collector-tcl"
    New-Item -ItemType Directory -Force -Path $TclStage | Out-Null
    Copy-Item -LiteralPath (Join-Path $TclSource "tcl8.6") -Destination $TclStage -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $TclSource "tk8.6") -Destination $TclStage -Recurse -Force
    $env:TCL_LIBRARY = Join-Path $TclStage "tcl8.6"
    $env:TK_LIBRARY = Join-Path $TclStage "tk8.6"
}

& $Python -m PyInstaller --clean --noconfirm --distpath (Join-Path $ProjectRoot "dist") --workpath (Join-Path $ProjectRoot "build\collector") $Spec
if ($LASTEXITCODE -ne 0) {
    throw "Collector CLI PyInstaller build failed"
}

& $Python -m PyInstaller --clean --noconfirm --distpath (Join-Path $ProjectRoot "dist") --workpath (Join-Path $ProjectRoot "build\collector-gui") $GuiSpec
if ($LASTEXITCODE -ne 0) {
    throw "Collector GUI PyInstaller build failed"
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
