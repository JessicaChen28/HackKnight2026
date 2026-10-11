[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$ConsoleDebug,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildRoot = Join-Path $ProjectRoot "build"
$BrowserRoot = Join-Path $BuildRoot "playwright-browsers"
$DistRoot = Join-Path $ProjectRoot "dist"

Push-Location $ProjectRoot
try {
    Write-Host "Installing the reproducible build dependencies..."
    & $Python -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

    New-Item -ItemType Directory -Force -Path $BrowserRoot | Out-Null
    # This process-only setting makes Playwright place Chromium inside the
    # build staging directory, never in the developer's personal browser cache.
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserRoot
    Write-Host "Downloading Playwright Chromium for the installer..."
    & $Python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium installation failed." }

    if ($ConsoleDebug) { $env:SOLAR_RPG_DEBUG_BUILD = "1" } else { Remove-Item Env:SOLAR_RPG_DEBUG_BUILD -ErrorAction SilentlyContinue }
    Write-Host "Building the PyInstaller one-directory distribution..."
    & $Python -m PyInstaller --noconfirm --clean SOLAR_RPG.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    $BuildName = if ($ConsoleDebug) { "SOLAR RPG Debug" } else { "SOLAR RPG" }
    $Executable = Join-Path (Join-Path $DistRoot $BuildName) "$BuildName.exe"
    if (-not (Test-Path -LiteralPath $Executable)) { throw "Expected executable was not produced: $Executable" }
    Write-Host "Built: $Executable"

    if (-not $ConsoleDebug -and -not $SkipInstaller) {
        $Iscc = Get-Command iscc -ErrorAction SilentlyContinue
        if (-not $Iscc) {
            $Candidates = @(
                (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
                (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
                (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
            )
            foreach ($Candidate in $Candidates) {
                if ($Candidate -and (Test-Path -LiteralPath $Candidate)) {
                    $Iscc = Get-Item -LiteralPath $Candidate
                    break
                }
            }
        }
        if (-not $Iscc) {
            throw "Inno Setup Compiler (iscc.exe) was not found. Install Inno Setup, then rerun this script."
        }
        Write-Host "Creating SOLAR-RPG-Setup.exe..."
        $IsccPath = if ($Iscc.PSObject.Properties.Name -contains "Source" -and $Iscc.Source) {
            $Iscc.Source
        } else {
            $Iscc.FullName
        }
        & $IsccPath (Join-Path $ProjectRoot "packaging\SOLAR_RPG.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
        Write-Host "Installer: $(Join-Path $DistRoot 'SOLAR-RPG-Setup.exe')"
    }
}
finally {
    Pop-Location
}
