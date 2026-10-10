$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
$python = ".venv\\Scripts\\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating .venv..."
    python -m venv .venv
}

Write-Host "Installing Python dependencies..."
& $python -m pip install -r requirements.txt

Write-Host "Installing Playwright Chromium..."
& $python -m playwright install chromium

Write-Host "Ready. Generate a world with: .\\.venv\\Scripts\\python.exe generate_world.py"
