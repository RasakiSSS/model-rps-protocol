# Forward all arguments to the CLI using this checkout's virtual environment.
$ErrorActionPreference = 'Stop'
$launcherPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $launcherPython -PathType Leaf)) {
    [Console]::Error.WriteLine('Python environment not found: ' + $launcherPython)
    [Console]::Error.WriteLine('Open the project folder and run:')
    [Console]::Error.WriteLine('  py -3.12 -m venv .venv')
    [Console]::Error.WriteLine('  .\.venv\Scripts\python.exe -m pip install -e .')
    exit 2
}

$launcherExitCode = 1
Push-Location -LiteralPath $PSScriptRoot
try {
    & $launcherPython -X utf8 -m model_rps @args
    $launcherExitCode = $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine('Unable to start Model RPS: ' + $_.Exception.Message)
}
finally {
    Pop-Location
}
exit $launcherExitCode
