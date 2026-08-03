$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "Installing uv for project-local Python management..."
    python -m pip install uv
}

if (-not (Test-Path -LiteralPath ".venv")) {
    python -m uv python install 3.12
    python -m uv venv --python 3.12
}

python -m uv sync

if (-not (Test-Path -LiteralPath "web\dist\index.html")) {
    Push-Location "web"
    npm install
    npm run build
    Pop-Location
}

python -m uv run uvicorn app.main:app --host 127.0.0.1 --port 8421
