param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,
    [string]$Name = "ChatAuditWechatTray"
)

$ErrorActionPreference = "Stop"
$ResolvedExe = Resolve-Path -LiteralPath $ExePath
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$Command = '"' + $ResolvedExe.Path + '"'

Set-ItemProperty -Path $RunKey -Name $Name -Value $Command
Write-Host "Installed current-user startup entry: $Name -> $Command"
