[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$pidFile = Join-Path $root ".runtime\pids.json"
$configFile = Join-Path $root "config\xu-agent.json"

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
            return $defaultPort
        }
        return $port
    }
    catch {
        return $defaultPort
    }
}

$knownPorts = @(3000, (Get-BackendPort), 8765)

function Get-ListeningProcessIds {
    param([int[]]$Ports)

    $ids = @()
    foreach ($line in (& netstat.exe -ano)) {
        if ($line -notmatch "LISTENING\s+(\d+)\s*$") {
            continue
        }
        $processId = [int]$Matches[1]
        foreach ($port in $Ports) {
            if ($line -match "[:.]${port}\s+") {
                $ids += $processId
                break
            }
        }
    }
    return @($ids | Sort-Object -Unique)
}

$targets = @()
if (Test-Path $pidFile) {
    try {
        $tracked = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        $targets += @(
            @{ Name = "frontend"; Id = $tracked.frontendPid },
            @{ Name = "backend"; Id = $tracked.backendPid },
            @{ Name = "ASR server"; Id = $tracked.asrPid }
        )
        if ($tracked.asrUrl -and "$($tracked.asrUrl)" -match ":(\d+)(?:/|$)") {
            $knownPorts += [int]$Matches[1]
        }
    }
    catch {
        throw "Cannot read PID file: $pidFile"
    }
}

$targets += Get-ListeningProcessIds -Ports @($knownPorts | Sort-Object -Unique) | ForEach-Object {
    $process = Get-Process -Id $_ -ErrorAction SilentlyContinue
    $name = if ($process) { "$($process.ProcessName) on qingzhou-agent port" } else { "process on qingzhou-agent port" }
    @{ Name = $name; Id = $_ }
}

$stopped = $false
$seen = @{}
foreach ($entry in $targets) {
    $processId = [int]$entry.Id
    if ($processId -le 0 -or $seen.ContainsKey($processId)) {
        continue
    }
    $seen[$processId] = $true

    if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
        Write-Host "Stopping $($entry.Name) (PID $processId)..."
        & taskkill.exe /PID $processId /T /F | Out-Null
        $stopped = $true
    }
}

if (Test-Path $pidFile) {
    Remove-Item -LiteralPath $pidFile -Force
}

if ($stopped) {
    Write-Host "qingzhou-agent stopped." -ForegroundColor Green
}
else {
    Write-Host "qingzhou-agent is not running."
}
