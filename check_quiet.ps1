<#
Verify the bot stays invisible and the dashboard stays reachable.

Run this any time, non-elevated. It is the check to run after
enable_session0.ps1, after a Windows update, or whenever the phone cannot
reach the dashboard.

  powershell -ExecutionPolicy Bypass -File check_quiet.ps1
#>

$ErrorActionPreference = 'Continue'
$fail = @()

'=== scheduled tasks ==='
$tasks = Get-ScheduledTask | Where-Object { $_.TaskName -like 'TimeInBot*' } | Sort-Object TaskName
foreach ($t in $tasks) {
  $exe = Split-Path $t.Actions[0].Execute -Leaf
  '{0,-30} {1,-14} hidden={2,-6} logon={3}' -f $t.TaskName, $exe, $t.Settings.Hidden, $t.Principal.LogonType
  # python.exe as a task entry point is what draws the black console window.
  if ($exe -eq 'python.exe') { $fail += "$($t.TaskName) runs python.exe - it will flash a console window" }
  if (-not $t.Settings.Hidden) { $fail += "$($t.TaskName) is not Hidden" }
}
if (-not $tasks) { $fail += 'no TimeInBot tasks found' }

''
'=== dashboard listener ==='
$conn = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
  $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
  'bind   : {0}:5000' -f $conn.LocalAddress
  'image  : {0}' -f $proc.Path
  'window : {0}' -f $proc.MainWindowHandle
  # Firewall rules are per-executable: pythonw.exe is Blocked inbound on this
  # host, so serving from it makes the phone lose access silently.
  if ($proc.Path -like '*pythonw.exe') {
    $fail += 'dashboard is served by pythonw.exe, which is BLOCKED inbound - the phone will not reach it'
  }
  if ($proc.MainWindowHandle -ne 0) { $fail += 'dashboard process has a visible window' }
} else {
  $fail += 'nothing is listening on port 5000'
  'NOT LISTENING'
}

''
'=== reachable on each interface ==='
$ips = @('127.0.0.1')
$ips += (Get-NetIPAddress -AddressFamily IPv4 |
         Where-Object { $_.InterfaceAlias -match 'Wi-Fi|Tailscale' }).IPAddress
foreach ($ip in $ips) {
  try {
    $r = Invoke-WebRequest -Uri "http://${ip}:5000/login" -UseBasicParsing -TimeoutSec 6
    '{0,-18} HTTP {1}' -f $ip, $r.StatusCode
  } catch {
    '{0,-18} FAILED: {1}' -f $ip, $_.Exception.Message
    $fail += "dashboard unreachable on $ip"
  }
}

''
'=== inbound firewall rules for python ==='
Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like '*python*' } | ForEach-Object {
  $r = $_ | Get-NetFirewallRule
  if ($r.Direction -eq 'Inbound') {
    '{0,-6} enabled={1,-6} {2,-16} {3}' -f $r.Action, $r.Enabled, $r.Profile, $_.Program
  }
}

''
'=== visible windows from bot processes ==='
$vis = Get-Process python,pythonw,msedgedriver -ErrorAction SilentlyContinue |
       Where-Object { $_.MainWindowHandle -ne 0 }
if ($vis) {
  $vis | ForEach-Object { '{0} pid={1} window={2}' -f $_.ProcessName, $_.Id, $_.MainWindowHandle }
  $fail += 'a bot process has a visible window'
} else {
  'none - nothing is drawing on screen'
}

''
if ($fail.Count -eq 0) {
  'RESULT: PASS - invisible and reachable'
  exit 0
} else {
  'RESULT: PROBLEMS FOUND'
  $fail | ForEach-Object { '  - ' + $_ }
  exit 1
}
