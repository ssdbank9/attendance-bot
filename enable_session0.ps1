<#
Move the headless TimeInBot tasks to session 0 so nothing they do can ever
draw on the interactive desktop.

Why this is a separate, elevated script: changing a scheduled task's principal
requires elevation, while swapping python.exe -> pythonw.exe and setting
Hidden does not. The pythonw swap is what actually removes the black console
window; this script is the belt-and-braces layer on top of it.

S4U ("Service for User") means "run whether the user is logged on or not"
without storing a password anywhere. The account must be a local admin, which
it is.

  Run elevated:  powershell -ExecutionPolicy Bypass -File enable_session0.ps1
  Undo:          powershell -ExecutionPolicy Bypass -File enable_session0.ps1 -Revert

TimeInBot_HolidayReminder is deliberately NOT included. It opens a real Tk
window (Confirm / -1 Day / +1 Day) that you are meant to click. On session 0
that window would render on an invisible desktop and mainloop() would block
until the task's execution limit killed it, silently losing every holiday
reminder. It stays interactive; running under pythonw.exe already means it
shows the intended popup and no console.
#>
param([switch]$Revert)

$ErrorActionPreference = 'Stop'

# Tasks with no interactive UI of their own: attendance, the Flask dashboard,
# its watchdog, and the ntfy-only tomorrow-plan notification.
$names = @(
  'TimeInBot',
  'TimeInBot_TimeOut',
  'TimeInBot_Dashboard',
  'TimeInBot_DashboardWatchdog',
  'TimeInBot_TomorrowPlan'
)

$elevated = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $elevated) {
  Write-Error "Not elevated. Re-run this from an Administrator PowerShell, or right-click > Run as administrator."
  exit 1
}

$me = "$env:USERDOMAIN\$env:USERNAME"

if ($Revert) {
  $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
  $target = 'Interactive'
} else {
  $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
  $target = 'S4U'
}

foreach ($n in $names) {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if (-not $t) { Write-Warning "$n not found - skipped"; continue }

  $t.Settings.Hidden = $true
  Set-ScheduledTask -TaskName $n -Action $t.Actions -Trigger $t.Triggers `
      -Settings $t.Settings -Principal $principal | Out-Null
  "{0,-30} -> {1}" -f $n, $target
}

''
'=== current state ==='
Get-ScheduledTask | Where-Object { $_.TaskName -like 'TimeInBot*' } | Sort-Object TaskName | ForEach-Object {
  '{0,-30} {1,-14} hidden={2,-6} logon={3}' -f `
    $_.TaskName,
    (Split-Path $_.Actions[0].Execute -Leaf),
    $_.Settings.Hidden,
    $_.Principal.LogonType
}
''
'Reminder: TimeInBot_HolidayReminder stays Interactive on purpose (Tk popup).'
