param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8421,
    [string]$ListenAddress = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$uvExecutable = Get-Command uv -ErrorAction SilentlyContinue
$pythonExecutable = $null
if (-not $uvExecutable) {
    $pythonExecutable = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonExecutable) {
        $pythonExecutable = Get-Command python -ErrorAction SilentlyContinue
    }
    if (-not $pythonExecutable) {
        throw "Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run this script again."
    }
    Write-Host "Installing uv for your user account..."
    & $pythonExecutable.Source -m pip install --user uv
    if ($LASTEXITCODE -ne 0) { throw "uv installation failed." }
}

function Invoke-Uv {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$UvArguments)
    if ($uvExecutable) {
        & $uvExecutable.Source @UvArguments
    } else {
        & $pythonExecutable.Source -m uv @UvArguments
    }
    if ($LASTEXITCODE -ne 0) { throw "uv failed: $($UvArguments -join ' ')" }
}

$nodeExecutable = Get-Command node -ErrorAction SilentlyContinue
$npmExecutable = Get-Command npm -ErrorAction SilentlyContinue
if (-not $nodeExecutable -or -not $npmExecutable) {
    throw "Install Node.js 22 LTS from https://nodejs.org/ and run this script again."
}
$nodeMajor = [int]((& $nodeExecutable.Source --version).TrimStart("v").Split(".")[0])
if ($nodeMajor -lt 22) {
    throw "Node.js 22 or newer is required. Found $(& $nodeExecutable.Source --version)."
}

Invoke-Uv python install 3.12
Invoke-Uv sync --locked

Push-Location "web"
try {
    & $npmExecutable.Source ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
    & $npmExecutable.Source run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
} finally {
    Pop-Location
}

Write-Host "STL to STEP Converter is ready at http://${ListenAddress}:$Port"
Invoke-Uv run uvicorn app.main:app --host $ListenAddress --port $Port
