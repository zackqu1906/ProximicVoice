param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".runtime\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Missing .runtime Python. Run scripts/setup.ps1 -SkipLocalLLM first."
}

if (-not $SkipDependencyInstall) {
    & $Python -m pip install -r (Join-Path $ProjectRoot "requirements-packaging.txt")
}

Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean "packaging\proximic_voice.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    $BundledExe = Join-Path $ProjectRoot "dist\ProximicVoice\ProximicVoice.exe"
    $PackageCheck = Start-Process -FilePath $BundledExe `
        -ArgumentList "--self-check-package" -PassThru -Wait -WindowStyle Hidden
    if ($PackageCheck.ExitCode -ne 0) {
        throw "Bundled application self-check failed (exit $($PackageCheck.ExitCode)); check the startup log under LOCALAPPDATA\ProxiMic Voice\logs."
    }
    Write-Host "Bundled QML, Opus, and ASR self-check passed."

    $IsccCandidates = @(
        "$ProjectRoot\.build\tools\InnoSetup6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $IsccCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $Iscc) {
        throw "Inno Setup 6 not found. Install it, then rerun this script."
    }
    & $Iscc "packaging\windows\ProximicVoice.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }

    $Installer = Join-Path $ProjectRoot "dist\installer\ProximicVoice-0.6.0-windows-x64-setup.exe"
    if ($env:WINDOWS_SIGNING_CERT_SHA1) {
        $SignToolCandidates = @(
            (Get-Command signtool.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
            "${env:ProgramFiles(x86)}\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe",
            "$env:ProgramFiles\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
        )
        $SignTool = $SignToolCandidates | Where-Object {
            $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
        } | Select-Object -First 1
        if (-not $SignTool) {
            throw "WINDOWS_SIGNING_CERT_SHA1 is set, but signtool.exe was not found."
        }
        $TimestampUrl = if ($env:WINDOWS_TIMESTAMP_URL) {
            $env:WINDOWS_TIMESTAMP_URL
        } else {
            "http://timestamp.digicert.com"
        }
        & $SignTool sign /sha1 $env:WINDOWS_SIGNING_CERT_SHA1 /fd SHA256 `
            /tr $TimestampUrl /td SHA256 $Installer
        if ($LASTEXITCODE -ne 0) { throw "Windows code signing failed." }
        & $SignTool verify /pa $Installer
        if ($LASTEXITCODE -ne 0) { throw "Windows signature verification failed." }
    }
} finally {
    Pop-Location
}

Write-Host "Installer: $ProjectRoot\dist\installer\ProximicVoice-0.6.0-windows-x64-setup.exe"
