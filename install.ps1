#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$InstallDir = 'C:\Attendance'
$PythonUrl  = 'https://www.python.org/ftp/python/3.13.5/python-3.13.5-amd64.exe'
$RepoZipUrl = 'https://github.com/ssdbank9/attendance-bot/archive/refs/heads/main.zip'

function Write-Step([int]$n, [string]$msg) {
    Write-Host "[$n/4] $msg" -ForegroundColor Cyan
}

# -- Step 1 - Python ---------------------------------------------------
Write-Step 1 "Checking Python installation..."

$py = $null
foreach ($cmd in 'python','py') {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match 'Python 3\.') {
            $py = $cmd
            Write-Host "  Found: $ver" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-not $py) {
    Write-Host "  Python 3 not found. Installing..." -ForegroundColor Yellow
    $installer = "$env:TEMP\python-installer.exe"
    Invoke-WebRequest -Uri $PythonUrl -OutFile $installer -UseBasicParsing
    Start-Process -FilePath $installer `
        -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_pip=1' -Wait
    Remove-Item $installer -ErrorAction SilentlyContinue

    # Refresh PATH so the current session sees the new install
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path','User')

    foreach ($cmd in 'python','py') {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match 'Python 3\.') {
                $py = $cmd
                Write-Host "  Installed: $ver" -ForegroundColor Green
                break
            }
        } catch {}
    }

    if (-not $py) {
        Write-Host "  ERROR: Python installation failed." -ForegroundColor Red
        exit 1
    }
}

# -- Step 2 - Download bot ---------------------------------------------
Write-Step 2 "Downloading attendance bot..."

$zipPath    = "$env:TEMP\attendance-bot.zip"
$extractDir = "$env:TEMP\attendance-bot-extract"

if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }

Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zipPath -UseBasicParsing
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

# GitHub zips contain one top-level directory
$innerDir = Get-ChildItem $extractDir -Directory | Select-Object -First 1

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

# Copy every file, but NEVER overwrite an existing config.json
Get-ChildItem $innerDir.FullName -Recurse | ForEach-Object {
    $rel  = $_.FullName.Substring($innerDir.FullName.Length + 1)
    $dest = Join-Path $InstallDir $rel

    if ($_.PSIsContainer) {
        if (-not (Test-Path $dest)) {
            New-Item -ItemType Directory -Path $dest -Force | Out-Null
        }
    } else {
        if ($rel -eq 'config.json' -and (Test-Path $dest)) {
            Write-Host "  Keeping existing config.json" -ForegroundColor Yellow
        } else {
            Copy-Item $_.FullName -Destination $dest -Force
        }
    }
}

Remove-Item $zipPath    -ErrorAction SilentlyContinue
Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  Bot files installed to $InstallDir" -ForegroundColor Green

# -- Step 3 - Setup wizard ---------------------------------------------
Write-Step 3 "Running setup wizard..."
Set-Location $InstallDir
& $py setup_new_user.py

# -- Step 4 - Verify ---------------------------------------------------
Write-Step 4 "Verifying installation..."

$dashOk = $false
try {
    $r = Invoke-WebRequest -Uri 'http://localhost:5000/' -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -eq 200) {
        $dashOk = $true
        Write-Host "  Dashboard: RUNNING" -ForegroundColor Green
    }
} catch {
    Write-Host "  Dashboard: NOT YET RUNNING (starts at next logon)" -ForegroundColor Yellow
}

$tasks = @(Get-ScheduledTask | Where-Object { $_.TaskName -like 'TimeInBot*' })
$n     = $tasks.Count
Write-Host "  Scheduled tasks: $n found" -ForegroundColor $(if ($n -gt 0) {'Green'} else {'Yellow'})

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "    INSTALLATION COMPLETE"                      -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Bot folder:  $InstallDir"                     -ForegroundColor White
Write-Host "  Dashboard:   http://localhost:5000/"           -ForegroundColor White
Write-Host ""
Write-Host "  TIP: Install the ntfy app on your phone"      -ForegroundColor Cyan
Write-Host "       for push notifications."                  -ForegroundColor Cyan
Write-Host ""
