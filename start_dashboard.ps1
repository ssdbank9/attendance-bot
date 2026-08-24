$ErrorActionPreference = "Stop"

$dashboardRoot = $PSScriptRoot
$logDirectory = Join-Path $dashboardRoot "timein_logs"
$logPath = Join-Path $logDirectory "dashboard.log"
$venvPython = Join-Path $dashboardRoot ".venv-dashboard\Scripts\python.exe"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Set-Location -LiteralPath $dashboardRoot

if (Test-Path -LiteralPath $venvPython) {
    $pythonExe = $venvPython
} else {
    $pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
}

$startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath $logPath -Value "$startedAt  Dashboard launcher starting with $pythonExe"

try {
    & $pythonExe -u (Join-Path $dashboardRoot "dashboard.py") *>> $logPath
    $dashboardExitCode = $LASTEXITCODE
} catch {
    $_ | Out-String | Add-Content -LiteralPath $logPath
    $dashboardExitCode = 1
}

$stoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath $logPath -Value "$stoppedAt  Dashboard stopped with exit code $dashboardExitCode"
exit $dashboardExitCode
