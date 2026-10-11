$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    $projectPython = ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $projectPython)) {
        Write-Host "Creating .venv..."
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Could not create .venv. Install Python 3.10+ and retry." }
    }
    Write-Host "Installing Python dependencies..."
    & $projectPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the pip error above." }
    Write-Host "Installing Playwright Chromium..."
    & $projectPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Chromium installation failed. Check the Playwright error above." }
    Write-Host "Ready. Generate a world with: .\.venv\Scripts\python.exe generate_world.py"
} finally {
    Pop-Location
}
