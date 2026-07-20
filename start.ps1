[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$WithAsr,
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
$configFile = Join-Path $root "config\xu-agent.json"
# Do not use 2024 here: on Windows it can fall inside the system TCP
# excluded port range (`netsh interface ipv4 show excludedportrange
# protocol=tcp`). In that state no process is LISTENING on 2024, but bind
# still fails and langgraph_cli silently falls back to a random port.
$enableAsrServer = $WithAsr -or $WithAsrServer
$asrPortWasProvided = $PSBoundParameters.ContainsKey("AsrPort")

function Get-BackendPort {
    $defaultPort = 2024
    if (-not (Test-Path $configFile)) {
        return $defaultPort
    }
    try {
        $config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
        $value = $config.server.backendPort
        if ($null -eq $value) {
            return $defaultPort
        }
        $port = [int]$value
        if ($port -lt 1 -or $port -gt 65535) {
            throw "backendPort must be in 1..65535."
        }
        return $port
    }
    catch {
        throw "Cannot read backend port from ${configFile}: $($_.Exception.Message)"
    }
}

$backendPort = Get-BackendPort

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

function Test-PortBindable {
    param([int]$Port)

    $listener = $null
    try {
        $address = [System.Net.IPAddress]::Parse("127.0.0.1")
        $listener = [System.Net.Sockets.TcpListener]::new($address, $Port)
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

function Resolve-AsrPort {
    param(
        [Parameter(Mandatory = $true)][int]$PreferredPort,
        [Parameter(Mandatory = $true)][bool]$Explicit
    )

    if (Test-Port $PreferredPort) {
        throw "Port $PreferredPort is already in use. Stop the existing ASR server first."
    }
    if (Test-PortBindable $PreferredPort) {
        return $PreferredPort
    }
    if ($Explicit) {
        throw "Port $PreferredPort cannot be bound on 127.0.0.1. Choose another port with -AsrPort."
    }

    foreach ($candidate in 18765..18864) {
        if ((-not (Test-Port $candidate)) -and (Test-PortBindable $candidate)) {
            Write-Host "ASR port $PreferredPort is unavailable; using $candidate instead." -ForegroundColor Yellow
            return $candidate
        }
    }

    throw "No bindable ASR port found in 18765..18864. Choose another port with -AsrPort."
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

function Get-PythonMinorVersion {
    $output = & $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to inspect Python version: $pythonExe"
    }
    return [version]($output | Select-Object -First 1)
}

function Assert-VoicePythonVersion {
    $version = Get-PythonMinorVersion
    if ($version.Major -ne 3 -or $version.Minor -lt 11 -or $version.Minor -ge 13) {
        throw "Voice dependencies require Python 3.11 or 3.12 on Windows. Current venv uses Python $version at $pythonExe. Recreate .venv with Python 3.11/3.12, then run .\build.ps1 -WithAsr."
    }
}

function Test-TtsDependencies {
    $check = "import importlib.util, sys; missing = [name for name in ('pyttsx3',) if importlib.util.find_spec(name) is None]; print(', '.join(missing)); sys.exit(1 if missing else 0)"
    $output = & $pythonExe -c $check 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "TTS dependencies are missing: $output. Run .\build.ps1 -WithAsr first."
    }
}

if (Test-TrackedProcess) {
    throw "qingzhou-agent is already running. Use .\stop.ps1 before starting it again."
}
if (Test-Port $backendPort) {
    throw "Port $backendPort is already in use. Stop the existing backend first."
}
if (-not (Test-PortBindable $backendPort)) {
    throw "Port $backendPort cannot be bound on 127.0.0.1. It may be in the Windows TCP excluded port range; choose another backend port."
}
if (Test-Port 3000) {
    throw "Port 3000 is already in use. Stop the existing frontend first."
}
if ($enableAsrServer) {
    $AsrPort = Resolve-AsrPort -PreferredPort $AsrPort -Explicit $asrPortWasProvided
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
$withVoiceRuntime = $enableAsrServer

if ($withVoiceRuntime) {
    Test-TtsDependencies
}
if ($enableAsrServer) {
    Assert-VoicePythonVersion
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
if ($withVoiceRuntime) {
    $env:AGENT_TTS_ENABLED = "true"
    $env:AGENT_TTS_PROVIDER = "edge_tts"
    if ($enableAsrServer) {
        $env:QINGZHOU_ASR_URL = "http://127.0.0.1:$AsrPort"
    }
}
else {
    $env:AGENT_TTS_ENABLED = $null
    $env:AGENT_TTS_PROVIDER = $null
    $env:QINGZHOU_ASR_URL = $null
}

$asrProcess = $null

Write-Host "Starting backend..."
$backendProcess = Start-Process `
    -FilePath $pythonExe `
    -ArgumentList @("-m", "langgraph_cli", "dev", "--no-reload", "--no-browser", "--host", "127.0.0.1", "--port", "$backendPort") `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "backend.out.log") `
    -RedirectStandardError (Join-Path $logDir "backend.err.log") `
    -PassThru

[ordered]@{
    backendPid = $backendProcess.Id
    frontendPid = $null
    asrPid = $null
    asrUrl = $null
    ttsEnabled = [bool]$withVoiceRuntime
    startedAt = (Get-Date).ToString("o")
    url = $null
} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8

Write-Host "Waiting for backend readiness..."
try {
    Wait-HttpReady `
        -Url "http://127.0.0.1:$backendPort/info" `
        -TimeoutSeconds $BackendTimeoutSeconds `
        -ServiceName "Backend" `
        -Process $backendProcess
}
catch {
    & (Join-Path $root "stop.ps1")
    throw
}
Write-Host "Backend is ready."

if ($enableAsrServer) {
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
        backendPid = $backendProcess.Id
        frontendPid = $null
        asrPid = $asrProcess.Id
        asrUrl = "http://127.0.0.1:$AsrPort"
        ttsEnabled = [bool]$withVoiceRuntime
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

$env:LANGGRAPH_API_URL = "http://127.0.0.1:$backendPort"
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:3000/api"
$env:NEXT_PUBLIC_ASSISTANT_ID = "agent"
if ($enableAsrServer) {
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
    asrUrl = if ($enableAsrServer) { "http://127.0.0.1:$AsrPort" } else { $null }
    ttsEnabled = [bool]$withVoiceRuntime
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
if ($enableAsrServer) {
    Write-Host "ASR server: http://127.0.0.1:$AsrPort"
}
if ($withVoiceRuntime) {
    $ttsProvider = if ($env:AGENT_TTS_PROVIDER) { $env:AGENT_TTS_PROVIDER } else { "edge_tts" }
    Write-Host "TTS enabled: $ttsProvider"
}
Write-Host "Logs: $logDir"
Write-Host "Stop: .\stop.ps1"

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:3000"
}
