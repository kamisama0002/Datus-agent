$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repositoryRoot

$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "NanZi integration environment is missing. Run scripts/setup-nanzi-integration.ps1 first."
}

& $python -m datus.api.main --host 127.0.0.1 --port 8001 --workers 1 --config conf/agent-nanzi.example.yml
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
