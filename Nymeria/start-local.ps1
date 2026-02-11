# Nymeria Local Startup Script
# Starts the API server and Discord bot side by side.
# Press Ctrl+C to stop everything.
#
# Usage:
#   .\start-local.ps1              # API only
#   .\start-local.ps1 -Discord     # API + Discord bot
#   .\start-local.ps1 -Port 9000   # API on custom port

param(
    [switch]$Discord,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host ""
Write-Host "=== Nymeria Local Startup ===" -ForegroundColor Cyan
Write-Host ""

# Track background jobs so we can clean them up
$jobs = @()

# Start the API server
Write-Host "[1/2] Starting API server on port $Port..." -ForegroundColor Yellow
$apiJob = Start-Process python -ArgumentList "run.py", "api", "--port", $Port `
    -NoNewWindow -PassThru

$jobs += $apiJob
Write-Host "  API started (PID: $($apiJob.Id))" -ForegroundColor Green
Write-Host "  http://localhost:$Port/docs" -ForegroundColor DarkGray

# Give the API a moment to start before the bot tries to connect
Start-Sleep -Seconds 3

# Start the Discord bot if requested
if ($Discord) {
    Write-Host "[2/2] Starting Discord bot (connected to API)..." -ForegroundColor Yellow
    $botJob = Start-Process python -ArgumentList "run.py", "discord-bot", "--api-url", "http://localhost:$Port" `
        -NoNewWindow -PassThru

    $jobs += $botJob
    Write-Host "  Discord bot started (PID: $($botJob.Id))" -ForegroundColor Green
} else {
    Write-Host "[2/2] Discord bot skipped (use -Discord flag to enable)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "=== All services running ===" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop everything." -ForegroundColor DarkGray
Write-Host ""

# Wait for Ctrl+C, then clean up
try {
    # Wait for any process to exit (or Ctrl+C)
    while ($true) {
        foreach ($job in $jobs) {
            if ($job.HasExited) {
                Write-Host ""
                Write-Host "Process $($job.Id) exited with code $($job.ExitCode)" -ForegroundColor Yellow
                throw "Process exited"
            }
        }
        Start-Sleep -Seconds 1
    }
} finally {
    Write-Host ""
    Write-Host "Stopping all services..." -ForegroundColor Yellow
    foreach ($job in $jobs) {
        if (-not $job.HasExited) {
            Stop-Process -Id $job.Id -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped PID $($job.Id)" -ForegroundColor DarkGray
        }
    }
    Write-Host "All services stopped." -ForegroundColor Green
}
