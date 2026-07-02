$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $ScriptDir "codex_usage_widget.py"
$Pythonw = $null

$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) {
    $Pythonw = $cmd.Source
}

if (-not $Pythonw -or -not (Test-Path $Pythonw)) {
    throw "pythonw.exe was not found."
}

Start-Process -FilePath $Pythonw -ArgumentList "`"$App`"" -WorkingDirectory $ScriptDir -WindowStyle Hidden
