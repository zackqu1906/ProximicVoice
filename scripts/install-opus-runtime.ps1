param(
    [string]$InstallRoot = "",
    [string]$DownloadRoot = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $RuntimeRoot "opus"
}
if (-not $DownloadRoot) {
    $DownloadRoot = Join-Path $RuntimeRoot "downloads"
}

$PackageName = "mingw-w64-x86_64-opus-1.6.1-1-any.pkg.tar.zst"
$PackageUrl = "https://mirror.msys2.org/mingw/mingw64/$PackageName"
$PackageSha256 = "8ff5a273c811e64c5af4c886b6f5d7a8aefca30ef2c7942a7e0a7e62c49e1c25"
$ArchivePath = Join-Path $DownloadRoot $PackageName
$TargetDll = Join-Path $InstallRoot "opus.dll"
$TargetLicense = Join-Path $InstallRoot "COPYING.libopus"

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

New-Item -ItemType Directory -Force -Path $InstallRoot, $DownloadRoot | Out-Null

if (-not (Test-Path -LiteralPath $ArchivePath) -or
    (Get-FileSha256 $ArchivePath) -ne $PackageSha256) {
    Invoke-WebRequest -Uri $PackageUrl -OutFile $ArchivePath
}
if ((Get-FileSha256 $ArchivePath) -ne $PackageSha256) {
    throw "libopus package SHA256 verification failed: $ArchivePath"
}

$ExtractRoot = Join-Path ([IO.Path]::GetTempPath()) ("proximic-opus-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $ExtractRoot | Out-Null
try {
    & tar -xf $ArchivePath -C $ExtractRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to extract libopus package (tar exit $LASTEXITCODE)"
    }
    $SourceDll = Join-Path $ExtractRoot "mingw64\bin\libopus-0.dll"
    $SourceLicense = Join-Path $ExtractRoot "mingw64\share\licenses\opus\COPYING"
    if (-not (Test-Path -LiteralPath $SourceDll)) {
        throw "libopus-0.dll was not found in the verified package"
    }
    Copy-Item -LiteralPath $SourceDll -Destination $TargetDll -Force
    Copy-Item -LiteralPath $SourceLicense -Destination $TargetLicense -Force
} finally {
    $ResolvedTemp = [IO.Path]::GetFullPath($ExtractRoot)
    $TempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if ($ResolvedTemp.StartsWith($TempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $ResolvedTemp)) {
        Remove-Item -LiteralPath $ResolvedTemp -Recurse -Force
    }
}

Write-Host "libopus runtime installed: $TargetDll"
