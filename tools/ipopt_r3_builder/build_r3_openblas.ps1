[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$ScientificPayload
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:CONDA_PREFIX_DATA_INTEROPERABILITY = 'false'

$BuilderRoot = $PSScriptRoot
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $BuilderRoot '..\..')).Path
. (Join-Path $BuilderRoot 'resolve_builder_commands.ps1')
. (Join-Path $BuilderRoot 'scientific_payload_guard.ps1')
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$ScientificPayload = [IO.Path]::GetFullPath($ScientificPayload)
$Specs = @(
    'python=3.12.13', 'numpy=2.5.2', 'scipy=1.18.0', 'cyipopt=1.7.0',
    'ipopt=3.14.19', 'mumps-seq=5.8.2', 'pandas=3.0.5', 'pyarrow=25.0.0',
    'pyproj=3.7.2', 'pyyaml=6.0.3', 'pytest=9.1.1',
    'libblas=*=*openblas', 'libcblas=*=*openblas', 'liblapack=*=*openblas', 'libopenblas'
)

function Write-Utf8NoBom {
    param([string]$Path, [AllowEmptyString()][string]$Value)
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    [IO.File]::WriteAllText($Path, $Value, [Text.UTF8Encoding]::new($false))
}

function Invoke-NativeCaptured {
    param(
        [string]$FilePath, [string[]]$Arguments, [string]$StdoutPath, [string]$StderrPath,
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

function Invoke-Probe {
    param(
        [string]$Python, [string]$Mode, [string]$Kind, [string]$Output,
        [string]$Module = '', [switch]$AllowFailure
    )
    $arguments = @($ProbeScript, '--kind', $Kind, '--mode', $Mode, '--output', $Output)
    if ($Module) { $arguments += @('--module', $Module) }
    if ($Module -eq 'callback_stack') { $arguments += @('--project-root', $ScientificRoot) }
    if ($Mode -in @('activated', 'relocated')) {
        $command = "call `"$ActivePrefix\Scripts\activate.bat`" && `"$Python`" " + (($arguments | ForEach-Object { '"' + ($_ -replace '"','\"') + '"' }) -join ' ')
        return Invoke-NativeCaptured $CmdExe @('/d', '/s', '/c', $command) ($Output + '.stdout.txt') ($Output + '.stderr.txt') -AllowFailure:$AllowFailure
    }
    return Invoke-NativeCaptured $Python $arguments ($Output + '.stdout.txt') ($Output + '.stderr.txt') -AllowFailure:$AllowFailure
}

function Merge-Probes {
    param([string[]]$Paths, [string]$Output, [string]$Classification)
    $records = @($Paths | ForEach-Object { Get-Content -LiteralPath $_ -Raw | ConvertFrom-Json })
    $passed = @($records | Where-Object { -not $_.passed }).Count -eq 0
    $document = [ordered]@{ classification = if ($passed) { $Classification } else { $Classification -replace '_PASS$', '_FAILURE' }; passed = $passed; expected = $Paths.Count; passed_count = @($records | Where-Object passed).Count; records = $records }
    Write-Utf8NoBom $Output (($document | ConvertTo-Json -Depth 100) + "`n")
    if (-not $passed) { throw "$Classification probe gate failed" }
}

if ($env:RUNNER_OS -ne 'Windows' -or $env:RUNNER_ARCH -ne 'X64' -or -not $env:GITHUB_ACTIONS) {
    throw 'R3 builder requires a GitHub-hosted Windows x64 runner.'
}
if (-not $env:RUNNER_TEMP) { throw 'RUNNER_TEMP is missing.' }
$payloadRecord = Get-R3ScientificPayloadRecord -ScientificPayload $ScientificPayload
if (Test-Path -LiteralPath $OutputDirectory) { throw "Refusing to overwrite output directory: $OutputDirectory" }
if (-not $env:CONDA -or -not (Test-Path -LiteralPath $env:CONDA -PathType Container)) {
    throw 'STAGE1B6J_R3_BUILDER_CONDA_PROVENANCE_FAILURE: setup-miniconda CONDA root is missing.'
}

$runToken = "$($env:GITHUB_RUN_ID)_$($env:GITHUB_RUN_ATTEMPT)"
$WorkRoot = Join-Path $env:RUNNER_TEMP "phase4c_stage1b6j_r3_$runToken"
$Candidate = Join-Path $WorkRoot 'candidate'
$Relocated = Join-Path $WorkRoot 'relocated_candidate'
$ScientificRoot = Join-Path $WorkRoot 'scientific_payload'
$Logs = Join-Path $WorkRoot 'logs'
$ProbeOutput = Join-Path $OutputDirectory 'probe_records'
$ProbeScript = Join-Path $BuilderRoot 'r3_runtime_probe.py'
$AuditScript = Join-Path $BuilderRoot 'r3_audit.py'
$ScientificScript = Join-Path $BuilderRoot 'r3_scientific_evaluate.py'
$ReferencePath = Join-Path $ScientificRoot 'tools\ipopt_r3_builder\scientific_reference_states.json'

foreach ($path in @($WorkRoot, $OutputDirectory)) {
    if (Test-Path -LiteralPath $path) { throw "Refusing to overwrite pre-existing path: $path" }
    New-Item -ItemType Directory -Path $path | Out-Null
}
New-Item -ItemType Directory -Path $Logs | Out-Null
New-Item -ItemType Directory -Path $ProbeOutput | Out-Null
New-R3ScientificPayloadExtractionRoot -ScientificRoot $ScientificRoot
Write-Utf8NoBom (Join-Path $OutputDirectory 'github_scientific_payload_source.json') (($payloadRecord | ConvertTo-Json -Depth 10) + "`n")

$condaCommandDiagnostic = Get-R3CommandDiagnostic -Name 'conda'
$condaExeCommandDiagnostic = Get-R3CommandDiagnostic -Name 'conda.exe'
$condaResolution = Select-R3CondaExecutable `
    -CondaExeEnvironment ([string]$env:CONDA_EXE) `
    -CondaCommandType ([string]$condaCommandDiagnostic.command_type) `
    -CondaCommandSource ([string]$condaCommandDiagnostic.source) `
    -ExpectedRoot $env:CONDA
$CondaExe = $condaResolution.Path

$expectedBuilderPrefix = if ($env:CONDA_PREFIX) {
    (Resolve-Path -LiteralPath $env:CONDA_PREFIX -ErrorAction Stop).Path
} else {
    Join-Path $env:CONDA 'envs\phase4c_r3_builder'
}
if (-not (Test-Path -LiteralPath $expectedBuilderPrefix -PathType Container) -or
        -not (Test-R3PathUnderRoot -Path $expectedBuilderPrefix -Root $env:CONDA)) {
    throw "STAGE1B6J_R3_BUILDER_TOOL_PROVENANCE_FAILURE: builder environment is outside setup-miniconda root: $expectedBuilderPrefix"
}
$builderPythonResolution = Resolve-R3ApplicationCommand -Name 'python.exe' `
    -ExplicitPath (Join-Path $expectedBuilderPrefix 'python.exe') `
    -ExpectedRoot $expectedBuilderPrefix -RequireExpectedRoot
$condaPackResolution = Resolve-R3ApplicationCommand -Name 'conda-pack.exe' `
    -ExplicitPath (Join-Path $expectedBuilderPrefix 'Scripts\conda-pack.exe') `
    -ExpectedRoot $expectedBuilderPrefix -RequireExpectedRoot
$cmdResolution = Resolve-R3ApplicationCommand -Name 'cmd.exe' -ExplicitPath ([string]$env:COMSPEC) -ExpectedRoot ''
$tarResolution = Resolve-R3ApplicationCommand -Name 'tar.exe' -ExplicitPath '' -ExpectedRoot ''
$BuilderPython = $builderPythonResolution.Path
$CondaPackExe = $condaPackResolution.Path
$CmdExe = $cmdResolution.Path
$TarExe = $tarResolution.Path

$condaInfoPath = Join-Path $OutputDirectory 'conda_info.json'
$resolutionDiagnosticPath = Join-Path $OutputDirectory 'github_builder_command_resolution.json'
$resolutionDiagnostic = [ordered]@{
    classification = 'STAGE1B6J_R3_BUILDER_COMMAND_RESOLUTION_PENDING_NATIVE_VALIDATION'
    conda_environment = [ordered]@{
        CONDA = $env:CONDA
        CONDA_EXE = $env:CONDA_EXE
        CONDA_PREFIX = $env:CONDA_PREFIX
        CONDA_DEFAULT_ENV = $env:CONDA_DEFAULT_ENV
    }
    get_command_conda = $condaCommandDiagnostic
    get_command_conda_exe = $condaExeCommandDiagnostic
    resolved_conda = [ordered]@{
        path = $CondaExe
        resolution_source = $condaResolution.ResolutionSource
        expected_setup_miniconda_root = $condaResolution.ExpectedRoot
    }
    adjacent_commands = [ordered]@{
        python = $builderPythonResolution
        conda_pack = $condaPackResolution
        cmd = $cmdResolution
        tar = $tarResolution
    }
    conda_version = $null
    conda_info_json_path = $condaInfoPath
}
Write-Utf8NoBom $resolutionDiagnosticPath (($resolutionDiagnostic | ConvertTo-Json -Depth 20) + "`n")

Invoke-NativeCaptured $TarExe @('-xf', $ScientificPayload, '-C', $ScientificRoot) (Join-Path $Logs 'scientific_payload_extract.stdout.txt') (Join-Path $Logs 'scientific_payload_extract.stderr.txt') | Out-Null
$payloadStructure = Assert-R3ScientificPayloadStructure -ScientificRoot $ScientificRoot
Write-Utf8NoBom (Join-Path $OutputDirectory 'github_scientific_payload_structure.json') (($payloadStructure | ConvertTo-Json -Depth 10) + "`n")

$runner = [ordered]@{
    repository = $env:GITHUB_REPOSITORY; workflow = $env:GITHUB_WORKFLOW; run_id = $env:GITHUB_RUN_ID
    run_attempt = $env:GITHUB_RUN_ATTEMPT; commit_sha = $env:GITHUB_SHA; branch = $env:GITHUB_REF_NAME
    runner_os = $env:RUNNER_OS; runner_arch = $env:RUNNER_ARCH; image_os = $env:ImageOS
    image_version = $env:ImageVersion; runner_name = $env:RUNNER_NAME; self_hosted = $false
}
Write-Utf8NoBom (Join-Path $OutputDirectory 'github_runner_metadata.json') (($runner | ConvertTo-Json -Depth 10) + "`n")

$protectedManifest = $payloadStructure.protected_bootstrap_manifest
Invoke-NativeCaptured $BuilderPython @($AuditScript, 'protected', '--root', $ScientificRoot, '--manifest', $protectedManifest, '--output', (Join-Path $OutputDirectory 'github_protected_preflight.json')) (Join-Path $Logs 'protected_preflight.stdout.txt') (Join-Path $Logs 'protected_preflight.stderr.txt') | Out-Null

$condaVersion = Invoke-NativeCaptured $CondaExe @('--version') (Join-Path $Logs 'conda_version.stdout.txt') (Join-Path $Logs 'conda_version.stderr.txt')
$condaInfo = Invoke-NativeCaptured $CondaExe @('info', '--json') $condaInfoPath (Join-Path $Logs 'conda_info.stderr.txt')
$resolutionDiagnostic.classification = 'STAGE1B6J_R3_BUILDER_COMMAND_RESOLUTION_PASS'
$resolutionDiagnostic.conda_version = $condaVersion.Stdout.Trim()
Write-Utf8NoBom $resolutionDiagnosticPath (($resolutionDiagnostic | ConvertTo-Json -Depth 20) + "`n")
$dryRunPath = Join-Path $OutputDirectory 'github_dry_run.json'
$dryRunArgs = @('create', '--dry-run', '--json', '--strict-channel-priority', '--override-channels', '--channel', 'conda-forge', '--prefix', $Candidate) + $Specs
$dryRun = Invoke-NativeCaptured $CondaExe $dryRunArgs $dryRunPath (Join-Path $Logs 'github_dry_run.stderr.txt') -AllowFailure
if ($dryRun.ExitCode -ne 0) {
    Invoke-NativeCaptured $BuilderPython @($AuditScript, 'classify', '--dry-run', $dryRunPath, '--stderr', (Join-Path $Logs 'github_dry_run.stderr.txt'), '--exit-code', [string]$dryRun.ExitCode, '--output', (Join-Path $OutputDirectory 'github_dry_run_failure.json')) (Join-Path $Logs 'classify.stdout.txt') (Join-Path $Logs 'classify.stderr.txt') -AllowFailure | Out-Null
    throw "Dry-run failed closed with native exit code $($dryRun.ExitCode)."
}

$PackagePlan = Join-Path $OutputDirectory 'github_package_plan.json'
$PackagePlanRejectionSummary = Join-Path $OutputDirectory 'github_package_plan_rejection_summary.json'
$planAudit = Invoke-NativeCaptured `
    $BuilderPython `
    @($AuditScript, 'plan', '--dry-run', $dryRunPath, '--output', $PackagePlan, '--rejection-summary', $PackagePlanRejectionSummary, '--conda-info', $condaInfoPath) `
    (Join-Path $Logs 'plan_audit.stdout.txt') `
    (Join-Path $Logs 'plan_audit.stderr.txt') `
    -AllowFailure
if ($planAudit.ExitCode -ne 0) {
    Copy-Item -LiteralPath $Logs -Destination (Join-Path $OutputDirectory 'logs') -Recurse
    if (Test-Path -LiteralPath $PackagePlanRejectionSummary -PathType Leaf) {
        $rejection = Get-Content -LiteralPath $PackagePlanRejectionSummary -Raw | ConvertFrom-Json
        $compact = [ordered]@{
            classification = $rejection.classification
            package_count = $rejection.package_count
            records_with_dependency_metadata = $rejection.records_with_dependency_metadata
            dependency_metadata_complete = $rejection.dependency_metadata_complete
            failure_gates = $rejection.failure_gates
            dependency_checks_total = $rejection.dependency_checks_total
            unsatisfied_dependency_count = $rejection.unsatisfied_dependency_count
            dependency_interpretation_classification = $rejection.dependency_interpretation_classification
        }
        Write-Output ('R3 package-plan rejection summary: ' + ($compact | ConvertTo-Json -Compress))
    }
    throw 'STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_AUDIT_REJECTED'
}

$createArgs = @('create', '--yes', '--strict-channel-priority', '--override-channels', '--channel', 'conda-forge', '--prefix', $Candidate) + $Specs
Invoke-NativeCaptured $CondaExe $createArgs (Join-Path $Logs 'environment_creation.stdout.txt') (Join-Path $Logs 'environment_creation.stderr.txt') | Out-Null
$CandidatePython = Join-Path $Candidate 'python.exe'
if (-not (Test-Path -LiteralPath $CandidatePython -PathType Leaf)) { throw 'Candidate Python was not created.' }
Invoke-NativeCaptured $CondaExe @('list', '--prefix', $Candidate, '--explicit') (Join-Path $OutputDirectory 'installed_conda_explicit.txt') (Join-Path $Logs 'conda_explicit.stderr.txt') | Out-Null
$ReceiptAudit = Join-Path $OutputDirectory 'github_receipt_and_file_ownership.json'
$InstalledCondaList = Join-Path $OutputDirectory 'installed_conda_list.json'
$ReceiptAuditStdout = Join-Path $Logs 'receipt_audit.stdout.txt'
$ReceiptAuditStderr = Join-Path $Logs 'receipt_audit.stderr.txt'
$ReceiptAuditResult = Invoke-NativeCaptured `
    $BuilderPython `
    @($AuditScript, 'receipts', '--plan', $PackagePlan, '--prefix', $Candidate, '--output', $ReceiptAudit, '--installed-list-output', $InstalledCondaList) `
    $ReceiptAuditStdout `
    $ReceiptAuditStderr `
    -AllowFailure
if ($ReceiptAuditResult.ExitCode -ne 0) {
    Copy-Item -LiteralPath $Logs -Destination (Join-Path $OutputDirectory 'logs') -Recurse
    $plannedCount = $null
    $installedCount = $null
    $missing = $null
    $extra = $null
    $criticalConflicts = $null
    if (Test-Path -LiteralPath $ReceiptAudit -PathType Leaf) {
        try {
            $receiptDiagnostic = Get-Content -LiteralPath $ReceiptAudit -Raw | ConvertFrom-Json
            $plannedCount = $receiptDiagnostic.plan_vs_receipts.planned_count
            $installedCount = $receiptDiagnostic.plan_vs_receipts.installed_count
            $missing = $receiptDiagnostic.plan_vs_receipts.missing
            $extra = $receiptDiagnostic.plan_vs_receipts.extra
            $criticalConflicts = $receiptDiagnostic.ownership.critical_conflicts
        } catch {
            Write-Output "R3 receipt-audit diagnostic parse failure: $($_.Exception.Message)"
        }
    }
    $compact = [ordered]@{
        planned_count = $plannedCount
        installed_count = $installedCount
        missing = $missing
        extra = $extra
        critical_conflicts = $criticalConflicts
    }
    Write-Output ('R3 receipt-audit rejection summary: ' + ($compact | ConvertTo-Json -Compress -Depth 20))
    throw 'STAGE1B6J_R3_GITHUB_RECEIPT_AND_FILE_OWNERSHIP_AUDIT_REJECTED'
}

$ActivePrefix = $Candidate
$dllPaths = @()
foreach ($mode in @('raw', 'activated')) {
    $path = Join-Path $ProbeOutput "github_dll_$mode.json"
    $dllProbeResult = Invoke-Probe $CandidatePython $mode 'dll' $path -AllowFailure
    if ($dllProbeResult.ExitCode -ne 0) {
        $classification = $null
        $externalNumericalModules = $null
        $mklOrIntelThreadModules = $null
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            try {
                $dllDiagnostic = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
                $classification = $dllDiagnostic.classification
                $externalNumericalModules = $dllDiagnostic.external_numerical_modules
                $mklOrIntelThreadModules = $dllDiagnostic.mkl_or_intel_thread_modules
            } catch {
                Write-Output "R3 DLL-probe diagnostic parse failure: $($_.Exception.Message)"
            }
        }
        $compact = [ordered]@{
            mode = $mode
            classification = $classification
            external_numerical_modules = $externalNumericalModules
            mkl_or_intel_thread_modules = $mklOrIntelThreadModules
        }
        Write-Output ('R3 DLL-probe rejection summary: ' + ($compact | ConvertTo-Json -Compress -Depth 20))
        throw 'STAGE1B6J_R3_GITHUB_DLL_RESOLUTION_PROBE_REJECTED'
    }
    $dllPaths += $path
}
Merge-Probes $dllPaths (Join-Path $OutputDirectory 'github_dll_resolution.json') 'STAGE1B6J_R3_GITHUB_DLL_RESOLUTION_PASS'

$importPaths = @()
foreach ($mode in @('raw', 'activated')) {
    foreach ($module in @('numpy', 'scipy', 'cyipopt', 'callback_stack')) {
        for ($iteration = 1; $iteration -le 5; $iteration++) {
            $path = Join-Path $ProbeOutput "github_import_${mode}_${module}_${iteration}.json"
            Invoke-Probe $CandidatePython $mode 'import' $path $module | Out-Null
            $importPaths += $path
        }
    }
}
Merge-Probes $importPaths (Join-Path $OutputDirectory 'github_import_smoke.json') 'STAGE1B6J_R3_GITHUB_IMPORT_40_OF_40_PASS'

$blasPaths = @()
foreach ($mode in @('raw', 'activated')) {
    for ($iteration = 1; $iteration -le 10; $iteration++) {
        $path = Join-Path $ProbeOutput "github_blas_${mode}_${iteration}.json"
        Invoke-Probe $CandidatePython $mode 'blas' $path | Out-Null
        $blasPaths += $path
    }
}
Merge-Probes $blasPaths (Join-Path $OutputDirectory 'github_openblas_execution.json') 'STAGE1B6J_R3_GITHUB_OPENBLAS_20_OF_20_PASS'

$ipoptPaths = @()
foreach ($mode in @('raw', 'activated')) {
    for ($iteration = 1; $iteration -le 5; $iteration++) {
        $path = Join-Path $ProbeOutput "github_ipopt_${mode}_${iteration}.json"
        Invoke-Probe $CandidatePython $mode 'ipopt' $path | Out-Null
        $ipoptPaths += $path
    }
}
Merge-Probes $ipoptPaths (Join-Path $OutputDirectory 'github_ipopt_smoke.json') 'STAGE1B6J_R3_GITHUB_IPOPT_10_OF_10_PASS'

$scientificOutput = Join-Path $OutputDirectory 'github_scientific_equivalence.json'
Invoke-NativeCaptured $CandidatePython @($ScientificScript, '--root', $ScientificRoot, '--reference', $ReferencePath, '--output', $scientificOutput) (Join-Path $Logs 'scientific.stdout.txt') (Join-Path $Logs 'scientific.stderr.txt') | Out-Null
Invoke-NativeCaptured $BuilderPython @($AuditScript, 'protected', '--root', $ScientificRoot, '--manifest', $protectedManifest, '--output', (Join-Path $OutputDirectory 'github_protected_postcheck.json')) (Join-Path $Logs 'protected_postcheck.stdout.txt') (Join-Path $Logs 'protected_postcheck.stderr.txt') | Out-Null

$Archive = Join-Path $OutputDirectory 'phase4c_stage1b6j_r3_openblas_runtime.tar.gz'
Invoke-NativeCaptured $CondaPackExe @('--prefix', $Candidate, '--output', $Archive, '--format', 'tar.gz', '--force') (Join-Path $Logs 'conda_pack.stdout.txt') (Join-Path $Logs 'conda_pack.stderr.txt') | Out-Null
New-Item -ItemType Directory -Path $Relocated | Out-Null
Invoke-NativeCaptured $TarExe @('-xzf', $Archive, '-C', $Relocated) (Join-Path $Logs 'relocation_extract.stdout.txt') (Join-Path $Logs 'relocation_extract.stderr.txt') | Out-Null
$RelocatedPython = Join-Path $Relocated 'python.exe'
$RelocatedUnpack = Join-Path $Relocated 'Scripts\conda-unpack.exe'
Invoke-NativeCaptured $RelocatedUnpack @() (Join-Path $Logs 'conda_unpack.stdout.txt') (Join-Path $Logs 'conda_unpack.stderr.txt') | Out-Null
$ActivePrefix = $Relocated
$relocationPaths = @()
foreach ($module in @('numpy', 'scipy', 'cyipopt')) {
    $path = Join-Path $ProbeOutput "relocated_import_${module}.json"
    Invoke-Probe $RelocatedPython 'relocated' 'import' $path $module | Out-Null
    $relocationPaths += $path
}
$path = Join-Path $ProbeOutput 'relocated_blas.json'; Invoke-Probe $RelocatedPython 'relocated' 'blas' $path | Out-Null; $relocationPaths += $path
$path = Join-Path $ProbeOutput 'relocated_ipopt.json'; Invoke-Probe $RelocatedPython 'relocated' 'ipopt' $path | Out-Null; $relocationPaths += $path
$path = Join-Path $ProbeOutput 'relocated_dll.json'; Invoke-Probe $RelocatedPython 'relocated' 'dll' $path | Out-Null; $relocationPaths += $path
Merge-Probes $relocationPaths (Join-Path $OutputDirectory 'github_relocation_smoke.json') 'STAGE1B6J_R3_GITHUB_ARTIFACT_RELOCATION_PASS'

Copy-Item -LiteralPath $ScientificPayload -Destination (Join-Path $OutputDirectory 'phase4c_stage1b6j_r3_scientific_payload.zip')
$summary = [ordered]@{
    classification = 'STAGE1B6J_R3_OPENBLAS_RUNTIME_ARTIFACT_BUILT'
    backend_change = 'STAGE1B6J_RUNTIME_BACKEND_CHANGE_MKL_TO_OPENBLAS'
    stage1b6j_executed = $false; optimization_executed = $false; accepted_solver_iterations = 0
    runner = $runner; conda_version = $condaVersion.Stdout.Trim(); dry_run_command = 'conda ' + ($dryRunArgs -join ' ')
    install_command = 'conda ' + ($createArgs -join ' '); candidate_prefix = $Candidate
    runtime_archive = [ordered]@{ filename = (Split-Path -Leaf $Archive); size_bytes = (Get-Item $Archive).Length; sha256 = (Get-FileHash $Archive -Algorithm SHA256).Hash.ToLowerInvariant() }
}
Write-Utf8NoBom (Join-Path $OutputDirectory 'github_runtime_summary.json') (($summary | ConvertTo-Json -Depth 20) + "`n")
Copy-Item -LiteralPath $Logs -Destination (Join-Path $OutputDirectory 'logs') -Recurse
Invoke-NativeCaptured $BuilderPython @($AuditScript, 'hash-manifest', '--root', $OutputDirectory, '--output', (Join-Path $OutputDirectory 'sha256_manifest.json')) (Join-Path $WorkRoot 'hash_manifest.stdout.txt') (Join-Path $WorkRoot 'hash_manifest.stderr.txt') | Out-Null

Write-Output 'STAGE1B6J_R3_OPENBLAS_RUNTIME_ARTIFACT_BUILT'

