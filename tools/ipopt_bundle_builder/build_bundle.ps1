[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$EnvironmentName = 'ipopt_bootstrap'
$BundleName = 'phase4c_stage1b6f_ipopt_env_bundle'
$ArchiveName = 'ipopt_benchmark_env.tar.gz'
$FinalZipName = 'phase4c_stage1b6f_ipopt_env_bundle.zip'
$BuilderRoot = $PSScriptRoot
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $BuilderRoot '..\..')).Path
$DependencyAuditManifestSha256 = 'a83b8013db1525904ca743a5858b028038d25c0230ddb696c8eefbb0f498daff'

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Value, [System.Text.UTF8Encoding]::new($false))
}

function Invoke-NativeCaptured {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $Arguments) {
        [void]$startInfo.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start native process: $FilePath"
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    Write-Utf8NoBom -Path $StdoutPath -Value $stdout
    Write-Utf8NoBom -Path $StderrPath -Value $stderr
    $exitCode = $process.ExitCode
    $process.Dispose()
    if ($exitCode -ne 0) {
        Write-Host '----- native stdout begin -----'
        if ($stdout) { Write-Host $stdout }
        Write-Host '----- native stdout end -----'
        Write-Host '----- native stderr begin -----'
        if ($stderr) { Write-Host $stderr }
        Write-Host '----- native stderr end -----'
        throw "Native process failed with exit code ${exitCode}: $FilePath $($Arguments -join ' ')"
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Stdout = $stdout; Stderr = $stderr }
}

function Get-Sha256Lower {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

if ($env:RUNNER_OS -ne 'Windows') {
    throw "This builder requires a GitHub-hosted native Windows runner; RUNNER_OS=$($env:RUNNER_OS)"
}
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess -or $env:RUNNER_ARCH -ne 'X64') {
    throw 'This builder requires a native Windows x86-64 OS and process.'
}
if (-not $env:RUNNER_TEMP) {
    throw 'RUNNER_TEMP is not defined; refuse to build outside an ephemeral GitHub runner.'
}

$condaCommand = Get-Command conda.exe -ErrorAction Stop
$CondaExe = $condaCommand.Source
$runToken = if ($env:GITHUB_RUN_ID) { "$($env:GITHUB_RUN_ID)_$($env:GITHUB_RUN_ATTEMPT)" } else { [guid]::NewGuid().ToString('N') }
$WorkRoot = Join-Path $env:RUNNER_TEMP "phase4c_stage1b6f_ipopt_bundle_$runToken"
$StageDirectory = Join-Path $WorkRoot $BundleName
$RelocatedDirectory = Join-Path $WorkRoot 'relocated_ipopt_benchmark_env'
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

foreach ($path in @($WorkRoot, $OutputDirectory)) {
    if (Test-Path -LiteralPath $path) {
        throw "Refusing to overwrite pre-existing path: $path"
    }
}
New-Item -ItemType Directory -Path $StageDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$managerResult = Invoke-NativeCaptured -FilePath $CondaExe -Arguments @('--version') -StdoutPath (Join-Path $StageDirectory 'environment_manager_stdout.txt') -StderrPath (Join-Path $StageDirectory 'environment_manager_stderr.txt')
$ManagerVersion = $managerResult.Stdout.Trim()
$CreationArguments = @(
    'create', '--yes', '--name', $EnvironmentName,
    '--override-channels', '--channel', 'conda-forge', '--strict-channel-priority',
    'python=3.12.13',
    'numpy=2.5.2',
    'scipy=1.18.0',
    'cyipopt=1.7.0',
    'ipopt=3.14.19',
    'mumps-seq=5.8.2',
    'pandas', 'pyarrow', 'pyyaml', 'pyproj', 'pytest',
    'conda-pack'
)
$CreationCommand = 'conda ' + ($CreationArguments -join ' ')
Invoke-NativeCaptured -FilePath $CondaExe -Arguments $CreationArguments -StdoutPath (Join-Path $StageDirectory 'environment_creation_stdout.txt') -StderrPath (Join-Path $StageDirectory 'environment_creation_stderr.txt') | Out-Null

$environmentList = Invoke-NativeCaptured -FilePath $CondaExe -Arguments @('env', 'list', '--json') -StdoutPath (Join-Path $StageDirectory 'conda_env_list.json') -StderrPath (Join-Path $StageDirectory 'conda_env_list_stderr.txt')
$environmentDocument = $environmentList.Stdout | ConvertFrom-Json
$matchingPrefixes = @($environmentDocument.envs | Where-Object { (Split-Path -Leaf $_) -eq $EnvironmentName })
if ($matchingPrefixes.Count -ne 1) {
    throw "Expected exactly one $EnvironmentName prefix, found $($matchingPrefixes.Count)."
}
$EnvironmentPrefix = [System.IO.Path]::GetFullPath([string]$matchingPrefixes[0])
$EnvironmentPython = Join-Path $EnvironmentPrefix 'python.exe'
if (-not (Test-Path -LiteralPath $EnvironmentPython -PathType Leaf)) {
    throw "Environment Python is missing: $EnvironmentPython"
}

$explicitResult = Invoke-NativeCaptured -FilePath $CondaExe -Arguments @('list', '--name', $EnvironmentName, '--explicit') -StdoutPath (Join-Path $StageDirectory 'conda_explicit.txt') -StderrPath (Join-Path $StageDirectory 'conda_explicit_stderr.txt')
$listResult = Invoke-NativeCaptured -FilePath $CondaExe -Arguments @('list', '--name', $EnvironmentName, '--json') -StdoutPath (Join-Path $StageDirectory 'conda_list.json') -StderrPath (Join-Path $StageDirectory 'conda_list_stderr.txt')
$historyResult = Invoke-NativeCaptured -FilePath $CondaExe -Arguments @('env', 'export', '--name', $EnvironmentName, '--from-history') -StdoutPath (Join-Path $StageDirectory 'environment_history.yml') -StderrPath (Join-Path $StageDirectory 'environment_history_stderr.txt')
if (($listResult.Stdout | ConvertFrom-Json).Count -lt 11) {
    throw 'Resolved conda package inventory is unexpectedly incomplete.'
}

Copy-Item -LiteralPath (Join-Path $BuilderRoot 'smoke_test.py') -Destination (Join-Path $StageDirectory 'smoke_test.py')
Copy-Item -LiteralPath (Join-Path $BuilderRoot 'inspect_environment.py') -Destination (Join-Path $StageDirectory 'inspect_environment.py')
Copy-Item -LiteralPath (Join-Path $BuilderRoot 'verify_runtime.py') -Destination (Join-Path $StageDirectory 'verify_runtime.py')
Copy-Item -LiteralPath (Join-Path $BuilderRoot 'build_bundle.ps1') -Destination (Join-Path $StageDirectory 'build_bundle.ps1')
Copy-Item -LiteralPath (Join-Path $BuilderRoot 'verify_bundle.ps1') -Destination (Join-Path $StageDirectory 'verify_bundle.ps1')
Copy-Item -LiteralPath (Join-Path $BuilderRoot 'README_TARGET_RECOVERY.txt') -Destination (Join-Path $StageDirectory 'README_TARGET_RECOVERY.txt')
Copy-Item -LiteralPath (Join-Path $RepositoryRoot '.github\workflows\build-ipopt-bootstrap-bundle.yml') -Destination (Join-Path $StageDirectory 'build-ipopt-bootstrap-bundle.yml')

$RuntimeResultPath = Join-Path $StageDirectory 'runtime_dependency_result.json'
Invoke-NativeCaptured -FilePath $CondaExe -Arguments @(
    'run', '--name', $EnvironmentName, 'python',
    (Join-Path $StageDirectory 'verify_runtime.py'),
    '--conda-list', (Join-Path $StageDirectory 'conda_list.json'),
    '--result', $RuntimeResultPath
) -StdoutPath (Join-Path $StageDirectory 'runtime_dependency_stdout.txt') -StderrPath (Join-Path $StageDirectory 'runtime_dependency_stderr.txt') | Out-Null
$runtimeResult = Get-Content -LiteralPath $RuntimeResultPath -Raw | ConvertFrom-Json
if ($runtimeResult.classification -ne 'MH370_BENCHMARK_RUNTIME_DEPENDENCY_PASS') {
    throw "MH370 benchmark runtime dependency gate failed: $($runtimeResult.classification)"
}
if ([string]$runtimeResult.dependency_audit_manifest_sha256 -ne $DependencyAuditManifestSha256) {
    throw 'Runtime dependency gate did not preserve the audited dependency-manifest SHA-256.'
}

$SmokeResultPath = Join-Path $StageDirectory 'smoke_test_result.json'
Invoke-NativeCaptured -FilePath $CondaExe -Arguments @(
    'run', '--name', $EnvironmentName, 'python',
    (Join-Path $StageDirectory 'smoke_test.py'), '--result', $SmokeResultPath
) -StdoutPath (Join-Path $StageDirectory 'smoke_test_stdout.txt') -StderrPath (Join-Path $StageDirectory 'smoke_test_stderr.txt') | Out-Null
$smokeResult = Get-Content -LiteralPath $SmokeResultPath -Raw | ConvertFrom-Json
if ($smokeResult.classification -ne 'IPOPT_EXTERNAL_SMOKE_TEST_PASS') {
    throw "Initial Ipopt smoke test failed: $($smokeResult.classification)"
}

Invoke-NativeCaptured -FilePath $CondaExe -Arguments @(
    'run', '--name', $EnvironmentName, 'python',
    (Join-Path $StageDirectory 'inspect_environment.py'),
    '--conda-list', (Join-Path $StageDirectory 'conda_list.json'),
    '--smoke-result', $SmokeResultPath,
    '--smoke-stdout', (Join-Path $StageDirectory 'smoke_test_stdout.txt'),
    '--build-environment', (Join-Path $StageDirectory 'build_environment.json'),
    '--capabilities', (Join-Path $StageDirectory 'interface_capabilities.json'),
    '--native-manifest', (Join-Path $StageDirectory 'native_library_manifest.json'),
    '--manager-version', $ManagerVersion,
    '--creation-command', $CreationCommand,
    '--channels', 'conda-forge'
) -StdoutPath (Join-Path $StageDirectory 'environment_inspection_stdout.txt') -StderrPath (Join-Path $StageDirectory 'environment_inspection_stderr.txt') | Out-Null

$buildEnvironmentPath = Join-Path $StageDirectory 'build_environment.json'
$buildEnvironment = Get-Content -LiteralPath $buildEnvironmentPath -Raw | ConvertFrom-Json
$buildEnvironment | Add-Member -NotePropertyName benchmark_runtime_dependency_manifest_sha256 -NotePropertyValue $DependencyAuditManifestSha256 -Force
$buildEnvironment | Add-Member -NotePropertyName benchmark_runtime_dependency_gate -NotePropertyValue ([string]$runtimeResult.classification) -Force
$buildEnvironment | Add-Member -NotePropertyName benchmark_runtime_import_versions -NotePropertyValue $runtimeResult.runtime_import_versions -Force
Write-Utf8NoBom -Path $buildEnvironmentPath -Value (($buildEnvironment | ConvertTo-Json -Depth 20) + "`n")

$EnvironmentArchive = Join-Path $StageDirectory $ArchiveName
Invoke-NativeCaptured -FilePath $CondaExe -Arguments @('run', '--name', $EnvironmentName, 'conda-pack', '--name', $EnvironmentName, '--output', $EnvironmentArchive, '--format', 'tar.gz', '--force') -StdoutPath (Join-Path $StageDirectory 'conda_pack_stdout.txt') -StderrPath (Join-Path $StageDirectory 'conda_pack_stderr.txt') | Out-Null
if (-not (Test-Path -LiteralPath $EnvironmentArchive -PathType Leaf) -or (Get-Item -LiteralPath $EnvironmentArchive).Length -le 0) {
    throw 'conda-pack did not create a nonempty environment archive.'
}

$buildEnvironment = Get-Content -LiteralPath $buildEnvironmentPath -Raw | ConvertFrom-Json
$buildEnvironment | Add-Member -NotePropertyName environment_archive_filename -NotePropertyValue $ArchiveName -Force
$buildEnvironment | Add-Member -NotePropertyName environment_archive_size_bytes -NotePropertyValue (Get-Item -LiteralPath $EnvironmentArchive).Length -Force
$buildEnvironment | Add-Member -NotePropertyName environment_archive_sha256 -NotePropertyValue (Get-Sha256Lower -Path $EnvironmentArchive) -Force
Write-Utf8NoBom -Path $buildEnvironmentPath -Value (($buildEnvironment | ConvertTo-Json -Depth 20) + "`n")

New-Item -ItemType Directory -Path $RelocatedDirectory | Out-Null
$TarExe = (Get-Command tar.exe -ErrorAction Stop).Source
Invoke-NativeCaptured -FilePath $TarExe -Arguments @('-xzf', $EnvironmentArchive, '-C', $RelocatedDirectory) -StdoutPath (Join-Path $StageDirectory 'relocation_extract_stdout.txt') -StderrPath (Join-Path $StageDirectory 'relocation_extract_stderr.txt') | Out-Null
$RelocatedPython = Join-Path $RelocatedDirectory 'python.exe'
$CondaUnpack = Join-Path $RelocatedDirectory 'Scripts\conda-unpack.exe'
if (-not (Test-Path -LiteralPath $RelocatedPython -PathType Leaf)) {
    throw 'Relocated Python executable is missing.'
}
if (-not (Test-Path -LiteralPath $CondaUnpack -PathType Leaf)) {
    throw 'Relocated conda-unpack executable is missing.'
}

$RelocationCommandPath = Join-Path $WorkRoot 'run_relocation_rehearsal.cmd'
$RelocationRuntimeResultPath = Join-Path $StageDirectory 'relocation_runtime_dependency_result.json'
$RelocationResultPath = Join-Path $StageDirectory 'relocation_smoke_test_result.json'
$relocationCommand = @"
@echo off
setlocal
call "$RelocatedDirectory\Scripts\activate.bat"
if errorlevel 1 exit /b %errorlevel%
"$CondaUnpack"
if errorlevel 1 exit /b %errorlevel%
"$RelocatedPython" "$StageDirectory\verify_runtime.py" --conda-list "$StageDirectory\conda_list.json" --result "$RelocationRuntimeResultPath"
if errorlevel 1 exit /b %errorlevel%
"$RelocatedPython" "$StageDirectory\smoke_test.py" --result "$RelocationResultPath"
exit /b %errorlevel%
"@
Write-Utf8NoBom -Path $RelocationCommandPath -Value $relocationCommand
$CmdExe = $env:COMSPEC
if (-not $CmdExe) { $CmdExe = (Get-Command cmd.exe -ErrorAction Stop).Source }
Invoke-NativeCaptured -FilePath $CmdExe -Arguments @('/d', '/s', '/c', $RelocationCommandPath) -StdoutPath (Join-Path $StageDirectory 'relocation_smoke_test_stdout.txt') -StderrPath (Join-Path $StageDirectory 'relocation_smoke_test_stderr.txt') | Out-Null

$relocationRuntimeResult = Get-Content -LiteralPath $RelocationRuntimeResultPath -Raw | ConvertFrom-Json
if ($relocationRuntimeResult.classification -ne 'MH370_BENCHMARK_RUNTIME_DEPENDENCY_PASS') {
    throw "Relocated MH370 benchmark runtime dependency gate failed: $($relocationRuntimeResult.classification)"
}
$relocationRuntimePrefix = [System.IO.Path]::GetFullPath([string]$relocationRuntimeResult.environment_prefix)
if (-not $relocationRuntimePrefix.StartsWith($RelocatedDirectory, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Relocated runtime gate used the wrong environment prefix: $relocationRuntimePrefix"
}
$relocationRuntimeResult | Add-Member -NotePropertyName relocation_classification -NotePropertyValue 'MH370_BENCHMARK_RUNTIME_RELOCATION_PASS' -Force
$relocationRuntimeResult | Add-Member -NotePropertyName original_environment_prefix_not_used -NotePropertyValue $true -Force
Write-Utf8NoBom -Path $RelocationRuntimeResultPath -Value (($relocationRuntimeResult | ConvertTo-Json -Depth 30) + "`n")

$relocationResult = Get-Content -LiteralPath $RelocationResultPath -Raw | ConvertFrom-Json
if ($relocationResult.classification -ne 'IPOPT_EXTERNAL_SMOKE_TEST_PASS') {
    throw "Relocated smoke test failed: $($relocationResult.classification)"
}
$relocatedExecutable = [System.IO.Path]::GetFullPath([string]$relocationResult.python_executable)
if (-not $relocatedExecutable.StartsWith($RelocatedDirectory, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Relocated smoke test used the wrong Python prefix: $relocatedExecutable"
}
$relocationResult | Add-Member -NotePropertyName relocation_classification -NotePropertyValue 'IPOPT_EXTERNAL_BUNDLE_RELOCATION_PASS' -Force
$relocationResult | Add-Member -NotePropertyName original_environment_prefix_not_used -NotePropertyValue $true -Force
Write-Utf8NoBom -Path $RelocationResultPath -Value (($relocationResult | ConvertTo-Json -Depth 30) + "`n")

$hashRecords = @()
foreach ($file in (Get-ChildItem -LiteralPath $StageDirectory -File | Where-Object { $_.Name -ne 'sha256_manifest.json' } | Sort-Object Name)) {
    $hashRecords += [ordered]@{
        filename = $file.Name
        size_bytes = $file.Length
        sha256 = Get-Sha256Lower -Path $file.FullName
    }
}
$hashManifest = [ordered]@{
    schema_version = '1'
    classification = 'IPOPT_EXTERNAL_BUNDLE_HASH_MANIFEST_COMPLETE'
    self_excluded_to_avoid_recursive_hash = $true
    records = $hashRecords
}
Write-Utf8NoBom -Path (Join-Path $StageDirectory 'sha256_manifest.json') -Value (($hashManifest | ConvertTo-Json -Depth 10) + "`n")

& (Join-Path $BuilderRoot 'verify_bundle.ps1') -BundleDirectory $StageDirectory | Out-Host
$FinalZip = Join-Path $OutputDirectory $FinalZipName
Compress-Archive -LiteralPath $StageDirectory -DestinationPath $FinalZip -CompressionLevel Optimal
& (Join-Path $BuilderRoot 'verify_bundle.ps1') -BundleDirectory $StageDirectory -ZipPath $FinalZip | Out-Host

$zipSize = (Get-Item -LiteralPath $FinalZip).Length
$zipHash = Get-Sha256Lower -Path $FinalZip
$hashTextPath = Join-Path $OutputDirectory ($FinalZipName + '.sha256.txt')
Write-Utf8NoBom -Path $hashTextPath -Value "$zipHash  $FinalZipName`n"

Write-Output "IPOPT_EXTERNAL_BUNDLE_BUILD_PASS"
Write-Output "ZIP_PATH=$FinalZip"
Write-Output "ZIP_SIZE_BYTES=$zipSize"
Write-Output "ZIP_SHA256=$zipHash"
