param(
    [string]$CatalogPath = "",
    [string]$InstallRoot = "",
    [string]$DownloadRoot = "",
    [string]$RuntimeId = "",
    [string]$ModelId = "",
    [string]$ExistingModelPath = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $CatalogPath) {
    $CatalogPath = Join-Path $ProjectRoot "src\proximic_ring\assets\local_llm_catalog.json"
}
if (-not $InstallRoot) {
    $InstallRoot = if ($env:PROXIMIC_LLM_HOME) {
        [IO.Path]::GetFullPath($env:PROXIMIC_LLM_HOME)
    } else {
        Join-Path $ProjectRoot ".runtime\local-llm"
    }
}
if (-not $DownloadRoot) {
    $DownloadRoot = Join-Path $ProjectRoot ".runtime\downloads"
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$DownloadRoot = [IO.Path]::GetFullPath($DownloadRoot)

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

function Remove-ManagedDirectory(
    [string]$Path,
    [string]$ManagedRoot,
    [string]$ExpectedName
) {
    $FullPath = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $RootPrefix = [IO.Path]::GetFullPath($ManagedRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $FullPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a directory outside the managed LLM root: $FullPath"
    }
    if ([IO.Path]::GetFileName($FullPath) -ne $ExpectedName) {
        throw "Refusing to remove unexpected local LLM directory: $FullPath"
    }
    if (Test-Path -LiteralPath $FullPath) {
        Remove-Item -LiteralPath $FullPath -Recurse -Force
    }
}

function Get-PackageDownloadUris($Package) {
    $Uris = @()
    $Multiple = $Package.PSObject.Properties["downloadUrls"]
    if ($null -ne $Multiple) {
        $Uris += @($Multiple.Value | ForEach-Object { [string]$_ })
    }
    $Single = $Package.PSObject.Properties["downloadUrl"]
    if ($null -ne $Single -and [string]$Single.Value) {
        $Uris += [string]$Single.Value
    }
    if ($Uris.Count -eq 0) {
        throw "Package $($Package.displayName) has no download URL."
    }

    $Resolved = @()
    if ($env:HF_ENDPOINT) {
        $HfEndpoint = $env:HF_ENDPOINT.TrimEnd("/")
        foreach ($Uri in $Uris) {
            if ($Uri.StartsWith("https://huggingface.co/", [StringComparison]::OrdinalIgnoreCase)) {
                $Resolved += $HfEndpoint + $Uri.Substring("https://huggingface.co".Length)
            }
        }
    }
    $Resolved += $Uris
    return @($Resolved | Select-Object -Unique)
}

function Get-VerifiedDownload(
    [string[]]$Uris,
    [string]$Destination,
    [string]$Sha256,
    [string]$Description,
    [string]$ExistingFile = ""
) {
    $ExpectedHash = $Sha256.ToLowerInvariant()
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    if (Test-Path -LiteralPath $Destination) {
        $ExistingHash = Get-FileSha256 -Path $Destination
        if ($ExistingHash -eq $ExpectedHash) {
            Write-Host "$Description is already downloaded and verified."
            return $Destination
        }
        Remove-Item -LiteralPath $Destination -Force
    }

    if ($ExistingFile) {
        $ExistingFile = [IO.Path]::GetFullPath($ExistingFile)
        if (-not (Test-Path -LiteralPath $ExistingFile -PathType Leaf)) {
            throw "Existing model file does not exist: $ExistingFile"
        }
        Write-Host "Verifying existing $Description at $ExistingFile ..."
        $SourceHash = Get-FileSha256 -Path $ExistingFile
        if ($SourceHash -ne $ExpectedHash) {
            throw "Existing model checksum mismatch: $ExistingFile"
        }
        try {
            New-Item -ItemType HardLink -Path $Destination -Target $ExistingFile -ErrorAction Stop | Out-Null
            Write-Host "Imported existing model using a hard link; no duplicate model data was created."
        } catch {
            Write-Host "Hard link unavailable; the verified existing model will be referenced in place."
            return $ExistingFile
        }
        $ImportedHash = Get-FileSha256 -Path $Destination
        if ($ImportedHash -ne $ExpectedHash) {
            Remove-Item -LiteralPath $Destination -Force
            throw "Imported model checksum mismatch. The invalid destination was removed."
        }
        return $Destination
    }

    $PartialPath = "$Destination.part"
    $Downloaded = $false
    $LastDownloadError = $null
    foreach ($Uri in $Uris) {
        try {
            Write-Host "Downloading $Description from $Uri ..."
            $Curl = Get-Command "curl.exe" -ErrorAction SilentlyContinue
            if ($null -ne $Curl) {
                & $Curl.Source --location --fail --retry 5 --retry-delay 2 `
                    --continue-at - --output $PartialPath $Uri
                Assert-LastCommand "$Description download"
            } else {
                if (Test-Path -LiteralPath $PartialPath) {
                    Remove-Item -LiteralPath $PartialPath -Force
                }
                Invoke-WebRequest -Uri $Uri -OutFile $PartialPath
            }
            $Downloaded = $true
            break
        } catch {
            $LastDownloadError = $_
            Write-Warning "Download source failed; trying the next source if configured: $Uri"
        }
    }
    if (-not $Downloaded) {
        throw "All download sources failed for $Description. Last error: $LastDownloadError"
    }

    $ActualHash = Get-FileSha256 -Path $PartialPath
    if ($ActualHash -ne $ExpectedHash) {
        Remove-Item -LiteralPath $PartialPath -Force
        throw "$Description checksum mismatch. The invalid download was removed; run setup again."
    }
    Move-Item -LiteralPath $PartialPath -Destination $Destination -Force
    return $Destination
}

if (-not (Test-Path -LiteralPath $CatalogPath)) {
    throw "Missing local LLM package catalog: $CatalogPath"
}
$Catalog = Get-Content -LiteralPath $CatalogPath -Raw | ConvertFrom-Json
if ($Catalog.schemaVersion -ne 1) {
    throw "Unsupported local LLM catalog schema: $($Catalog.schemaVersion)"
}
if (-not $RuntimeId) { $RuntimeId = [string]$Catalog.defaultRuntime }
if (-not $ModelId) { $ModelId = [string]$Catalog.defaultModel }
$RuntimePackage = $Catalog.runtimes.PSObject.Properties[$RuntimeId].Value
$ModelPackage = $Catalog.models.PSObject.Properties[$ModelId].Value
if ($null -eq $RuntimePackage) { throw "Unknown local LLM runtime package: $RuntimeId" }
if ($null -eq $ModelPackage) { throw "Unknown local LLM model package: $ModelId" }

$RuntimeArchive = Join-Path $DownloadRoot ([string]$RuntimePackage.archiveFilename)
$RuntimeInstallRoot = Join-Path (Join-Path $InstallRoot "runtimes") $RuntimeId
$RuntimeExecutable = Join-Path $RuntimeInstallRoot ([string]$RuntimePackage.executable)
$ModelInstallRoot = Join-Path (Join-Path $InstallRoot "models") $ModelId
$ModelFile = Join-Path $ModelInstallRoot ([string]$ModelPackage.filename)

$null = Get-VerifiedDownload `
    -Uris @(Get-PackageDownloadUris -Package $RuntimePackage) `
    -Destination $RuntimeArchive `
    -Sha256 ([string]$RuntimePackage.sha256) `
    -Description ([string]$RuntimePackage.displayName)
if (-not (Test-Path -LiteralPath $RuntimeExecutable)) {
    if (Test-Path -LiteralPath $RuntimeInstallRoot) {
        Remove-ManagedDirectory -Path $RuntimeInstallRoot -ManagedRoot $InstallRoot -ExpectedName $RuntimeId
    }
    New-Item -ItemType Directory -Force -Path $RuntimeInstallRoot | Out-Null
    Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath $RuntimeInstallRoot
}
if (-not (Test-Path -LiteralPath $RuntimeExecutable)) {
    throw "The llama.cpp package does not contain $RuntimeExecutable"
}

$ActiveModelFile = Get-VerifiedDownload `
    -Uris @(Get-PackageDownloadUris -Package $ModelPackage) `
    -Destination $ModelFile `
    -Sha256 ([string]$ModelPackage.sha256) `
    -Description ([string]$ModelPackage.displayName) `
    -ExistingFile $ExistingModelPath
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
Copy-Item -LiteralPath $CatalogPath -Destination (Join-Path $InstallRoot "catalog.json") -Force
$Installation = [ordered]@{
    schemaVersion = 1
    runtimeId = $RuntimeId
    modelId = $ModelId
    modelPath = $ActiveModelFile
}
$InstallationJson = $Installation | ConvertTo-Json
[IO.File]::WriteAllText(
    (Join-Path $InstallRoot "installation.json"),
    $InstallationJson,
    (New-Object Text.UTF8Encoding($false))
)

Write-Host "Local LLM installation completed."
Write-Host "Runtime: $RuntimeExecutable"
Write-Host "Model: $ActiveModelFile"
