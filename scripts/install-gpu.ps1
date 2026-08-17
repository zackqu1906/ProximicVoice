param(
    [int]$WaitForProcessId = 0,
    [switch]$Restart,
    [switch]$Interactive
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $ProjectRoot ".runtime\venv\Scripts\python.exe"
$CacheRoot = Join-Path $ProjectRoot ".cache"
$TorchIndexUrl = "https://download.pytorch.org/whl/cu128"

function Assert-LastCommand([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "NVIDIA GPU acceleration requires 64-bit Windows."
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "The managed environment is missing. Run scripts/setup.ps1 first."
    }

    $NvidiaSmi = Get-Command "nvidia-smi.exe" -ErrorAction SilentlyContinue
    if ($null -eq $NvidiaSmi) {
        throw "No NVIDIA driver was detected. Install or update the NVIDIA driver first."
    }
    $GpuNames = @(
        & $NvidiaSmi.Source --query-gpu=name --format=csv,noheader 2>$null |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    Assert-LastCommand "NVIDIA driver check"
    if ($GpuNames.Count -eq 0) {
        throw "No NVIDIA GPU was detected."
    }

    if ($WaitForProcessId -gt 0) {
        $RunningApp = Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue
        if ($null -ne $RunningApp) {
            Write-Host "Waiting for Proximic Voice to exit before replacing PyTorch..."
            $Deadline = [DateTime]::UtcNow.AddSeconds(30)
            while (
                $null -ne (Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue) -and
                [DateTime]::UtcNow -lt $Deadline
            ) {
                Start-Sleep -Milliseconds 250
            }
            if ($null -ne (Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue)) {
                throw "Proximic Voice did not exit within 30 seconds. Close it and run this script again."
            }
        }
    }

    Set-Location -LiteralPath $ProjectRoot
    $env:PIP_CACHE_DIR = Join-Path $CacheRoot "pip"
    $env:MODELSCOPE_CACHE = Join-Path $CacheRoot "modelscope"
    $env:HF_HOME = Join-Path $CacheRoot "huggingface"
    $env:TORCH_HOME = Join-Path $CacheRoot "torch"
    New-Item -ItemType Directory -Force -Path $env:PIP_CACHE_DIR | Out-Null

    Write-Host "Detected NVIDIA GPU: $($GpuNames -join ', ')"
    Write-Host "Installing the CUDA 12.8 PyTorch runtime. This download is several GB..."
    & $VenvPython -m pip install --force-reinstall --no-deps `
        "torch==2.11.0" "torchaudio==2.11.0" --index-url $TorchIndexUrl
    Assert-LastCommand "CUDA PyTorch installation"

    & $VenvPython -c "import torch; assert torch.version.cuda is not None; assert torch.cuda.is_available(); print('Torch:', torch.__version__); print('CUDA:', torch.version.cuda); print('GPU:', torch.cuda.get_device_name(0))"
    Assert-LastCommand "CUDA runtime validation"

    & $VenvPython -c "from PySide6.QtCore import QSettings; s=QSettings('ProxiMic', 'ProxiMic Voice'); s.setValue('asr/device', 'cuda:0'); s.sync()"
    Assert-LastCommand "GPU preference update"

    Write-Host ""
    Write-Host "GPU acceleration was installed and verified successfully."

    if ($Restart) {
        $Pythonw = Join-Path $ProjectRoot ".runtime\venv\Scripts\pythonw.exe"
        $Launcher = if (Test-Path -LiteralPath $Pythonw) { $Pythonw } else { $VenvPython }
        Start-Process -FilePath $Launcher -ArgumentList @("-m", "proximic_ring.ui") `
            -WorkingDirectory $ProjectRoot
    }
} catch {
    Write-Host ""
    Write-Host "GPU installation failed: $($_.Exception.Message)" -ForegroundColor Red
    if ($Interactive) {
        Read-Host "Press Enter to close this window" | Out-Null
    }
    exit 1
}
