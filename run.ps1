$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$botPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $botPython)) {
    throw 'Create .venv and install requirements.lock.txt first. See README.md.'
}
& $botPython -m gemini_bot
exit $LASTEXITCODE
