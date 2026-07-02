$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistDir = Join-Path $ScriptDir "dist"
$BuildDir = Join-Path $ScriptDir "build"
$SpecFile = Join-Path $ScriptDir "CodexUsageWidget.spec"
$AppDir = Join-Path $DistDir "CodexUsageWidget"
$DefaultConfig = Join-Path $AppDir "config.sample.json"
$BuildTools = Join-Path $ScriptDir ".buildtools"

Push-Location $ScriptDir
try {
    if (Test-Path $BuildDir) {
        Remove-Item -LiteralPath $BuildDir -Recurse -Force
    }
    if (Test-Path $AppDir) {
        Remove-Item -LiteralPath $AppDir -Recurse -Force
    }
    if (Test-Path $SpecFile) {
        Remove-Item -LiteralPath $SpecFile -Force
    }

    $oldPythonPath = $env:PYTHONPATH
    $localPyInstaller = Join-Path $BuildTools "PyInstaller\__main__.py"
    $hasGlobalPyInstaller = $false

    python -m PyInstaller --version *> $null
    if ($LASTEXITCODE -eq 0) {
        $hasGlobalPyInstaller = $true
    }

    if (-not $hasGlobalPyInstaller -and -not (Test-Path $localPyInstaller)) {
        New-Item -ItemType Directory -Force -Path $BuildTools | Out-Null
        python -m pip install --target $BuildTools pyinstaller
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install PyInstaller into $BuildTools."
        }
    }

    if (-not $hasGlobalPyInstaller -and (Test-Path $BuildTools)) {
        if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
            $env:PYTHONPATH = $BuildTools
        }
        else {
            $env:PYTHONPATH = "$BuildTools;$env:PYTHONPATH"
        }
    }

    try {
        python -m PyInstaller `
            --noconfirm `
            --clean `
            --windowed `
            --onedir `
            --name CodexUsageWidget `
            codex_usage_widget.py

        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        $env:PYTHONPATH = $oldPythonPath
    }

    Copy-Item -LiteralPath (Join-Path $ScriptDir "README.md") -Destination (Join-Path $AppDir "README.md") -Force
    Copy-Item -LiteralPath (Join-Path $ScriptDir "config.sample.json") -Destination $DefaultConfig -Force

    Write-Host "Built: $AppDir"
    Write-Host "Run:   $AppDir\CodexUsageWidget.exe"
}
finally {
    Pop-Location
}
