[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$runtimeDir = Join-Path $root ".runtime"
$logDir = Join-Path $runtimeDir "logs"
$pidFile = Join-Path $runtimeDir "pids.json"
$langgraphExe = Join-Path $backendDir ".venv\Scripts\langgraph.exe"
$nextExe = Join-Path $frontendDir "node_modules\.bin\next.CMD"
$nextBuild = Join-Path $frontendDir ".next"

function Test-Port {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return $task.Wait(500) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-TrackedProcess {
    if (-not (Test-Path $pidFile)) {
        return $false
    }

    try {
        $tracked = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        foreach ($processId in @($tracked.backendPid, $tracked.frontendPid)) {
            if ($processId -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
                return $true
            }
        }
    }
    catch {
        return $false
    }
    return $false
}

if (Test-TrackedProcess) {
    throw "xu-agent is already running. Use .\stop.ps1 before starting it again."
}
if (Test-Port 2024) {
    throw "Port 2024 is already in use. Stop the existing backend first."
}
if (Test-Port 3000) {
    throw "Port 3000 is already in use. Stop the existing frontend first."
}
if (-not (Test-Path $langgraphExe)) {
    throw "Backend environment is missing. Run .\build.ps1 first."
}
if (-not (Test-Path $nextBuild)) {
    throw "Frontend production build is missing. Run .\build.ps1 first."
}
if (-not (Test-Path $nextExe)) {
    throw "Frontend dependencies are missing. Run .\build.ps1 first."
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Some launchers inject both "Path" and "PATH". Windows PowerShell 5
# Start-Process treats them as duplicate dictionary keys and fails. Normalize
# only this script process; user and system environment variables are untouched.
$processPath = [Environment]::GetEnvironmentVariable("Path", "Process")
if ($processPath) {
    [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
    [Environment]::SetEnvironmentVariable("Path", $processPath, "Process")
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "Starting backend..."
$backendProcess = Start-Process `
    -FilePath $langgraphExe `
    -ArgumentList @("dev", "--no-reload", "--host", "127.0.0.1", "--port", "2024") `
    -WorkingDirectory $backendDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "backend.out.log") `
    -RedirectStandardError (Join-Path $logDir "backend.err.log") `
    -PassThru

$env:LANGGRAPH_API_URL = "http://127.0.0.1:2024"
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:3000/api"
$env:NEXT_PUBLIC_ASSISTANT_ID = "agent"

Write-Host "Starting frontend..."
$frontendProcess = Start-Process `
    -FilePath $nextExe `
    -ArgumentList @("start", "--hostname", "127.0.0.1", "--port", "3000") `
    -WorkingDirectory $frontendDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "frontend.out.log") `
    -RedirectStandardError (Join-Path $logDir "frontend.err.log") `
    -PassThru

$processInfo = [ordered]@{
    backendPid = $backendProcess.Id
    frontendPid = $frontendProcess.Id
    startedAt = (Get-Date).ToString("o")
    url = "http://127.0.0.1:3000"
}
$processInfo | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8

Start-Sleep -Seconds 3
if ($backendProcess.HasExited -or $frontendProcess.HasExited) {
    & (Join-Path $root "stop.ps1")
    throw "A service exited during startup. Check logs in $logDir"
}

Write-Host ""
Write-Host "xu-agent is running at http://127.0.0.1:3000" -ForegroundColor Green
Write-Host "Logs: $logDir"
Write-Host "Stop: .\stop.ps1"

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:3000"
}
