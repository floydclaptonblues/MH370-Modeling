[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$BundleName = 'phase4c_stage1b6f_openblas_successor_bundle'
$ArchiveName = 'ipopt_benchmark_env_openblas_run9.tar.gz'
$ZipName = "$BundleName.zip"
$BuilderRoot = $PSScriptRoot
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $BuilderRoot '..\..')).Path
$AuditScript = Join-Path $BuilderRoot 'successor_audit.py'
$ProbeScript = Join-Path $BuilderRoot 'runtime_probe.py'
$Specs = @(
    'python=3.12.13', 'numpy=2.5.2', 'scipy=1.18.0', 'cyipopt=1.7.0',
    'ipopt=3.14.19', 'mumps-seq=5.8.2', 'pandas=3.0.5', 'pyarrow=25.0.0',
    'pyproj=3.7.2', 'pyyaml=6.0.3', 'pytest=9.1.1', 'conda-pack',
    'libblas=*=*openblas', 'libcblas=*=*openblas',
    'liblapack=*=*openblas', 'libopenblas'
)

function Write-Utf8NoBom {
    param([string]$Path, [AllowEmptyString()][string]$Value)
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    [IO.File]::WriteAllText($Path, $Value, [Text.UTF8Encoding]::new($false))
}

function Invoke-NativeCaptured {
    param(
        [string]$FilePath, [string[]]$Arguments,
        [string]$StdoutPath, [string]$StderrPath,
        [switch]$AllowFailure
    )
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $FilePath
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in $Arguments) { [void]$start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) { throw "Failed to start $FilePath" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $exitCode = $process.ExitCode
    $process.Dispose()
    Write-Utf8NoBom $StdoutPath $stdout
    Write-Utf8NoBom $StderrPath $stderr
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "Native command failed exit=${exitCode}: $FilePath $($Arguments -join ' ')"
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Stdout = $stdout; Stderr = $stderr }
}

function Get-Sha256Lower {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

if ($env:RUNNER_OS -ne 'Windows' -or $env:RUNNER_ARCH -ne 'X64' -or -not $env:GITHUB_ACTIONS) {
    throw 'Successor builder requires a GitHub-hosted Windows x64 runner.'
}
if (-not $env:RUNNER_TEMP -or -not $env:CONDA -or -not $env:CONDA_PREFIX) {
    throw 'setup-miniconda provenance is incomplete.'
}

$CondaExe = [IO.Path]::GetFullPath([string]$env:CONDA_EXE)
$BuilderPython = [IO.Path]::GetFullPath((Join-Path $env:CONDA_PREFIX 'python.exe'))
$CondaPack = [IO.Path]::GetFullPath((Join-Path $env:CONDA_PREFIX 'Scripts\conda-pack.exe'))
$expectedCondaRoot = [IO.Path]::GetFullPath([string]$env:CONDA)
foreach ($path in @($CondaExe, $BuilderPython, $CondaPack)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required builder tool is missing: $path" }
    if (-not $path.StartsWith($expectedCondaRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Builder tool is outside setup-miniconda root: $path"
    }
}

$runToken = "$($env:GITHUB_RUN_ID)_$($env:GITHUB_RUN_ATTEMPT)"
$WorkRoot = Join-Path $env:RUNNER_TEMP "phase4c_stage1b6f_openblas_successor_$runToken"
$Candidate = Join-Path $WorkRoot 'candidate'
$Relocated = Join-Path $WorkRoot 'relocated'
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$BundleDirectory = Join-Path $OutputDirectory $BundleName
$Logs = Join-Path $BundleDirectory 'logs'
foreach ($path in @($WorkRoot, $OutputDirectory)) {
    if (Test-Path -LiteralPath $path) { throw "Refusing to overwrite pre-existing path: $path" }
}
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
New-Item -ItemType Directory -Path $BundleDirectory | Out-Null
New-Item -ItemType Directory -Path $Logs | Out-Null

$requested = [ordered]@{
    classification = 'STAGE1B6F_OPENBLAS_SUCCESSOR_EXPLICIT_SPEC'
    specifications = $Specs
    channels = @('conda-forge')
    strict_channel_priority = $true
    prohibited_backend_packages = @('mkl', 'mkl-devel', 'mkl-include', 'mkl-service')
}
Write-Utf8NoBom (Join-Path $BundleDirectory 'requested_specifications.json') (($requested | ConvertTo-Json -Depth 10) + "`n")

Invoke-NativeCaptured $CondaExe @('--version') (Join-Path $BundleDirectory 'conda_version.txt') (Join-Path $Logs 'conda_version.stderr.txt') | Out-Null
$CondaInfo = Join-Path $BundleDirectory 'conda_info.json'
Invoke-NativeCaptured $CondaExe @('info', '--json') $CondaInfo (Join-Path $Logs 'conda_info.stderr.txt') | Out-Null

$DryRunArgs = @(
    'create', '--dry-run', '--json', '--strict-channel-priority',
    '--override-channels', '--channel', 'conda-forge', '--prefix', $Candidate
) + $Specs
$DryRun1 = Join-Path $BundleDirectory 'dry_run_1.json'
$DryRun2 = Join-Path $BundleDirectory 'dry_run_2.json'
Invoke-NativeCaptured $CondaExe $DryRunArgs $DryRun1 (Join-Path $Logs 'dry_run_1.stderr.txt') | Out-Null
$Plan1 = Join-Path $BundleDirectory 'package_plan_1.json'
Invoke-NativeCaptured $BuilderPython @($AuditScript, 'plan', '--dry-run', $DryRun1, '--conda-info', $CondaInfo, '--output', $Plan1) (Join-Path $Logs 'plan_1.stdout.txt') (Join-Path $Logs 'plan_1.stderr.txt') | Out-Null

Invoke-NativeCaptured $CondaExe $DryRunArgs $DryRun2 (Join-Path $Logs 'dry_run_2.stderr.txt') | Out-Null
$Plan2 = Join-Path $BundleDirectory 'package_plan_2.json'
Invoke-NativeCaptured $BuilderPython @($AuditScript, 'plan', '--dry-run', $DryRun2, '--conda-info', $CondaInfo, '--output', $Plan2) (Join-Path $Logs 'plan_2.stdout.txt') (Join-Path $Logs 'plan_2.stderr.txt') | Out-Null
Invoke-NativeCaptured $BuilderPython @($AuditScript, 'compare', '--left', $Plan1, '--right', $Plan2, '--output', (Join-Path $BundleDirectory 'package_plan_reproducibility.json')) (Join-Path $Logs 'plan_compare.stdout.txt') (Join-Path $Logs 'plan_compare.stderr.txt') | Out-Null

$CreateArgs = @(
    'create', '--yes', '--strict-channel-priority', '--override-channels',
    '--channel', 'conda-forge', '--prefix', $Candidate
) + $Specs
Invoke-NativeCaptured $CondaExe $CreateArgs (Join-Path $BundleDirectory 'environment_creation_stdout.txt') (Join-Path $BundleDirectory 'environment_creation_stderr.txt') | Out-Null
$CandidatePython = Join-Path $Candidate 'python.exe'
if (-not (Test-Path -LiteralPath $CandidatePython -PathType Leaf)) { throw 'Candidate Python is missing.' }

$Explicit = Join-Path $BundleDirectory 'installed_conda_explicit.txt'
Invoke-NativeCaptured $CondaExe @('list', '--prefix', $Candidate, '--explicit') $Explicit (Join-Path $Logs 'conda_explicit.stderr.txt') | Out-Null
Invoke-NativeCaptured $CondaExe @('list', '--prefix', $Candidate, '--json') (Join-Path $BundleDirectory 'installed_conda_list.json') (Join-Path $Logs 'conda_list.stderr.txt') | Out-Null
$ReceiptAudit = Join-Path $BundleDirectory 'receipt_audit.json'
Invoke-NativeCaptured $BuilderPython @($AuditScript, 'receipts', '--plan', $Plan1, '--explicit', $Explicit, '--prefix', $Candidate, '--output', $ReceiptAudit) (Join-Path $Logs 'receipt_audit.stdout.txt') (Join-Path $Logs 'receipt_audit.stderr.txt') | Out-Null

$ReceiptDirectory = Join-Path $BundleDirectory 'conda_meta_receipts'
New-Item -ItemType Directory -Path $ReceiptDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $Candidate 'conda-meta\history') -Destination (Join-Path $ReceiptDirectory 'history')
Get-ChildItem -LiteralPath (Join-Path $Candidate 'conda-meta') -Filter '*.json' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $ReceiptDirectory $_.Name)
}

$CandidateProbe = Join-Path $BundleDirectory 'candidate_runtime_probe.json'
Invoke-NativeCaptured $CandidatePython @($ProbeScript, '--mode', 'candidate', '--output', $CandidateProbe) (Join-Path $Logs 'candidate_probe.stdout.txt') (Join-Path $Logs 'candidate_probe.stderr.txt') | Out-Null

$EnvironmentArchive = Join-Path $BundleDirectory $ArchiveName
Invoke-NativeCaptured $CondaPack @('--prefix', $Candidate, '--output', $EnvironmentArchive, '--format', 'tar.gz', '--force') (Join-Path $Logs 'conda_pack.stdout.txt') (Join-Path $Logs 'conda_pack.stderr.txt') | Out-Null
if (-not (Test-Path -LiteralPath $EnvironmentArchive -PathType Leaf) -or (Get-Item $EnvironmentArchive).Length -le 0) {
    throw 'conda-pack did not create a nonempty archive.'
}

New-Item -ItemType Directory -Path $Relocated | Out-Null
$TarExe = (Get-Command tar.exe -ErrorAction Stop).Source
Invoke-NativeCaptured $TarExe @('-xzf', $EnvironmentArchive, '-C', $Relocated) (Join-Path $Logs 'relocation_extract.stdout.txt') (Join-Path $Logs 'relocation_extract.stderr.txt') | Out-Null
$RelocatedPython = Join-Path $Relocated 'python.exe'
$CondaUnpack = Join-Path $Relocated 'Scripts\conda-unpack.exe'
Invoke-NativeCaptured $CondaUnpack @() (Join-Path $Logs 'conda_unpack.stdout.txt') (Join-Path $Logs 'conda_unpack.stderr.txt') | Out-Null
$RelocatedProbe = Join-Path $BundleDirectory 'relocated_runtime_probe.json'
Invoke-NativeCaptured $RelocatedPython @($ProbeScript, '--mode', 'relocated', '--output', $RelocatedProbe) (Join-Path $Logs 'relocated_probe.stdout.txt') (Join-Path $Logs 'relocated_probe.stderr.txt') | Out-Null

foreach ($name in @('successor_audit.py', 'runtime_probe.py', 'README.md')) {
    Copy-Item -LiteralPath (Join-Path $BuilderRoot $name) -Destination (Join-Path $BundleDirectory $name)
}
Copy-Item -LiteralPath (Join-Path $RepositoryRoot '.github\workflows\build-ipopt-openblas-successor.yml') -Destination (Join-Path $BundleDirectory 'build-ipopt-openblas-successor.yml')
Copy-Item -LiteralPath (Join-Path $BuilderRoot 'build_successor.ps1') -Destination (Join-Path $BundleDirectory 'build_successor.ps1')

$plan = Get-Content -LiteralPath $Plan1 -Raw | ConvertFrom-Json
$receipts = Get-Content -LiteralPath $ReceiptAudit -Raw | ConvertFrom-Json
$candidateRuntime = Get-Content -LiteralPath $CandidateProbe -Raw | ConvertFrom-Json
$relocatedRuntime = Get-Content -LiteralPath $RelocatedProbe -Raw | ConvertFrom-Json
$summary = [ordered]@{
    classification = 'STAGE1B6F_OPENBLAS_SUCCESSOR_BUILD_PASS'
    repository = $env:GITHUB_REPOSITORY
    commit_sha = $env:GITHUB_SHA
    branch = $env:GITHUB_REF_NAME
    workflow_run_id = $env:GITHUB_RUN_ID
    workflow_run_attempt = $env:GITHUB_RUN_ATTEMPT
    requested_specs = $Specs
    dry_run_command = 'conda ' + ($DryRunArgs -join ' ')
    creation_command = 'conda ' + ($CreateArgs -join ' ')
    package_count = $plan.package_count
    plan_vs_explicit = $receipts.plan_vs_explicit
    backend_receipts = $receipts.backend_receipts
    candidate_openblas_dlls = $candidateRuntime.openblas_dlls
    relocated_openblas_dlls = $relocatedRuntime.openblas_dlls
    candidate_mkl_dlls = $candidateRuntime.mkl_dlls
    relocated_mkl_dlls = $relocatedRuntime.mkl_dlls
    ipopt_version = $candidateRuntime.module_versions.ipopt
    optimization_executed = $false
    mh370_model_imported_or_executed = $false
    environment_archive = [ordered]@{
        filename = $ArchiveName
        size_bytes = (Get-Item $EnvironmentArchive).Length
        sha256 = Get-Sha256Lower $EnvironmentArchive
    }
}
Write-Utf8NoBom (Join-Path $BundleDirectory 'successor_build_summary.json') (($summary | ConvertTo-Json -Depth 30) + "`n")

$HashManifest = Join-Path $BundleDirectory 'sha256_manifest.json'
Invoke-NativeCaptured $BuilderPython @($AuditScript, 'hash-manifest', '--root', $BundleDirectory, '--output', $HashManifest) (Join-Path $Logs 'hash_manifest.stdout.txt') (Join-Path $Logs 'hash_manifest.stderr.txt') | Out-Null

$ZipPath = Join-Path $OutputDirectory $ZipName
Compress-Archive -LiteralPath $BundleDirectory -DestinationPath $ZipPath -CompressionLevel Optimal
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
try {
    foreach ($entry in $zip.Entries) {
        $stream = $entry.Open()
        try {
            $buffer = New-Object byte[] 1048576
            while ($stream.Read($buffer, 0, $buffer.Length) -gt 0) { }
        } finally { $stream.Dispose() }
    }
} finally { $zip.Dispose() }
$ZipHash = Get-Sha256Lower $ZipPath
Write-Utf8NoBom (Join-Path $OutputDirectory ($ZipName + '.sha256.txt')) "$ZipHash  $ZipName`n"
Write-Output 'STAGE1B6F_OPENBLAS_SUCCESSOR_BUILD_PASS'
Write-Output "ZIP_PATH=$ZipPath"
Write-Output "ZIP_SHA256=$ZipHash"
