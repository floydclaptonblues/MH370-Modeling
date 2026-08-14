[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ArtifactDirectory,
    [Parameter(Mandatory = $true)][string]$LocalPrefix,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$BuilderRoot = $PSScriptRoot
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $BuilderRoot '..\..')).Path
$ArtifactDirectory = (Resolve-Path -LiteralPath $ArtifactDirectory).Path
$LocalPrefix = [IO.Path]::GetFullPath($LocalPrefix)
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$AuditScript = Join-Path $BuilderRoot 'r3_audit.py'
$ProbeScript = Join-Path $BuilderRoot 'r3_runtime_probe.py'
$ScientificScript = Join-Path $BuilderRoot 'r3_scientific_evaluate.py'
$Archive = Join-Path $ArtifactDirectory 'phase4c_stage1b6j_r3_openblas_runtime.tar.gz'
$ScientificPayload = Join-Path $ArtifactDirectory 'phase4c_stage1b6j_r3_scientific_payload.zip'
$Manifest = Join-Path $ArtifactDirectory 'sha256_manifest.json'

function Write-Utf8NoBom {
    param([string]$Path, [AllowEmptyString()][string]$Value)
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    [IO.File]::WriteAllText($Path, $Value, [Text.UTF8Encoding]::new($false))
}

function Invoke-NativeCaptured {
    param([string]$FilePath, [string[]]$Arguments, [string]$StdoutPath, [string]$StderrPath)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $FilePath; $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    foreach ($argument in $Arguments) { [void]$start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $start
    if (-not $process.Start()) { throw "Failed to start $FilePath" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync(); $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit(); $stdout = $stdoutTask.GetAwaiter().GetResult(); $stderr = $stderrTask.GetAwaiter().GetResult(); $code = $process.ExitCode; $process.Dispose()
    Write-Utf8NoBom $StdoutPath $stdout; Write-Utf8NoBom $StderrPath $stderr
    if ($code -ne 0) { throw "Native command failed exit=${code}: $FilePath $($Arguments -join ' ')" }
}

function Invoke-Probe {
    param([string]$Mode, [string]$Kind, [string]$Output, [string]$Module = '')
    $arguments = @($ProbeScript, '--kind', $Kind, '--mode', $Mode, '--output', $Output)
    if ($Module) { $arguments += @('--module', $Module) }
    if ($Module -eq 'callback_stack') { $arguments += @('--project-root', $ScientificRoot) }
    if ($Mode -eq 'local_activated') {
        $command = "call `"$LocalPrefix\Scripts\activate.bat`" && `"$Python`" " + (($arguments | ForEach-Object { '"' + $_ + '"' }) -join ' ')
        Invoke-NativeCaptured $CmdExe @('/d', '/s', '/c', $command) ($Output + '.stdout.txt') ($Output + '.stderr.txt')
    } else {
        Invoke-NativeCaptured $Python $arguments ($Output + '.stdout.txt') ($Output + '.stderr.txt')
    }
}

function Merge-Probes {
    param([string[]]$Paths, [string]$Output, [string]$Classification)
    $records = @($Paths | ForEach-Object { Get-Content -LiteralPath $_ -Raw | ConvertFrom-Json })
    $failed = @($records | Where-Object { -not $_.passed })
    $document = [ordered]@{ classification = if ($failed.Count -eq 0) { $Classification } else { $Classification -replace '_PASS$', '_FAILURE' }; passed = $failed.Count -eq 0; expected = $Paths.Count; passed_count = $Paths.Count - $failed.Count; records = $records }
    Write-Utf8NoBom $Output (($document | ConvertTo-Json -Depth 100) + "`n")
    if ($failed.Count) { throw "$Classification failed" }
}

foreach ($required in @($Archive, $ScientificPayload, $Manifest)) { if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Artifact component missing: $required" } }
foreach ($path in @($LocalPrefix, $OutputDirectory)) { if (Test-Path -LiteralPath $path) { throw "Refusing to overlay pre-existing path: $path" }; New-Item -ItemType Directory -Path $path | Out-Null }
$HostPython = Join-Path $RepositoryRoot 'tools\ipopt_benchmark_env\python.exe'
if (-not (Test-Path -LiteralPath $HostPython -PathType Leaf)) { $HostPython = (Get-Command python.exe -ErrorAction Stop).Source }
$TarExe = (Get-Command tar.exe -ErrorAction Stop).Source
$CmdExe = if ($env:COMSPEC) { $env:COMSPEC } else { (Get-Command cmd.exe -ErrorAction Stop).Source }
$ProbeRoot = Join-Path $OutputDirectory 'probe_records'; New-Item -ItemType Directory -Path $ProbeRoot | Out-Null
$ScientificRoot = Join-Path $OutputDirectory 'scientific_payload'; New-Item -ItemType Directory -Path $ScientificRoot | Out-Null

$manifestDocument = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$failures = @()
foreach ($row in $manifestDocument.records) {
    $path = Join-Path $ArtifactDirectory ([string]$row.path)
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $failures += "$($row.path):MISSING"; continue }
    if ((Get-Item -LiteralPath $path).Length -ne [long]$row.size_bytes) { $failures += "$($row.path):SIZE"; continue }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$row.sha256) { $failures += "$($row.path):SHA256" }
}
$integrity = [ordered]@{ passed = $failures.Count -eq 0; expected = $manifestDocument.records.Count; failures = $failures }
Write-Utf8NoBom (Join-Path $OutputDirectory 'local_artifact_integrity.json') (($integrity | ConvertTo-Json -Depth 10) + "`n")
if ($failures.Count) { throw 'Artifact hash manifest failed.' }

Invoke-NativeCaptured $TarExe @('-xzf', $Archive, '-C', $LocalPrefix) (Join-Path $OutputDirectory 'extract.stdout.txt') (Join-Path $OutputDirectory 'extract.stderr.txt')
$Python = Join-Path $LocalPrefix 'python.exe'
$Unpack = Join-Path $LocalPrefix 'Scripts\conda-unpack.exe'
Invoke-NativeCaptured $Unpack @() (Join-Path $OutputDirectory 'conda_unpack.stdout.txt') (Join-Path $OutputDirectory 'conda_unpack.stderr.txt')
Invoke-NativeCaptured $TarExe @('-xf', $ScientificPayload, '-C', $ScientificRoot) (Join-Path $OutputDirectory 'scientific_payload_extract.stdout.txt') (Join-Path $OutputDirectory 'scientific_payload_extract.stderr.txt')

$dll = @(); foreach ($mode in @('local_raw', 'local_activated')) { $path = Join-Path $ProbeRoot "dll_$mode.json"; Invoke-Probe $mode 'dll' $path; $dll += $path }
Merge-Probes $dll (Join-Path $OutputDirectory 'local_dll_resolution.json') 'STAGE1B6J_R3_LOCAL_DLL_RESOLUTION_PASS'
$imports = @(); foreach ($mode in @('local_raw', 'local_activated')) { foreach ($module in @('numpy','scipy','cyipopt','callback_stack')) { 1..5 | ForEach-Object { $path = Join-Path $ProbeRoot "import_${mode}_${module}_$_.json"; Invoke-Probe $mode 'import' $path $module; $imports += $path } } }
Merge-Probes $imports (Join-Path $OutputDirectory 'local_import_smoke.json') 'STAGE1B6J_R3_LOCAL_IMPORT_40_OF_40_PASS'
$blas = @(); foreach ($mode in @('local_raw','local_activated')) { 1..10 | ForEach-Object { $path = Join-Path $ProbeRoot "blas_${mode}_$_.json"; Invoke-Probe $mode 'blas' $path; $blas += $path } }
Merge-Probes $blas (Join-Path $OutputDirectory 'local_openblas_execution.json') 'STAGE1B6J_R3_LOCAL_OPENBLAS_20_OF_20_PASS'
$ipopt = @(); foreach ($mode in @('local_raw','local_activated')) { 1..5 | ForEach-Object { $path = Join-Path $ProbeRoot "ipopt_${mode}_$_.json"; Invoke-Probe $mode 'ipopt' $path; $ipopt += $path } }
Merge-Probes $ipopt (Join-Path $OutputDirectory 'local_ipopt_smoke.json') 'STAGE1B6J_R3_LOCAL_IPOPT_10_OF_10_PASS'

$Reference = Join-Path $ScientificRoot 'tools\ipopt_r3_builder\scientific_reference_states.json'
Invoke-NativeCaptured $Python @($ScientificScript, '--root', $ScientificRoot, '--reference', $Reference, '--output', (Join-Path $OutputDirectory 'local_scientific_equivalence.json')) (Join-Path $OutputDirectory 'scientific.stdout.txt') (Join-Path $OutputDirectory 'scientific.stderr.txt')
$ProtectedManifest = Join-Path $ScientificRoot 'provenance\phase4c_stage1b6f_bootstrap_input_hashes.json'
Invoke-NativeCaptured $HostPython @($AuditScript, 'protected', '--root', $RepositoryRoot, '--manifest', $ProtectedManifest, '--output', (Join-Path $OutputDirectory 'local_protected_postcheck.json')) (Join-Path $OutputDirectory 'protected.stdout.txt') (Join-Path $OutputDirectory 'protected.stderr.txt')

Write-Output 'STAGE1B6J_R3_GITHUB_AND_LOCAL_RUNTIME_VALIDATION_PASS'
