param(
    [string]$Name = "ChatAuditWechatTray"
)

$ErrorActionPreference = "Stop"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

$Existing = Get-ItemProperty -Path $RunKey -Name $Name -ErrorAction SilentlyContinue
if ($null -eq $Existing) {
    Write-Host "Startup entry not found: $Name"
    exit 0
}

Remove-ItemProperty -Path $RunKey -Name $Name
Write-Host "Removed current-user startup entry: $Name"
