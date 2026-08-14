Set-StrictMode -Version Latest

function Get-R3ScientificPayloadRecord {
    param([Parameter(Mandatory = $true)][string]$ScientificPayload)

    if (-not (Test-Path -LiteralPath $ScientificPayload -PathType Leaf)) {
        throw "STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_FILE_FAILURE: payload is missing or is not a file: $ScientificPayload"
    }
    $item = Get-Item -LiteralPath $ScientificPayload
    if ($item.Length -le 0) {
        throw "STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_FILE_FAILURE: payload is empty: $ScientificPayload"
    }
    return [ordered]@{
        path = $item.FullName
        size_bytes = $item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function New-R3ScientificPayloadExtractionRoot {
    param([Parameter(Mandatory = $true)][string]$ScientificRoot)

    if (Test-Path -LiteralPath $ScientificRoot) {
        throw "Refusing to reuse scientific payload extraction directory: $ScientificRoot"
    }
    New-Item -ItemType Directory -Path $ScientificRoot | Out-Null
    if (-not (Test-Path -LiteralPath $ScientificRoot -PathType Container)) {
        throw "STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_EXTRACTION_TARGET_FAILURE: extraction directory was not created: $ScientificRoot"
    }
}

function Assert-R3ScientificPayloadStructure {
    param([Parameter(Mandatory = $true)][string]$ScientificRoot)

    $referencePath = Join-Path $ScientificRoot 'tools\ipopt_r3_builder\scientific_reference_states.json'
    $protectedManifestPath = Join-Path $ScientificRoot 'provenance\phase4c_stage1b6f_bootstrap_input_hashes.json'
    $missing = @(
        @($referencePath, $protectedManifestPath) |
            Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }
    )
    if ($missing.Count -ne 0) {
        throw "STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_STRUCTURE_FAILURE: missing required extracted file(s): $($missing -join ', ')"
    }
    return [ordered]@{
        classification = 'STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_STRUCTURE_PASS'
        scientific_root = (Resolve-Path -LiteralPath $ScientificRoot).Path
        scientific_reference_states = (Resolve-Path -LiteralPath $referencePath).Path
        protected_bootstrap_manifest = (Resolve-Path -LiteralPath $protectedManifestPath).Path
    }
}
