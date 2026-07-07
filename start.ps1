[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [int]$BackendTimeoutSeconds = 90,
    [int]$FrontendTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$frontendDir = Join-Path $root "web"
$runtimeDir = Join-Path $root ".runtime"
$logDir = Join-Path $runtimeDir "logs"
$pidFile = Join-Path $runtimeDir "pids.json"
$pythonExe = Join-Path $root ".venv\Scripts\python.exe"
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

function Wait-HttpReady {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$ServiceName
    )

    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max($TimeoutSeconds, 1))
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        }
        catch {
            # The service may still be importing modules or binding its port.
        }
        Start-Sleep -Milliseconds 500
    }

    throw "$ServiceName did not become ready within $TimeoutSeconds seconds: $Url"
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
if (-not (Test-Path $pythonExe)) {
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
    -FilePath $pythonExe `
    -ArgumentList @("-m", "langgraph_cli", "dev", "--no-reload", "--no-browser", "--host", "127.0.0.1", "--port", "2024") `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "backend.out.log") `
    -RedirectStandardError (Join-Path $logDir "backend.err.log") `
    -PassThru

Write-Host "Waiting for backend readiness..."
try {
    Wait-HttpReady `
        -Url "http://127.0.0.1:2024/info" `
        -TimeoutSeconds $BackendTimeoutSeconds `
        -ServiceName "Backend"
}
catch {
    & (Join-Path $root "stop.ps1")
    throw
}
Write-Host "Backend is ready."

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

Write-Host "Waiting for frontend readiness..."
try {
    Wait-HttpReady `
        -Url "http://127.0.0.1:3000" `
        -TimeoutSeconds $FrontendTimeoutSeconds `
        -ServiceName "Frontend"
}
catch {
    & (Join-Path $root "stop.ps1")
    throw
}
Write-Host "Frontend is ready."

Write-Host ""
Write-Host "xu-agent is running at http://127.0.0.1:3000" -ForegroundColor Green
Write-Host "Logs: $logDir"
Write-Host "Stop: .\stop.ps1"

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:3000"
}
