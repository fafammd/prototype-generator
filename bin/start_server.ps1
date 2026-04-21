# AI Prototype Generator - Startup Script

$pythonPath = "python"
$projectRoot = Split-Path $PSScriptRoot
$scriptPath = Join-Path $projectRoot "server.py"

Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "       AI Prototype Generator - Local Server" -ForegroundColor Cyan
Write-Host "==================================================="
Write-Host ""

Write-Host "[INFO] Starting server..." -ForegroundColor Green
Write-Host "Project Root: $projectRoot" -ForegroundColor Gray
Write-Host ""
Write-Host "Access URL: http://localhost:8080/src/index.html" -ForegroundColor Yellow
Write-Host ""

& $pythonPath $scriptPath

Read-Host "Press Enter to exit"
