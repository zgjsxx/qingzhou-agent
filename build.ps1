[CmdletBinding()]
param(
    [switch]$SkipBackend,
    [switch]$SkipFrontend,
    [switch]$WithAsr,
    [switch]$WarmAsrModel,
    [switch]$AllowRunning
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$frontendDir = Join-Path $root "web"
$venvDir = Join-Path $root ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

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

Write-Host "Building qingzhou-agent..." -ForegroundColor Cyan

if (-not $SkipFrontend -and -not $AllowRunning) {
    if (Test-Port 3000) {
        throw "Frontend is running on port 3000. Run .\stop.ps1 before building so Next.js CSS assets are not replaced underneath the active server. Use -AllowRunning only if you understand the risk."
    }
}

if (-not $SkipBackend) {
    if (-not (Test-Path $pythonExe)) {
        Write-Host "[backend] Creating Python virtual environment..."
        Invoke-Checked -FilePath "python" -ArgumentList @("-m", "venv", $venvDir) -WorkingDirectory $root
    }

    & $pythonExe -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[backend] pip is missing; bootstrapping it with ensurepip..."
        Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "ensurepip", "--upgrade") -WorkingDirectory $root
    }

    Write-Host "[backend] Installing dependencies..."
    Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "pip", "install", "--upgrade", "pip") -WorkingDirectory $root
    Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "pip", "install", "-r", "requirements.txt") -WorkingDirectory $root

    if ($WithAsr -or $WarmAsrModel) {
        $asrRequirements = Join-Path $root "requirements-asr.txt"
        if (-not (Test-Path $asrRequirements)) {
            throw "ASR requirements file not found: $asrRequirements"
        }
        Write-Host "[backend] Installing optional SenseVoice ASR dependencies..."
        Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "pip", "install", "-r", "requirements-asr.txt") -WorkingDirectory $root

        if ($WarmAsrModel) {
            Write-Host "[backend] Warming SenseVoice ASR model cache..."
            Invoke-Checked -FilePath $pythonExe -ArgumentList @("-m", "agent.asr", "--warm") -WorkingDirectory $root
        }
    }
}

if (-not $SkipFrontend) {
    Write-Host "[frontend] Installing dependencies..."
    Invoke-Checked -FilePath "corepack.cmd" -ArgumentList @("pnpm", "install", "--frozen-lockfile") -WorkingDirectory $frontendDir

    Write-Host "[frontend] Creating production build..."
    $previousApiUrl = $env:NEXT_PUBLIC_API_URL
    $previousAssistantId = $env:NEXT_PUBLIC_ASSISTANT_ID
    try {
        $env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:3000/api"
        $env:NEXT_PUBLIC_ASSISTANT_ID = "agent"
        Invoke-Checked -FilePath "corepack.cmd" -ArgumentList @("pnpm", "build") -WorkingDirectory $frontendDir
    }
    finally {
        $env:NEXT_PUBLIC_API_URL = $previousApiUrl
        $env:NEXT_PUBLIC_ASSISTANT_ID = $previousAssistantId
    }
}

Write-Host ""
Write-Host "Build completed. Run .\start.ps1 to start qingzhou-agent." -ForegroundColor Green
