[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$pidFile = Join-Path $root ".runtime\pids.json"

if (-not (Test-Path $pidFile)) {
    Write-Host "xu-agent is not running (no PID file found)."
    exit 0
}

try {
    $tracked = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
}
catch {
    throw "Cannot read PID file: $pidFile"
}

$stopped = $false
foreach ($entry in @(
    @{ Name = "frontend"; Id = $tracked.frontendPid },
    @{ Name = "backend"; Id = $tracked.backendPid }
)) {
    $processId = [int]$entry.Id
    if ($processId -le 0) {
        continue
    }

    if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
        Write-Host "Stopping $($entry.Name) (PID $processId)..."
        & taskkill.exe /PID $processId /T /F | Out-Null
        $stopped = $true
    }
}

Remove-Item -LiteralPath $pidFile -Force

if ($stopped) {
    Write-Host "xu-agent stopped." -ForegroundColor Green
}
else {
    Write-Host "No tracked processes were running; stale PID file removed."
}
