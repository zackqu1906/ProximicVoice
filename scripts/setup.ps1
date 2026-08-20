param(
    [switch]$SkipFunASR,
    [switch]$SkipLocalLLM,
    [switch]$Recreate,
    [string]$ExistingLocalModelPath = "",
    [ValidateSet("prompt", "cpu", "cuda")]
    [string]$Compute = "prompt"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot

$PythonVersion = "3.11.9"
$PythonArchiveName = "python-$PythonVersion-amd64.zip"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/$PythonArchiveName"
$PythonSha256 = "4ba90a4ab8990891033d37ff04d2047fdae8948d0d2729a68d3a6a17c585b681"
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$DownloadRoot = Join-Path $RuntimeRoot "downloads"
$PythonRoot = Join-Path $RuntimeRoot "python-$PythonVersion"
$PythonArchive = Join-Path $DownloadRoot $PythonArchiveName
$RuntimePython = Join-Path $PythonRoot "python.exe"
$VenvRoot = Join-Path $RuntimeRoot "venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$ConstraintFile = Join-Path $ProjectRoot "requirements-windows.lock"
$CacheRoot = Join-Path $ProjectRoot ".cache"
$LocalLLMCatalogPath = Join-Path $ProjectRoot "src\proximic_ring\assets\local_llm_catalog.json"

function Assert-LastCommand([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Get-FileSha256([string]$Path) {
    $Stream = [IO.File]::OpenRead($Path)
    try {
        $Hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $Bytes = $Hasher.ComputeHash($Stream)
            return ([BitConverter]::ToString($Bytes)).Replace("-", "").ToLowerInvariant()
        } finally {
            $Hasher.Dispose()
        }
    } finally {
        $Stream.Dispose()
    }
}

function Remove-ProjectDirectory([string]$Path, [string]$ExpectedName) {
    $FullPath = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $RootPrefix = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $FullPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a directory outside the project: $FullPath"
    }
    if ([IO.Path]::GetFileName($FullPath) -ne $ExpectedName) {
        throw "Refusing to remove unexpected directory: $FullPath"
    }
    if (Test-Path -LiteralPath $FullPath) {
        Remove-Item -LiteralPath $FullPath -Recurse -Force
    }
}

function Get-NvidiaGpuNames {
    $NvidiaSmi = Get-Command "nvidia-smi.exe" -ErrorAction SilentlyContinue
    if ($null -eq $NvidiaSmi) {
        return @()
    }
    $Output = & $NvidiaSmi.Source --query-gpu=name --format=csv,noheader 2>$null
    if ($LASTEXITCODE -ne 0) {
        return @()
    }
    return @($Output | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Resolve-ComputeMode([string]$RequestedMode, [string[]]$GpuNames) {
    if ($RequestedMode -eq "cuda") {
        if ($GpuNames.Count -eq 0) {
            throw "-Compute cuda was requested, but no working NVIDIA driver was detected."
        }
        return "cuda"
    }
    if ($RequestedMode -eq "cpu") {
        return "cpu"
    }

    Write-Host ""
    Write-Host "Choose the local ASR compute mode:"
    Write-Host "  [1] CPU - works on every supported Windows computer (default)"
    if ($GpuNames.Count -gt 0) {
        Write-Host "  [2] NVIDIA GPU - faster, but downloads several additional GB"
        Write-Host "      Detected: $($GpuNames -join ', ')"
        $Choice = Read-Host "Enter 1 or 2"
        if ($Choice.Trim() -eq "2") {
            return "cuda"
        }
    } else {
        Write-Host "  NVIDIA GPU option unavailable: no working NVIDIA driver was detected."
    }
    return "cpu"
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Proximic Voice requires 64-bit Windows."
}

$NvidiaGpuNames = @(Get-NvidiaGpuNames)
$ResolvedCompute = Resolve-ComputeMode -RequestedMode $Compute -GpuNames $NvidiaGpuNames
$TorchIndexUrl = if ($ResolvedCompute -eq "cuda") {
    "https://download.pytorch.org/whl/cu128"
} else {
    "https://download.pytorch.org/whl/cpu"
}

Write-Host "[1/9] Checking bundled source repositories..."
$StreamingRepo = Join-Path $ProjectRoot "third_party\streaming-sensevoice"
$FunRepo = Join-Path $ProjectRoot "third_party\Fun-ASR"
if (-not (Test-Path -LiteralPath (Join-Path $StreamingRepo "streaming_sensevoice"))) {
    throw "Missing third_party/streaming-sensevoice. Clone or download the complete project."
}
if (-not $SkipFunASR -and -not (Test-Path -LiteralPath (Join-Path $FunRepo "model.py"))) {
    throw "Missing third_party/Fun-ASR. Clone or download the complete project."
}
if (-not (Test-Path -LiteralPath $ConstraintFile)) {
    throw "Missing requirements-windows.lock. Clone or download the complete project."
}
if (-not $SkipLocalLLM -and -not (Test-Path -LiteralPath $LocalLLMCatalogPath)) {
    throw "Missing local LLM package catalog: $LocalLLMCatalogPath"
}

Write-Host "[2/9] Preparing project-local Python $PythonVersion..."
New-Item -ItemType Directory -Force -Path $DownloadRoot, $CacheRoot | Out-Null
if (-not (Test-Path -LiteralPath $PythonArchive)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Host "Downloading Python from python.org..."
    Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonArchive
}
$ActualHash = Get-FileSha256 -Path $PythonArchive
if ($ActualHash -ne $PythonSha256) {
    Remove-Item -LiteralPath $PythonArchive -Force
    throw "Python archive checksum mismatch. The invalid download was removed; run setup again."
}
if (-not (Test-Path -LiteralPath $RuntimePython)) {
    Remove-ProjectDirectory -Path $PythonRoot -ExpectedName "python-$PythonVersion"
    Expand-Archive -LiteralPath $PythonArchive -DestinationPath $PythonRoot
}
& $RuntimePython -c "import sys; assert sys.version_info[:3] == (3, 11, 9); print(sys.version)"
Assert-LastCommand "project-local Python validation"

Write-Host "[3/9] Preparing project-local caches..."
$env:PIP_CACHE_DIR = Join-Path $CacheRoot "pip"
$env:MODELSCOPE_CACHE = Join-Path $CacheRoot "modelscope"
$env:HF_HOME = Join-Path $CacheRoot "huggingface"
$env:TORCH_HOME = Join-Path $CacheRoot "torch"
New-Item -ItemType Directory -Force -Path $env:PIP_CACHE_DIR, $env:MODELSCOPE_CACHE, $env:HF_HOME, $env:TORCH_HOME | Out-Null

Write-Host "[4/9] Creating the isolated virtual environment..."
$MustRecreate = $Recreate.IsPresent
if (Test-Path -LiteralPath $VenvPython) {
    $VenvConfig = Join-Path $VenvRoot "pyvenv.cfg"
    $ExpectedHome = "home = $PythonRoot"
    $ConfigText = if (Test-Path -LiteralPath $VenvConfig) { Get-Content -LiteralPath $VenvConfig -Raw } else { "" }
    if ($ConfigText -notmatch [regex]::Escape($ExpectedHome)) {
        Write-Host "Existing managed environment was created by another Python and will be replaced."
        $MustRecreate = $true
    }
}
if ($MustRecreate) {
    Remove-ProjectDirectory -Path $VenvRoot -ExpectedName "venv"
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $RuntimePython -m venv --copies $VenvRoot
    Assert-LastCommand "virtual environment creation"
}

Write-Host "[5/9] Installing pinned packaging tools..."
& $VenvPython -m pip install --upgrade "pip==26.2.1" "setuptools==81.0.0" "wheel==0.48.0"
Assert-LastCommand "pip bootstrap"

Write-Host "[6/9] Installing the $ResolvedCompute PyTorch runtime..."
$InstalledTorchFlavor = & $VenvPython -c "import torch; print('cuda' if torch.version.cuda else 'cpu')" 2>$null
if ($LASTEXITCODE -ne 0) {
    $InstalledTorchFlavor = ""
}
$InstalledTorchFlavor = "$InstalledTorchFlavor".Trim()
if ($InstalledTorchFlavor -ne $ResolvedCompute) {
    & $VenvPython -m pip install --force-reinstall --no-deps `
        "torch==2.11.0" "torchaudio==2.11.0" --index-url $TorchIndexUrl
    Assert-LastCommand "$ResolvedCompute PyTorch installation"
} else {
    Write-Host "The requested PyTorch runtime is already installed."
}

Write-Host "[7/9] Installing Proximic Voice and ASR dependencies..."
$Extras = if ($SkipFunASR) {
    ".[ring,asr-streaming-sensevoice,asr-volcengine,ui]"
} else {
    ".[ring,asr-streaming-sensevoice,asr-funasr-nano,asr-volcengine,ui]"
}
& $VenvPython -m pip install -c $ConstraintFile -e $Extras
Assert-LastCommand "project dependency installation"

Write-Host "[8/9] Preparing the default local LLM package..."
if ($SkipLocalLLM) {
    Write-Host "Local LLM installation skipped by -SkipLocalLLM."
} else {
    $LocalLLMRoot = if ($env:PROXIMIC_LLM_HOME) {
        [IO.Path]::GetFullPath($env:PROXIMIC_LLM_HOME)
    } else {
        Join-Path $RuntimeRoot "local-llm"
    }
    & (Join-Path $PSScriptRoot "install-local-llm.ps1") `
        -CatalogPath $LocalLLMCatalogPath `
        -InstallRoot $LocalLLMRoot `
        -DownloadRoot $DownloadRoot `
        -ExistingModelPath $ExistingLocalModelPath
    & $VenvPython -c "from pathlib import Path; from proximic_ring.text_processing import resolve_default_local_llm; p=resolve_default_local_llm(); assert Path(p['server_path']).is_file(); assert Path(p['model_path']).is_file(); print('Local LLM package self-check passed:', p['model_id'])"
    Assert-LastCommand "local LLM package self-check"
}

Write-Host "[9/9] Verifying native libraries and UI imports..."
$ComputeCheck = if ($ResolvedCompute -eq "cuda") {
    "assert torch.version.cuda is not None; assert torch.cuda.is_available(); print('GPU:', torch.cuda.get_device_name(0))"
} else {
    "assert torch.version.cuda is None"
}
$SelfCheck = if ($SkipFunASR) {
    "import sys, torch, PySide6, proximic_ring; import proximic_ring.ui.main; assert sys.prefix.lower().startswith(r'$VenvRoot'.lower()); $ComputeCheck; print('Python:', sys.version.split()[0]); print('Torch:', torch.__version__); print('PySide6:', PySide6.__version__); print('Project:', proximic_ring.__file__); print('Installation self-check passed.')"
} else {
    "import sys, torch, torchaudio, PySide6, proximic_ring; import proximic_ring.ui.main; assert sys.prefix.lower().startswith(r'$VenvRoot'.lower()); $ComputeCheck; print('Python:', sys.version.split()[0]); print('Torch:', torch.__version__); print('TorchAudio:', torchaudio.__version__); print('PySide6:', PySide6.__version__); print('Project:', proximic_ring.__file__); print('Installation self-check passed.')"
}
& $VenvPython -c $SelfCheck
Assert-LastCommand "installation self-check"

Write-Host ""
Write-Host "Installation completed and verified. Start the UI with:"
Write-Host "  .\scripts\start-ui.cmd"
Write-Host ""
Write-Host "Runtime: $PythonRoot"
Write-Host "Environment: $VenvRoot"
Write-Host "Cache: $CacheRoot"
Write-Host "Compute: $ResolvedCompute"
if (-not $SkipLocalLLM) {
    Write-Host "Local LLM: $LocalLLMRoot"
}
Write-Host "The first ASR run may still download ASR weights. Fun-ASR-Nano is approximately 2 GB."
