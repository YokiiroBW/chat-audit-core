param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$OutputName = "chat-audit-wechat-tray",
    [string]$InstallerName = "ChatAuditWechatTraySetup.ps1"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

& .\scripts\build_wechat_tray.ps1 -Python $Python -OutputName $OutputName

$DistRoot = Join-Path $RepoRoot "dist\$OutputName"
$InstallerWork = Join-Path $RepoRoot ".tmp\wechat-tray-installer"
$PayloadZip = Join-Path $InstallerWork "payload.zip"
$InstallerPath = Join-Path $RepoRoot "dist\$InstallerName"

if (Test-Path -LiteralPath $InstallerWork) {
    $Resolved = Resolve-Path -LiteralPath $InstallerWork
    if ($Resolved.Path -notlike "$RepoRoot*") {
        throw "Refusing to clean path outside repo: $Resolved"
    }
    Remove-Item -LiteralPath $Resolved.Path -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $InstallerWork | Out-Null

Compress-Archive -Path (Join-Path $DistRoot "*") -DestinationPath $PayloadZip -Force
$PayloadBase64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($PayloadZip))

$InstallerTemplate = @'
$ErrorActionPreference = "Stop"
$PayloadBase64 = "__PAYLOAD_BASE64__"
$InstallRoot = Join-Path $env:LOCALAPPDATA "ChatAuditWechatTray"
$AppRoot = Join-Path $InstallRoot "app"
$Payload = Join-Path $env:TEMP ("chat-audit-wechat-tray-" + [guid]::NewGuid().ToString("N") + ".zip")

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
[System.IO.File]::WriteAllBytes($Payload, [Convert]::FromBase64String($PayloadBase64))
if (Test-Path -LiteralPath $AppRoot) {
    $Resolved = Resolve-Path -LiteralPath $AppRoot
    if ($Resolved.Path -notlike "$env:LOCALAPPDATA\ChatAuditWechatTray*") {
        throw "Refusing to replace path outside ChatAuditWechatTray: $Resolved"
    }
    Remove-Item -LiteralPath $Resolved.Path -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $AppRoot | Out-Null
Expand-Archive -LiteralPath $Payload -DestinationPath $AppRoot -Force
Remove-Item -LiteralPath $Payload -Force

$ConfigPath = Join-Path $InstallRoot "config.json"
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    @{
        nas_url = "http://192.168.31.210:8000"
        token = "replace-with-operator-token"
        account_id = "wxid_xxx"
        account_name = "微信采集账号"
        auto_download_media = $true
        autostart = $true
        paused = $false
        retry_interval_seconds = 10
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
}

$ExePath = Join-Path $AppRoot "chat-audit-wechat-tray.exe"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Set-ItemProperty -Path $RunKey -Name "ChatAuditWechatTray" -Value ('"' + $ExePath + '"')

$ShortcutCmd = Join-Path $InstallRoot "打开配置.cmd"
("@echo off", "notepad `"$ConfigPath`"") | Set-Content -LiteralPath $ShortcutCmd -Encoding ASCII

Write-Host "ChatAudit 微信托盘采集器已安装到: $AppRoot"
Write-Host "配置文件: $ConfigPath"
Write-Host "请先填写 token 和 account_id，然后运行: $ExePath"
'@
$InstallerTemplate.Replace("__PAYLOAD_BASE64__", $PayloadBase64) | Set-Content -LiteralPath $InstallerPath -Encoding UTF8

if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "Installer was not created: $InstallerPath"
}

$Hash = Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath
$ManifestPath = Join-Path $RepoRoot "dist\$InstallerName.manifest.json"
$Manifest = [ordered]@{
    name = $InstallerName
    app = $OutputName
    installer_type = "self_extracting_powershell"
    sha256 = $Hash.Hash.ToLowerInvariant()
    built_at = (Get-Date).ToUniversalTime().ToString("o")
}
$Manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8

Write-Host "Built installer $InstallerPath"
Write-Host "Wrote $ManifestPath"
