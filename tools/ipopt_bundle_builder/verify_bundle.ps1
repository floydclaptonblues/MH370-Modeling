[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BundleDirectory,

    [Parameter(Mandatory = $false)]
    [string]$ZipPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-Sha256Lower {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$bundle = (Resolve-Path -LiteralPath $BundleDirectory).Path
$required = @(
    'ipopt_benchmark_env.tar.gz',
    'conda_explicit.txt',
    'conda_list.json',
    'environment_history.yml',
    'build_environment.json',
    'interface_capabilities.json',
    'native_library_manifest.json',
    'smoke_test.py',
    'smoke_test_stdout.txt',
    'smoke_test_stderr.txt',
    'smoke_test_result.json',
    'relocation_smoke_test_stdout.txt',
    'relocation_smoke_test_stderr.txt',
    'relocation_smoke_test_result.json',
    'sha256_manifest.json',
    'README_TARGET_RECOVERY.txt',
    'build_bundle.ps1',
    'verify_bundle.ps1',
    'inspect_environment.py',
    'build-ipopt-bootstrap-bundle.yml'
)

foreach ($name in $required) {
    $target = Join-Path $bundle $name
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        throw "Required bundle file is missing: $name"
    }
}

$hashManifestPath = Join-Path $bundle 'sha256_manifest.json'
$hashManifest = Get-Content -LiteralPath $hashManifestPath -Raw | ConvertFrom-Json
if (-not $hashManifest.self_excluded_to_avoid_recursive_hash) {
    throw 'sha256_manifest.json must explicitly document its recursive self-hash exclusion.'
}

$recordNames = @($hashManifest.records | ForEach-Object { [string]$_.filename })
$actualNames = @(
    Get-ChildItem -LiteralPath $bundle -File |
        Where-Object { $_.Name -ne 'sha256_manifest.json' } |
        ForEach-Object { $_.Name }
)
$nameDifference = Compare-Object -ReferenceObject ($recordNames | Sort-Object) -DifferenceObject ($actualNames | Sort-Object)
if ($nameDifference) {
    throw "Top-level hash-manifest coverage mismatch: $($nameDifference | Out-String)"
}

foreach ($record in $hashManifest.records) {
    $target = Join-Path $bundle ([string]$record.filename)
    $item = Get-Item -LiteralPath $target
    if ($item.Length -ne [int64]$record.size_bytes) {
        throw "Byte-size mismatch for $($record.filename)"
    }
    if ((Get-Sha256Lower -Path $target) -ne ([string]$record.sha256).ToLowerInvariant()) {
        throw "SHA-256 mismatch for $($record.filename)"
    }
}

$initialSmoke = Get-Content -LiteralPath (Join-Path $bundle 'smoke_test_result.json') -Raw | ConvertFrom-Json
if ($initialSmoke.classification -ne 'IPOPT_EXTERNAL_SMOKE_TEST_PASS') {
    throw "Initial smoke test did not pass: $($initialSmoke.classification)"
}
$relocationSmoke = Get-Content -LiteralPath (Join-Path $bundle 'relocation_smoke_test_result.json') -Raw | ConvertFrom-Json
if ($relocationSmoke.relocation_classification -ne 'IPOPT_EXTERNAL_BUNDLE_RELOCATION_PASS') {
    throw "Relocation rehearsal did not pass: $($relocationSmoke.relocation_classification)"
}
$native = Get-Content -LiteralPath (Join-Path $bundle 'native_library_manifest.json') -Raw | ConvertFrom-Json
$nativeClasses = @($native.records | ForEach-Object { [string]$_.classification })
if ('cyipopt_compiled_extension' -notin $nativeClasses -or 'native_ipopt_library' -notin $nativeClasses) {
    throw 'Native manifest lacks the cyipopt extension or native Ipopt library.'
}

if ($ZipPath) {
    $zip = (Resolve-Path -LiteralPath $ZipPath).Path
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $prefix = (Split-Path -Leaf $bundle) + '/'
        $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
        foreach ($name in $required) {
            if (($prefix + $name) -notin $entries) {
                throw "Outer ZIP is missing $prefix$name"
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

Write-Output 'IPOPT_EXTERNAL_BUNDLE_SOURCE_VERIFICATION_PASS'
