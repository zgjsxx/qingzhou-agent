[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$WithAsrServer,
    [int]$AsrPort = 8765,
    [int]$AsrTimeoutSeconds = 300,
    [int]$BackendTimeoutSeconds = 180,
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
        foreach ($processId in @($tracked.backendPid, $tracked.frontendPid, $tracked.asrPid)) {
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
        [Parameter(Mandatory = $true)][string]$ServiceName,
        [System.Diagnostics.Process]$Process = $null
    )

    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max($TimeoutSeconds, 1))
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process -and $Process.HasExited) {
            throw "$ServiceName exited before it became ready. Check logs in $logDir."
        }
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

function Test-AsrServerDependencies {
    $check = "import importlib.util, sys; missing = [name for name in ('fastapi', 'uvicorn', 'multipart') if importlib.util.find_spec(name) is None]; print(', '.join(missing)); sys.exit(1 if missing else 0)"
    $output = & $pythonExe -c $check 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "ASR server dependencies are missing: $output. Run .\build.ps1 -WithAsr first."
    }
}

if (Test-TrackedProcess) {
    throw "qingzhou-agent is already running. Use .\stop.ps1 before starting it again."
}
if (Test-Port 2024) {
    throw "Port 2024 is already in use. Stop the existing backend first."
}
if (Test-Port 3000) {
    throw "Port 3000 is already in use. Stop the existing frontend first."
}
if ($WithAsrServer -and (Test-Port $AsrPort)) {
    throw "Port $AsrPort is already in use. Stop the existing ASR server first."
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

$asrProcess = $null
if ($WithAsrServer) {
    Test-AsrServerDependencies

    Write-Host "Starting ASR server..."
    $asrProcess = Start-Process `
        -FilePath $pythonExe `
        -ArgumentList @("-m", "agent.asr_server", "--host", "127.0.0.1", "--port", "$AsrPort") `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "asr.out.log") `
        -RedirectStandardError (Join-Path $logDir "asr.err.log") `
        -PassThru

    [ordered]@{
        backendPid = $null
        frontendPid = $null
        asrPid = $asrProcess.Id
        asrUrl = "http://127.0.0.1:$AsrPort"
        startedAt = (Get-Date).ToString("o")
        url = $null
    } | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8

    Write-Host "Waiting for ASR server readiness..."
    try {
        Wait-HttpReady `
            -Url "http://127.0.0.1:$AsrPort/health" `
            -TimeoutSeconds $AsrTimeoutSeconds `
            -ServiceName "ASR server" `
            -Process $asrProcess
    }
    catch {
        & (Join-Path $root "stop.ps1")
        throw
    }
    Write-Host "ASR server is ready."
}

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
        -ServiceName "Backend" `
        -Process $backendProcess
}
catch {
    & (Join-Path $root "stop.ps1")
    throw
}
Write-Host "Backend is ready."

$env:LANGGRAPH_API_URL = "http://127.0.0.1:2024"
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:3000/api"
$env:NEXT_PUBLIC_ASSISTANT_ID = "agent"
if ($WithAsrServer) {
    $env:QINGZHOU_ASR_URL = "http://127.0.0.1:$AsrPort"
}

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
    asrPid = if ($asrProcess) { $asrProcess.Id } else { $null }
    asrUrl = if ($WithAsrServer) { "http://127.0.0.1:$AsrPort" } else { $null }
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
Write-Host "qingzhou-agent is running at http://127.0.0.1:3000" -ForegroundColor Green
if ($WithAsrServer) {
    Write-Host "ASR server: http://127.0.0.1:$AsrPort"
}
Write-Host "Logs: $logDir"
Write-Host "Stop: .\stop.ps1"

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:3000"
}
