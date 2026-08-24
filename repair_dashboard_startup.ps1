#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$dashboardRoot = $PSScriptRoot
$launcherPath = Join-Path $dashboardRoot "start_dashboard.ps1"
$taskName = "TimeInBot_Dashboard"
$firewallRuleName = "TimeInDashboardPort5000"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "Dashboard launcher not found: $launcherPath"
}

$taskAction = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcherPath`"" `
    -WorkingDirectory $dashboardRoot
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$taskPrincipal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $taskAction `
    -Trigger $taskTrigger `
    -Principal $taskPrincipal `
    -Settings $taskSettings `
    -Description "Starts the Flask attendance dashboard at logon." `
    -Force | Out-Null

$firewallRule = Get-NetFirewallRule -Name $firewallRuleName -ErrorAction SilentlyContinue
if ($null -eq $firewallRule) {
    New-NetFirewallRule `
        -Name $firewallRuleName `
        -DisplayName "TimeIn Dashboard - TCP 5000" `
        -Description "Allows access to the local attendance dashboard." `
        -Enabled True `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort 5000 `
        -RemoteAddress "100.64.0.0/10" `
        -Profile Any | Out-Null
} else {
    Set-NetFirewallRule `
        -Name $firewallRuleName `
        -Enabled True `
        -Direction Inbound `
        -Action Allow `
        -Profile Any | Out-Null
    $firewallRule |
        Get-NetFirewallAddressFilter |
        Set-NetFirewallAddressFilter -RemoteAddress "100.64.0.0/10" | Out-Null
}

$listener = Get-NetTCPConnection -State Listen -LocalPort 5000 -ErrorAction SilentlyContinue
if ($null -eq $listener) {
    Start-ScheduledTask -TaskName $taskName
}

Write-Host "Dashboard logon task repaired: $taskName"
Write-Host "Firewall rule enabled: TimeIn Dashboard - TCP 5000"
Write-Host "Dashboard URL: http://100.85.88.55:5000"
