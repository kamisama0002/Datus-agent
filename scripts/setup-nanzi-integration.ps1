$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repositoryRoot

$uv = (Get-Command uv -ErrorAction Stop).Source
& $uv venv --python 3.12 .venv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python 3.12 virtual environment was not created at $python."
}

& $uv pip install --python $python -e . datus-mysql datus-metricflow "datus-semantic-osi[metricflow]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
