Set-StrictMode -Version Latest

function Test-R3PathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return $false }
    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    return $resolvedPath.Equals($resolvedRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $resolvedPath.StartsWith($resolvedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Test-R3NativeExecutable {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    return [IO.Path]::GetExtension($Path) -in @('.exe', '.com')
}

function Select-R3CondaExecutable {
    param(
        [AllowEmptyString()][string]$CondaExeEnvironment,
        [AllowEmptyString()][string]$CondaCommandType,
        [AllowEmptyString()][string]$CondaCommandSource,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot
    )

    if (-not (Test-Path -LiteralPath $ExpectedRoot -PathType Container)) {
        throw "STAGE1B6J_R3_BUILDER_CONDA_PROVENANCE_FAILURE: setup-miniconda root is missing: $ExpectedRoot"
    }

    $candidate = $null
    $resolutionSource = $null
    if ($CondaExeEnvironment -and (Test-R3NativeExecutable -Path $CondaExeEnvironment)) {
        $candidate = (Resolve-Path -LiteralPath $CondaExeEnvironment).Path
        $resolutionSource = 'CONDA_EXE'
    }
    elseif ($CondaCommandType -eq 'Application' -and $CondaCommandSource -and
            (Test-R3NativeExecutable -Path $CondaCommandSource)) {
        $candidate = (Resolve-Path -LiteralPath $CondaCommandSource).Path
        $resolutionSource = 'Get-Command conda'
    }
    else {
        throw 'STAGE1B6J_R3_BUILDER_CONDA_COMMAND_RESOLUTION_FAILURE: unable to resolve a concrete Conda executable from CONDA_EXE or Get-Command conda.'
    }

    if (-not (Test-R3PathUnderRoot -Path $candidate -Root $ExpectedRoot)) {
        throw "STAGE1B6J_R3_BUILDER_CONDA_PROVENANCE_FAILURE: resolved Conda is outside setup-miniconda root: $candidate"
    }
    return [pscustomobject]@{
        Path = $candidate
        ResolutionSource = $resolutionSource
        ExpectedRoot = (Resolve-Path -LiteralPath $ExpectedRoot).Path
    }
}

function Resolve-R3ApplicationCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowEmptyString()][string]$ExplicitPath,
        [AllowEmptyString()][string]$ExpectedRoot,
        [switch]$RequireExpectedRoot
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    $candidate = $null
    $resolutionSource = $null
    if ($ExplicitPath -and (Test-R3NativeExecutable -Path $ExplicitPath)) {
        $candidate = (Resolve-Path -LiteralPath $ExplicitPath).Path
        $resolutionSource = 'explicit_path'
    }
    elseif ($command -and $command.CommandType -eq 'Application' -and $command.Source -and
            (Test-R3NativeExecutable -Path $command.Source)) {
        $candidate = (Resolve-Path -LiteralPath $command.Source).Path
        $resolutionSource = 'Get-Command'
    }
    else {
        throw "STAGE1B6J_R3_BUILDER_TOOL_RESOLUTION_FAILURE: unable to resolve application $Name"
    }

    if ($RequireExpectedRoot -and (-not $ExpectedRoot -or -not (Test-R3PathUnderRoot -Path $candidate -Root $ExpectedRoot))) {
        throw "STAGE1B6J_R3_BUILDER_TOOL_PROVENANCE_FAILURE: $Name is outside the activated builder environment: $candidate"
    }
    return [pscustomobject]@{
        Name = $Name
        Path = $candidate
        ResolutionSource = $resolutionSource
        CommandType = if ($command) { [string]$command.CommandType } else { $null }
        CommandSource = if ($command) { [string]$command.Source } else { $null }
        ExpectedRoot = if ($ExpectedRoot) { $ExpectedRoot } else { $null }
    }
}

function Get-R3CommandDiagnostic {
    param([Parameter(Mandatory = $true)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        return [ordered]@{ name = $Name; found = $false; command_type = $null; source = $null; path = $null }
    }
    return [ordered]@{
        name = $Name
        found = $true
        command_type = [string]$command.CommandType
        source = if ($command.Source) { [string]$command.Source } else { $null }
        path = if ($command.PSObject.Properties['Path'] -and $command.Path) { [string]$command.Path } else { $null }
    }
}
