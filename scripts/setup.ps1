param(
    [string]$Python = "",
    [switch]$SkipFunASR
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot

function Assert-LastCommand([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Write-Host "[1/4] Checking bundled third-party source repositories..."

$StreamingRepo = Join-Path $ProjectRoot "third_party\streaming-sensevoice"
$FunRepo = Join-Path $ProjectRoot "third_party\Fun-ASR"
if (-not (Test-Path -LiteralPath (Join-Path $StreamingRepo "streaming_sensevoice"))) {
    throw "Missing third_party/streaming-sensevoice. Clone or download the complete project."
}
if (-not $SkipFunASR -and -not (Test-Path -LiteralPath (Join-Path $FunRepo "model.py"))) {
    throw "Missing third_party/Fun-ASR. Clone or download the complete project."
}

Write-Host "[2/4] Checking Python 3.11+..."
$PythonArgs = @()
if ($Python) {
    $PythonExe = $Python
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"
    $PythonArgs = @("-3.11")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = "python"
} else {
    throw "Python was not found. Install 64-bit Python 3.11 and run this script again."
}
& $PythonExe @PythonArgs -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'; print(sys.version)"
Assert-LastCommand "Python version check"

Write-Host "[3/4] Creating the local virtual environment..."
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".venv\Scripts\python.exe"))) {
    & $PythonExe @PythonArgs -m venv .venv
    Assert-LastCommand "virtual environment creation"
}
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip setuptools wheel
Assert-LastCommand "pip bootstrap"

Write-Host "[4/4] Installing Proximic Voice and ASR dependencies..."
$Extras = if ($SkipFunASR) {
    ".[ring,asr-streaming-sensevoice,ui]"
} else {
    ".[ring,asr-streaming-sensevoice,asr-funasr-nano,ui]"
}
& $VenvPython -m pip install -e $Extras
Assert-LastCommand "project dependency installation"

Write-Host ""
Write-Host "Installation completed. Start the UI with:"
Write-Host "  .\scripts\start-ui.cmd"
Write-Host ""
Write-Host "The first ASR run may download model weights. Fun-ASR-Nano is approximately 2 GB."
