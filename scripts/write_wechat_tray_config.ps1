param(
    [string]$NasUrl = "http://192.168.31.210:8000",
    [Parameter(Mandatory = $true)]
    [string]$Token,
    [Parameter(Mandatory = $true)]
    [string]$AccountId,
    [string]$AccountName = "微信采集账号",
    [string]$ConfigPath = "$env:APPDATA\ChatAuditWechatTray\config.json"
)

$ErrorActionPreference = "Stop"
$Target = [System.IO.Path]::GetFullPath($ConfigPath)
$TargetDir = Split-Path -Parent $Target
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

$Config = [ordered]@{
    nas_url = $NasUrl
    token = $Token
    account_id = $AccountId
    account_name = $AccountName
    auto_download_media = $true
    autostart = $false
    paused = $false
    retry_interval_seconds = 10
}

$Config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Target -Encoding UTF8
Write-Host "Wrote config: $Target"
