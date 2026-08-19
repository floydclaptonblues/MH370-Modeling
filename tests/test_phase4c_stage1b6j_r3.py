from __future__ import annotations

import hashlib
import inspect
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "ipopt_r3_builder"
COMMAND_RESOLVER = TOOLS / "resolve_builder_commands.ps1"
PAYLOAD_GUARD = TOOLS / "scientific_payload_guard.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
WINDOWS_TAR = shutil.which("tar")
sys.path.insert(0, str(TOOLS))

from r3_audit import (  # noqa: E402
    EXPECTED,
    _fallback_version_satisfies,
    _version_satisfies,
    audit_dll_resolution,
    audit_ownership,
    audit_plan,
    canonical_build,
    classify_dry_run,
    compare_plan_receipts,
    dependency_satisfied,
    plan_rejection_summary,
    plan_records,
    protected_check,
    receipt_inventory_records,
    sha256_file,
    version_matcher_info,
    verify_hash_manifest,
)


def package(name: str, version: str, build: str = "h000_0", depends: list[str] | None = None) -> dict:
    return {
        "name": name,
        "version": version,
        "build": build,
        "build_number": 0,
        "channel": "conda-forge",
        "subdir": "win-64",
        "url": f"https://conda.anaconda.org/conda-forge/win-64/{name}-{version}-{build}.conda",
        "depends": depends or [],
    }


def valid_payload() -> dict:
    rows = [package(name, version, "py312h000_0" if name not in {"python", "ipopt", "mumps-seq"} else "h000_0") for name, version in EXPECTED.items()]
    rows.extend(
        [
            package("libblas", "3.11.0", "0_hopenblas", ["libopenblas >=0.3.30,<1.0a0"]),
            package("libcblas", "3.11.0", "0_hopenblas", ["libblas 3.11.0 0_hopenblas"]),
            package("liblapack", "3.11.0", "0_hopenblas", ["libblas >=3.9.0,<4.0a0"]),
            package("libopenblas", "0.3.30", "pthreads_h000_0"),
            package("llvm-openmp", "20.1.8", "h000_0"),
        ]
    )
    return {"success": True, "actions": {"LINK": rows, "FETCH": rows}}


R3E_CORE_BUILDS = {
    "python": "hb12b558_1_cpython",
    "numpy": "py312ha3f287d_0",
    "scipy": "py312h9b3c559_0",
    "cyipopt": "py312h478e429_0",
    "ipopt": "he5a0f77_2",
    "mumps-seq": "h607cc0b_2",
    "pandas": "py312h95189c4_1",
    "pyarrow": "py312h2e8e312_0",
    "pyproj": "py312h589cc8f_5",
    "pyyaml": "py312h05f76fc_1",
    "pytest": "pyhc364b38_2",
}


def r3e_schema_payload() -> dict:
    versions_and_builds = {
        **{name: (version, R3E_CORE_BUILDS[name]) for name, version in EXPECTED.items()},
        "libblas": ("3.11.0", "9_h0adab6e_openblas"),
        "libcblas": ("3.11.0", "9_h2a8eebe_openblas"),
        "liblapack": ("3.11.0", "9_hd232482_openblas"),
        "libopenblas": ("0.3.34", "openmp_hdb726d1_0"),
        "llvm-openmp": ("22.1.8", "h4fa8253_0"),
    }
    dependencies = {
        "libblas": ["libopenblas >=0.3.34,<1.0a0"],
        "libcblas": ["libblas 3.11.0 9_h0adab6e_openblas"],
        "liblapack": ["libblas >=3.11.0,<4.0a0"],
        "libopenblas": ["llvm-openmp >=22.1.8"],
    }
    link = []
    fetch = []
    for name, (version, build) in versions_and_builds.items():
        dist_name = f"{name}-{version}-{build}"
        link.append(
            {
                "name": name,
                "version": version,
                "build_string": build,
                "dist_name": dist_name,
                "channel": "conda-forge",
            }
        )
        fetch.append(package(name, version, build, dependencies.get(name, [])))
    return {"success": True, "actions": {"LINK": link, "FETCH": fetch}}


def test_01_dry_run_json_parsing() -> None:
    rows = plan_records(valid_payload())
    assert len(rows) == len(EXPECTED) + 5
    assert rows == sorted(rows, key=lambda row: (row["name"], row["version"], row["build"]))


def test_02_transport_failure_classification() -> None:
    assert classify_dry_run(1, None, "CondaHTTPError: connection timed out fetching repodata") == "STAGE1B6J_R3_GITHUB_METADATA_TRANSPORT_FAILURE"


def test_03_unsatisfiable_plan_classification() -> None:
    assert classify_dry_run(1, {"success": False, "error": "PackagesNotFoundError: impossible"}, "") == "STAGE1B6J_OPENBLAS_PLAN_UNSATISFIABLE"


def test_04_exact_core_version_gate() -> None:
    assert audit_plan(valid_payload())["passed"]
    broken = valid_payload()
    next(row for row in broken["actions"]["LINK"] if row["name"] == "numpy")["version"] = "2.5.1"
    assert not audit_plan(broken)["core_versions"]["numpy"]["passed"]


def test_05_prohibited_mkl_detection() -> None:
    payload = valid_payload()
    row = package("mkl", "2026.1.0")
    payload["actions"]["LINK"].append(row)
    payload["actions"]["FETCH"].append(row)
    assert audit_plan(payload)["prohibited_packages"] == ["mkl"]


def test_06_scipy_requires_genuine_conda_receipt() -> None:
    payload = valid_payload()
    scipy = next(row for row in payload["actions"]["LINK"] if row["name"] == "scipy")
    scipy["build"] = "pypi_0"
    scipy["channel"] = "pypi"
    scipy["url"] = ""
    assert not audit_plan(payload)["scipy_genuine_conda_forge_record"]


def test_07_dependency_constraints_checked() -> None:
    packages = {row["name"]: row for row in plan_records(valid_payload())}
    assert dependency_satisfied("libopenblas >=0.3.29,<1.0a0", packages)[0]
    assert not dependency_satisfied("libopenblas >=9.0", packages)[0]
    assert not dependency_satisfied("missing-runtime >=1", packages)[0]


def test_08_openmp_ownership_conflict(tmp_path: Path) -> None:
    receipts = [
        {"name": "llvm-openmp", "version": "1", "build": "a", "files": ["Library/bin/libiomp5md.dll"]},
        {"name": "intel-openmp", "version": "1", "build": "b", "files": ["Library/bin/libiomp5md.dll"]},
    ]
    audit = audit_ownership(tmp_path, receipts)
    assert not audit["passed"]
    assert len(audit["libiomp5md_owners"]) == 2


def test_09_plan_vs_receipts_exactness() -> None:
    plan = audit_plan(valid_payload())
    receipts = [{"name": row["name"], "version": row["version"], "build": row["build"]} for row in plan["packages"]]
    assert compare_plan_receipts(plan, receipts)["passed"]
    assert not compare_plan_receipts(plan, receipts[:-1])["passed"]


def test_10_dll_resolution_must_stay_inside_prefix(tmp_path: Path) -> None:
    inside = tmp_path / "Library" / "bin" / "libopenblas.dll"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"dll")
    assert audit_dll_resolution({"numerical_modules": [str(inside)]}, tmp_path)["passed"]
    assert not audit_dll_resolution({"numerical_modules": [r"C:\other\mkl_rt.dll"]}, tmp_path)["passed"]


def test_11_artifact_manifest_hashing(tmp_path: Path) -> None:
    item = tmp_path / "artifact.bin"
    item.write_bytes(b"r3")
    manifest = {"records": [{"path": "artifact.bin", "size_bytes": 2, "sha256": hashlib.sha256(b"r3").hexdigest()}]}
    assert verify_hash_manifest(tmp_path, manifest)["passed"]
    assert sha256_file(item) == manifest["records"][0]["sha256"]


def test_12_scientific_tolerances_are_frozen_and_complete() -> None:
    path = ROOT / "provenance" / "phase4c_stage1b6f_bootstrap_tolerances.json"
    before = path.read_bytes()
    document = json.loads(before)
    assert document["post_result_relaxation_permitted"] is False
    assert {"objective", "residual_vector_max", "gradient_max", "constraints_max", "constraint_jacobian_max", "physical_state_max", "endpoint_displacement_m", "canonical_kkt"} <= set(document["absolute_tolerances"])
    assert path.read_bytes() == before


def test_13_reference_evaluator_forbids_optimization() -> None:
    text = (TOOLS / "r3_scientific_evaluate.py").read_text(encoding="utf-8")
    assert '"optimization_executed": False' in text
    assert ".solve(" not in text
    assert "minimize(" not in text


def test_14_protected_529_integrity() -> None:
    result = protected_check(ROOT, ROOT / "provenance" / "phase4c_stage1b6f_bootstrap_input_hashes.json")
    assert result == {"passed": True, "expected": 529, "matched": 529, "failures": []}


def invoke_conda_selector(
    expected_root: Path,
    conda_exe: Path | None,
    command_type: str = "",
    command_source: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = (
        f". {literal(str(COMMAND_RESOLVER))}; "
        "$result = Select-R3CondaExecutable "
        f"-CondaExeEnvironment {literal(str(conda_exe) if conda_exe else '')} "
        f"-CondaCommandType {literal(command_type)} "
        f"-CondaCommandSource {literal(str(command_source) if command_source else '')} "
        f"-ExpectedRoot {literal(str(expected_root))}; "
        "$result | ConvertTo-Json -Compress"
    )
    return subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        text=True,
        capture_output=True,
        check=False,
    )


def invoke_payload_guard(script_body: str) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    guard = "'" + str(PAYLOAD_GUARD).replace("'", "''") + "'"
    return subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f". {guard}; {script_body}"],
        text=True,
        capture_output=True,
        check=False,
    )


def test_15_valid_conda_exe_does_not_require_literal_conda_exe_lookup(tmp_path: Path) -> None:
    root = tmp_path / "setup-miniconda"
    conda = root / "Scripts" / "conda.exe"
    conda.parent.mkdir(parents=True)
    conda.write_bytes(b"test")
    result = invoke_conda_selector(root, conda)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ResolutionSource"] == "CONDA_EXE"


def test_16_valid_conda_exe_is_preferred(tmp_path: Path) -> None:
    root = tmp_path / "setup-miniconda"
    preferred = root / "Scripts" / "conda.exe"
    fallback = root / "condabin" / "conda.exe"
    preferred.parent.mkdir(parents=True)
    fallback.parent.mkdir(parents=True)
    preferred.write_bytes(b"preferred")
    fallback.write_bytes(b"fallback")
    result = invoke_conda_selector(root, preferred, "Application", fallback)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert Path(payload["Path"]).resolve() == preferred.resolve()
    assert payload["ResolutionSource"] == "CONDA_EXE"


def test_17_unrelated_conda_executable_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "setup-miniconda"
    root.mkdir()
    unrelated = tmp_path / "old-environment" / "conda.exe"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"unrelated")
    result = invoke_conda_selector(root, unrelated)
    assert result.returncode != 0
    assert "STAGE1B6J_R3_BUILDER_CONDA_PROVENANCE_FAILURE" in result.stderr


def test_18_missing_conda_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "setup-miniconda"
    root.mkdir()
    result = invoke_conda_selector(root, None)
    assert result.returncode != 0
    assert "STAGE1B6J_R3_BUILDER_CONDA_COMMAND_RESOLUTION_FAILURE" in result.stderr


def test_19_r3_package_specifications_are_unchanged() -> None:
    text = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    match = re.search(r"\$Specs = @\(.*?\n\)", text, flags=re.DOTALL)
    assert match is not None
    normalized = match.group(0).replace("\r\n", "\n").encode()
    assert hashlib.sha256(normalized).hexdigest() == "dbe2f12bbe5e33a26c638d80ae44577fe5e95c941e096af108f3baae7c8aff81"
    assert "Get-Command conda.exe -ErrorAction Stop" not in text


def test_20_scientific_root_is_created_before_extraction(tmp_path: Path) -> None:
    payload = tmp_path / "payload.zip"
    payload.write_bytes(b"nonempty")
    scientific_root = tmp_path / "work" / "scientific_payload"
    result = invoke_payload_guard(
        f"Get-R3ScientificPayloadRecord -ScientificPayload '{payload}' | Out-Null; "
        f"New-R3ScientificPayloadExtractionRoot -ScientificRoot '{scientific_root}'; "
        f"Test-Path -LiteralPath '{scientific_root}' -PathType Container"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    assert builder.index("New-R3ScientificPayloadExtractionRoot") < builder.index("Invoke-NativeCaptured $TarExe @('-xf'")


def test_21_preexisting_scientific_root_fails_closed(tmp_path: Path) -> None:
    scientific_root = tmp_path / "scientific_payload"
    scientific_root.mkdir()
    marker = scientific_root / "existing.txt"
    marker.write_text("preserve", encoding="utf-8")
    result = invoke_payload_guard(f"New-R3ScientificPayloadExtractionRoot -ScientificRoot '{scientific_root}'")
    assert result.returncode != 0
    assert "Refusing to reuse scientific payload extraction directory" in result.stderr
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_22_missing_scientific_payload_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.zip"
    result = invoke_payload_guard(f"Get-R3ScientificPayloadRecord -ScientificPayload '{missing}'")
    assert result.returncode != 0
    assert "STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_FILE_FAILURE" in result.stderr


def test_23_zero_byte_scientific_payload_fails_closed(tmp_path: Path) -> None:
    payload = tmp_path / "empty.zip"
    payload.touch()
    result = invoke_payload_guard(f"Get-R3ScientificPayloadRecord -ScientificPayload '{payload}'")
    assert result.returncode != 0
    assert "STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_FILE_FAILURE" in result.stderr


def test_24_expected_payload_structure_is_required_after_extraction(tmp_path: Path) -> None:
    complete = tmp_path / "complete"
    reference = complete / "tools" / "ipopt_r3_builder" / "scientific_reference_states.json"
    manifest = complete / "provenance" / "phase4c_stage1b6f_bootstrap_input_hashes.json"
    reference.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    reference.write_text("{}", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    passed = invoke_payload_guard(
        f"Assert-R3ScientificPayloadStructure -ScientificRoot '{complete}' | ConvertTo-Json -Compress"
    )
    assert passed.returncode == 0, passed.stderr
    assert json.loads(passed.stdout)["classification"] == "STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_STRUCTURE_PASS"

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    failed = invoke_payload_guard(f"Assert-R3ScientificPayloadStructure -ScientificRoot '{incomplete}'")
    assert failed.returncode != 0
    assert "STAGE1B6J_R3_SCIENTIFIC_PAYLOAD_STRUCTURE_FAILURE" in failed.stderr
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    assert builder.index("Invoke-NativeCaptured $TarExe @('-xf'") < builder.index("Assert-R3ScientificPayloadStructure")


def test_25_r3_powershell_files_parse_cleanly() -> None:
    assert POWERSHELL is not None
    for path in (TOOLS / "build_r3_openblas.ps1", COMMAND_RESOLVER, PAYLOAD_GUARD):
        quoted = "'" + str(path).replace("'", "''") + "'"
        script = (
            "$tokens=$null; $errors=$null; "
            f"[Management.Automation.Language.Parser]::ParseFile({quoted},[ref]$tokens,[ref]$errors) | Out-Null; "
            "if ($errors.Count -ne 0) { $errors | ForEach-Object Message; exit 1 }"
        )
        result = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{path}: {result.stdout}{result.stderr}"


def test_26_windows_tar_extracts_existing_payload_when_target_exists(tmp_path: Path) -> None:
    assert WINDOWS_TAR is not None
    payload = TOOLS / "phase4c_stage1b6j_r3_scientific_payload.zip"
    extraction_root = tmp_path / "scientific_payload"
    extraction_root.mkdir()
    result = subprocess.run(
        [WINDOWS_TAR, "-xf", str(payload), "-C", str(extraction_root)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (extraction_root / "tools" / "ipopt_r3_builder" / "scientific_reference_states.json").is_file()
    assert (extraction_root / "provenance" / "phase4c_stage1b6f_bootstrap_input_hashes.json").is_file()


def test_27_r3e_r3f_plan_acceptance_policy_remains_strict() -> None:
    source = inspect.getsource(audit_plan)
    for required_gate in (
        'str(packages[name].get("version")) == version',
        "not prohibited",
        'all(item["openblas_variant"] for item in blas.values())',
        '"libopenblas" in packages or "libopenblas-ilp64" in packages',
        "scipy_conda",
        "all_conda_forge",
        "dependency_metadata_complete",
        "not unsatisfied",
    ):
        assert required_gate in source


def test_28_r3e_summary_identifies_every_failure_gate() -> None:
    cases: list[tuple[str, dict]] = []

    core = valid_payload()
    next(row for row in core["actions"]["LINK"] if row["name"] == "numpy")["version"] = "2.5.1"
    cases.append(("core_version_mismatch", core))

    prohibited = valid_payload()
    prohibited["actions"]["LINK"].append(package("mkl", "2026.1.0"))
    cases.append(("prohibited_package", prohibited))

    blas = valid_payload()
    next(row for row in blas["actions"]["LINK"] if row["name"] == "libblas")["build"] = "0_mkl"
    cases.append(("blas_variant", blas))

    missing_openblas = valid_payload()
    rows = [row for row in missing_openblas["actions"]["LINK"] if row["name"] != "libopenblas"]
    missing_openblas["actions"]["LINK"] = rows
    missing_openblas["actions"]["FETCH"] = rows
    cases.append(("libopenblas_missing", missing_openblas))

    scipy = valid_payload()
    scipy_row = next(row for row in scipy["actions"]["LINK"] if row["name"] == "scipy")
    scipy_row.update({"build": "pypi_0", "channel": "pypi", "url": ""})
    cases.append(("scipy_provenance", scipy))

    channel = valid_payload()
    pytest_row = next(row for row in channel["actions"]["LINK"] if row["name"] == "pytest")
    pytest_row.update({"channel": "defaults", "url": "https://repo.anaconda.com/pkgs/main/pytest.conda"})
    cases.append(("non_conda_forge", channel))

    dependency = valid_payload()
    next(row for row in dependency["actions"]["LINK"] if row["name"] == "numpy")["depends"].append("missing-runtime >=1")
    cases.append(("dependency_checker", dependency))

    for expected_gate, payload in cases:
        summary = plan_rejection_summary(audit_plan(payload))
        assert not summary["passed"]
        assert summary["classification"] == "STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_AUDIT_REJECTED"
        assert expected_gate in summary["failure_gates"]

    dependency_summary = plan_rejection_summary(audit_plan(dependency))
    assert dependency_summary["dependency_interpretation_classification"] == (
        "STAGE1B6J_R3_PACKAGE_AUDITOR_DEPENDENCY_SEMANTICS_MISMATCH_SUSPECTED"
    )
    assert dependency_summary["unsatisfied_dependencies"][0]["observed_target"] is None


def test_29_r3e_rejected_plan_writes_diagnostics_and_returns_failure(tmp_path: Path) -> None:
    payload = valid_payload()
    next(row for row in payload["actions"]["LINK"] if row["name"] == "numpy")["version"] = "2.5.1"
    dry_run = tmp_path / "dry_run.json"
    plan = tmp_path / "plan.json"
    summary = tmp_path / "summary.json"
    dry_run.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "r3_audit.py"),
            "plan",
            "--dry-run",
            str(dry_run),
            "--output",
            str(plan),
            "--rejection-summary",
            str(summary),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(plan.read_text(encoding="utf-8"))["passed"] is False
    assert json.loads(summary.read_text(encoding="utf-8"))["failure_gates"] == ["core_version_mismatch"]


def test_30_r3e_successful_plan_still_passes(tmp_path: Path) -> None:
    dry_run = tmp_path / "dry_run.json"
    plan = tmp_path / "plan.json"
    summary = tmp_path / "summary.json"
    dry_run.write_text(json.dumps(valid_payload()), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "r3_audit.py"),
            "plan",
            "--dry-run",
            str(dry_run),
            "--output",
            str(plan),
            "--rejection-summary",
            str(summary),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(plan.read_text(encoding="utf-8"))["passed"] is True
    assert not summary.exists()


def test_31_r3e_builder_stops_before_candidate_installation() -> None:
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    plan_start = builder.index("$PackagePlan =")
    install_start = builder.index("$createArgs =")
    rejection_block = builder[plan_start:install_start]
    assert "-AllowFailure" in rejection_block
    assert "STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_AUDIT_REJECTED" in rejection_block
    assert rejection_block.index("if ($planAudit.ExitCode -ne 0)") < rejection_block.index("throw 'STAGE1B6J")
    assert plan_start < install_start


def test_32_r3e_failure_artifact_excludes_runtime_payloads() -> None:
    workflow = (ROOT / ".github/workflows/phase4c-stage1b6j-r3-openblas-runtime.yml").read_text(encoding="utf-8")
    assert "if: ${{ failure() }}" in workflow
    assert "phase4c-stage1b6j-r3-package-plan-failure-diagnostics" in workflow
    for path in (
        "github_dry_run.json",
        "github_package_plan.json",
        "github_package_plan_rejection_summary.json",
        "conda_info.json",
        "github_builder_command_resolution.json",
        "github_scientific_payload_source.json",
        "github_scientific_payload_structure.json",
        "github_protected_preflight.json",
        "logs/github_dry_run.stderr.txt",
        "logs/plan_audit.stdout.txt",
        "logs/plan_audit.stderr.txt",
    ):
        assert f"phase4c_stage1b6j_r3_artifact/{path}" in workflow
    failure_step = workflow.split("- name: Upload package-plan failure diagnostics", 1)[1].split(
        "- name: Upload only the fully validated runtime artifact", 1
    )[0]
    assert "runtime.tar.gz" not in failure_step
    assert "phase4c_stage1b6j_r3_scientific_payload.zip" not in failure_step
    assert "probe_records/github_dll_*.json" in failure_step


def test_33_r3f_link_build_string_fetch_build_merge_retains_rich_metadata() -> None:
    rows = {row["name"]: row for row in plan_records(r3e_schema_payload())}
    expected = {
        "libblas": "9_h0adab6e_openblas",
        "libcblas": "9_h2a8eebe_openblas",
        "liblapack": "9_hd232482_openblas",
    }
    for name, build in expected.items():
        assert rows[name]["build"] == build
        assert rows[name]["build_string"] == build
        assert rows[name]["metadata_source"] == "LINK+FETCH"
        assert rows[name]["dependency_metadata_available"] is True
        assert rows[name]["depends"]


def test_34_r3f_canonical_build_agreement_and_conflict() -> None:
    assert canonical_build({"name": "x", "version": "1", "build": "a", "build_string": "a"}) == "a"
    assert canonical_build({"name": "x", "version": "1", "build_string": "a"}) == "a"
    with pytest.raises(ValueError, match="STAGE1B6J_R3_CONDA_BUILD_FIELD_CONFLICT"):
        canonical_build({"name": "x", "version": "1", "build": "a", "build_string": "b"})

    payload = {
        "actions": {
            "LINK": [{"name": "x", "version": "1", "build_string": "a"}],
            "FETCH": [{"name": "x", "version": "1", "build": "b", "depends": []}],
        }
    }
    with pytest.raises(ValueError, match="STAGE1B6J_R3_CONDA_BUILD_FIELD_CONFLICT"):
        plan_records(payload)


def test_35_r3f_link_only_exact_package_cache_enrichment(tmp_path: Path) -> None:
    cache = tmp_path / "pkgs"
    dist_name = "cached-1.2.3-habc_0"
    metadata = cache / dist_name / "info" / "repodata_record.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "name": "cached",
                "version": "1.2.3",
                "build": "habc_0",
                "depends": ["python >=3.12"],
                "channel": "conda-forge",
                "url": "https://conda.anaconda.org/conda-forge/win-64/cached.conda",
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "actions": {
            "LINK": [
                {
                    "name": "cached",
                    "version": "1.2.3",
                    "build_string": "habc_0",
                    "dist_name": dist_name,
                }
            ],
            "FETCH": [],
        }
    }
    row = plan_records(payload, [cache])[0]
    assert row["metadata_source"] == "LINK+PACKAGE_CACHE"
    assert row["package_cache_metadata_kind"] == "REPODATA_RECORD"
    assert row["dependency_metadata_available"] is True
    assert row["depends"] == ["python >=3.12"]
    assert Path(row["package_cache_metadata_path"]).resolve() == metadata.resolve()


def test_36_r3f_wrong_package_cache_distribution_is_rejected(tmp_path: Path) -> None:
    cache = tmp_path / "pkgs"
    dist_name = "cached-1.2.3-habc_0"
    metadata = cache / dist_name / "info" / "repodata_record.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps({"name": "wrong", "version": "1.2.3", "build": "habc_0", "depends": []}),
        encoding="utf-8",
    )
    payload = {
        "actions": {
            "LINK": [
                {
                    "name": "cached",
                    "version": "1.2.3",
                    "build_string": "habc_0",
                    "dist_name": dist_name,
                }
            ],
            "FETCH": [],
        }
    }
    row = plan_records(payload, [cache])[0]
    assert row["metadata_source"] == "LINK_ONLY"
    assert row["dependency_metadata_available"] is False
    assert row["depends"] is None
    assert row["package_cache_rejections"][0]["reason"] == "EXACT_DISTRIBUTION_MISMATCH"


def test_37_r3f_missing_dependency_metadata_is_not_an_empty_list() -> None:
    payload = {
        "actions": {
            "LINK": [{"name": "unknown", "version": "1", "build_string": "h0", "dist_name": "unknown-1-h0"}],
            "FETCH": [],
        }
    }
    row = plan_records(payload)[0]
    assert row["depends"] is None
    assert row["dependency_metadata_available"] is False
    audit = audit_plan(payload)
    assert audit["classification"] == "STAGE1B6J_R3_PLAN_DEPENDENCY_METADATA_INCOMPLETE"
    assert audit["dependency_metadata_complete"] is False
    assert audit["records_total"] == 1
    assert audit["records_with_dependency_metadata"] == 0
    assert audit["dependency_checks_total"] == 0


def test_38_r3f_dependency_metadata_coverage_is_explicit() -> None:
    payload = {
        "actions": {
            "LINK": [
                {"name": "known", "version": "1", "build_string": "h0"},
                {"name": "unknown", "version": "1", "build_string": "h0"},
            ],
            "FETCH": [{"name": "known", "version": "1", "build": "h0", "depends": ["unknown >=1"]}],
        }
    }
    audit = audit_plan(payload)
    assert audit["records_total"] == 2
    assert audit["records_with_dependency_metadata"] == 1
    assert audit["dependency_metadata_complete"] is False
    assert audit["dependency_checks_total"] == 1
    summary = plan_rejection_summary(audit)
    assert "dependency_metadata_incomplete" in summary["failure_gates"]


def test_39_r3f_r3e_solved_builds_are_generically_normalized() -> None:
    audit = audit_plan(r3e_schema_payload())
    assert audit["passed"], audit
    assert audit["dependency_metadata_complete"] is True
    assert audit["records_with_dependency_metadata"] == audit["records_total"]
    for name, build in R3E_CORE_BUILDS.items():
        assert audit["core_versions"][name]["build"] == build
    assert audit["blas_family"]["libblas"]["build"] == "9_h0adab6e_openblas"
    assert audit["blas_family"]["libcblas"]["build"] == "9_h2a8eebe_openblas"
    assert audit["blas_family"]["liblapack"]["build"] == "9_hd232482_openblas"
    assert all(row["openblas_variant"] for row in audit["blas_family"].values())
    packages = {row["name"]: row for row in audit["packages"]}
    assert packages["libopenblas"]["build"] == "openmp_hdb726d1_0"
    assert packages["llvm-openmp"]["build"] == "h4fa8253_0"
    assert not ({"mkl", "mkl-devel", "mkl-service", "intel-openmp"} & packages.keys())


def test_40_r3f_link_membership_survives_partial_fetch_with_cache(tmp_path: Path) -> None:
    cache = tmp_path / "pkgs"
    link = []
    fetch = []
    for index in range(109):
        name = f"pkg{index:03d}"
        build = f"h{index:03d}_0"
        dist_name = f"{name}-1.0-{build}"
        link.append(
            {
                "name": name,
                "version": "1.0",
                "build_string": build,
                "dist_name": dist_name,
                "channel": "conda-forge",
            }
        )
        if index < 90:
            fetch.append(package(name, "1.0", build, []))
        else:
            metadata = cache / dist_name / "info" / "repodata_record.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(
                json.dumps(
                    {
                        "name": name,
                        "version": "1.0",
                        "build": build,
                        "depends": [],
                        "channel": "conda-forge",
                    }
                ),
                encoding="utf-8",
            )
    rows = plan_records({"actions": {"LINK": link, "FETCH": fetch}}, [cache])
    assert len(rows) == 109
    assert sum(row["metadata_source"] == "LINK+FETCH" for row in rows) == 90
    assert sum(row["metadata_source"] == "LINK+PACKAGE_CACHE" for row in rows) == 19
    assert all(row["dependency_metadata_available"] for row in rows)


def test_41_r3g_exact_builder_matcher_provenance_is_recorded() -> None:
    info = version_matcher_info()
    assert Path(info["builder_python"]).resolve() == Path(sys.executable).resolve()
    assert isinstance(info["conda_import_available"], bool)
    assert isinstance(info["conda_version_spec_import_available"], bool)
    assert isinstance(info["conda_match_spec_import_available"], bool)
    assert info["runtime_matcher"] in {
        "conda.models.version.VersionSpec",
        "r3_component_boundary_fallback",
    }
    audit = audit_plan(valid_payload())
    assert audit["version_matcher"] == info


def test_42_r3g_python_abi_fuzzy_version_has_component_boundaries() -> None:
    cases = {
        "3.12": True,
        "3.12.0": True,
        "3.12.13": True,
        "3.1": False,
        "3.120": False,
        "3.13": False,
        "13.12": False,
    }
    for version, expected in cases.items():
        assert _fallback_version_satisfies(version, "3.12.*") is expected
        assert _version_satisfies(version, "3.12.*") is expected


def test_43_r3g_arrow_fuzzy_version_has_component_boundaries() -> None:
    cases = {
        "25.0.0": True,
        "25.0.0.1": True,
        "25.0.0.post1": True,
        "25.0": False,
        "25.0.1": False,
        "25.0.01": False,
    }
    for version, expected in cases.items():
        assert _fallback_version_satisfies(version, "25.0.0.*") is expected
        assert _version_satisfies(version, "25.0.0.*") is expected


def test_44_r3g_existing_version_operators_and_invalid_specs_remain_fail_closed() -> None:
    passing = (
        ("1.2.3", "1.2.3"),
        ("1.2.3", "==1.2.3"),
        ("1.2.3", "!=1.2.4"),
        ("1.2.3", ">1.2.2"),
        ("1.2.3", ">=1.2.3"),
        ("1.2.3", "<2"),
        ("1.2.3", "<=1.2.3"),
        ("1.2.3", ">=1.2,<2"),
        ("2.0", "1.2.3|2.0"),
        ("1.2.3", "1.*.3"),
    )
    for version, expression in passing:
        assert _version_satisfies(version, expression)
    for expression in ("", "=>1.0", ">=", "1.0||2.0", ">=1.0,", ">=1.0.*"):
        assert not _fallback_version_satisfies("1.2.3", expression)
        assert not _version_satisfies("1.2.3", expression)


def test_45_r3g_all_fourteen_r3f_false_rejections_are_satisfied() -> None:
    packages = {
        "python_abi": {"version": "3.12", "build": "8_cp312"},
        "libarrow-acero": {"version": "25.0.0", "build": "h123_3"},
        "libarrow": {"version": "25.0.0", "build": "h20c36f3_3_cpu"},
        "libarrow-compute": {"version": "25.0.0", "build": "h081cd8e_3_cpu"},
    }
    dependencies = (
        ["python_abi 3.12.* *_cp312"] * 8
        + ["libarrow-acero 25.0.0.*"] * 2
        + ["libarrow 25.0.0.* *cpu"] * 2
        + ["libarrow-compute 25.0.0.* *cpu"] * 2
    )
    results = [dependency_satisfied(dependency, packages) for dependency in dependencies]
    assert len(results) == 14
    assert all(passed and reason == "SATISFIED" for passed, reason in results)


def test_46_r3g_r3f_transaction_fixture_runs_502_dependency_checks_without_rejection() -> None:
    payload = r3e_schema_payload()
    targets = (
        package("python_abi", "3.12", "8_cp312"),
        package("libarrow-acero", "25.0.0", "h123_3"),
        package("libarrow", "25.0.0", "h20c36f3_3_cpu"),
        package("libarrow-compute", "25.0.0", "h081cd8e_3_cpu"),
    )
    for row in targets:
        payload["actions"]["LINK"].append(
            {
                "name": row["name"],
                "version": row["version"],
                "build_string": row["build"],
                "dist_name": f'{row["name"]}-{row["version"]}-{row["build"]}',
                "channel": "conda-forge",
            }
        )
        payload["actions"]["FETCH"].append(row)

    numpy = next(row for row in payload["actions"]["FETCH"] if row["name"] == "numpy")
    numpy["depends"] = (
        ["python >=3.12"] * 484
        + ["python_abi 3.12.* *_cp312"] * 8
        + ["libarrow-acero 25.0.0.*"] * 2
        + ["libarrow 25.0.0.* *cpu"] * 2
        + ["libarrow-compute 25.0.0.* *cpu"] * 2
    )
    audit = audit_plan(payload)
    assert audit["dependency_metadata_complete"] is True
    assert audit["dependency_checks_total"] == 502
    assert audit["unsatisfied_dependencies"] == []
    assert audit["passed"] is True


def test_47_r3g_build_matching_and_package_plan_policy_are_unchanged() -> None:
    packages = {"python_abi": {"version": "3.12", "build": "8_cp311"}}
    assert dependency_satisfied("python_abi 3.12.* *_cp312", packages) == (
        False,
        "VERSION_OR_BUILD_MISMATCH",
    )
    source = inspect.getsource(audit_plan)
    assert "dependency_metadata_complete and not unsatisfied" in source
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    match = re.search(r"\$Specs = @\(.*?\n\)", builder, flags=re.DOTALL)
    assert match is not None
    normalized = match.group(0).replace("\r\n", "\n").encode()
    assert hashlib.sha256(normalized).hexdigest() == "dbe2f12bbe5e33a26c638d80ae44577fe5e95c941e096af108f3baae7c8aff81"


def test_48_r3h_failed_receipt_audit_writes_complete_diagnostic_before_exit(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    prefix = tmp_path / "candidate"
    conda_meta = prefix / "conda-meta"
    output = tmp_path / "github_receipt_and_file_ownership.json"
    conda_meta.mkdir(parents=True)
    plan.write_text(
        json.dumps({"packages": [{"name": "libopenblas", "version": "1", "build": "h0"}]}),
        encoding="utf-8",
    )
    shared_file = "Library/bin/libopenblas.dll"
    receipts = (
        {"name": "libopenblas", "version": "1", "build": "h0", "files": [shared_file]},
        {"name": "shadow-runtime", "version": "1", "build": "h1", "files": [shared_file]},
    )
    for index, receipt in enumerate(receipts):
        (conda_meta / f"receipt-{index}.json").write_text(json.dumps(receipt), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "r3_audit.py"),
            "receipts",
            "--plan",
            str(plan),
            "--prefix",
            str(prefix),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert output.is_file()
    diagnostic = json.loads(output.read_text(encoding="utf-8"))
    assert diagnostic["passed"] is False
    assert diagnostic["plan_vs_receipts"] == {
        "passed": False,
        "missing": [],
        "extra": [["shadow-runtime", "1", "h1"]],
        "planned_count": 1,
        "installed_count": 2,
    }
    assert diagnostic["ownership"]["critical_conflicts"] == {
        "library\\bin\\libopenblas.dll": ["libopenblas-1-h0", "shadow-runtime-1-h1"]
    }


def test_49_r3h_builder_preserves_receipt_evidence_summarizes_and_fails_closed() -> None:
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    receipt_start = builder.index("$ReceiptAudit =")
    probes_start = builder.index("$ActivePrefix =", receipt_start)
    receipt_block = builder[receipt_start:probes_start]

    for required in (
        "receipt_audit.stdout.txt",
        "receipt_audit.stderr.txt",
        "-AllowFailure",
        "if ($ReceiptAuditResult.ExitCode -ne 0)",
        "Copy-Item -LiteralPath $Logs",
        "planned_count",
        "installed_count",
        "missing",
        "extra",
        "critical_conflicts",
        "ConvertTo-Json -Compress",
        "STAGE1B6J_R3_GITHUB_RECEIPT_AND_FILE_OWNERSHIP_AUDIT_REJECTED",
    ):
        assert required in receipt_block
    assert receipt_block.index("-AllowFailure") < receipt_block.index("if ($ReceiptAuditResult.ExitCode -ne 0)")
    assert receipt_block.index("Copy-Item -LiteralPath $Logs") < receipt_block.index("throw 'STAGE1B6J")


def test_50_r3h_existing_failure_artifact_uploads_all_receipt_diagnostics() -> None:
    workflow = (ROOT / ".github/workflows/phase4c-stage1b6j-r3-openblas-runtime.yml").read_text(encoding="utf-8")
    failure_step = workflow.split("- name: Upload package-plan failure diagnostics", 1)[1].split(
        "- name: Upload only the fully validated runtime artifact", 1
    )[0]
    assert "phase4c-stage1b6j-r3-package-plan-failure-diagnostics" in failure_step
    for path in (
        "github_receipt_and_file_ownership.json",
        "logs/receipt_audit.stdout.txt",
        "logs/receipt_audit.stderr.txt",
    ):
        assert f"phase4c_stage1b6j_r3_artifact/{path}" in failure_step
    assert "runtime.tar.gz" not in failure_step


def test_51_r3i_existing_failure_artifact_uploads_install_transaction_diagnostics() -> None:
    workflow = (ROOT / ".github/workflows/phase4c-stage1b6j-r3-openblas-runtime.yml").read_text(encoding="utf-8")
    failure_step = workflow.split("Upload package-plan failure diagnostics", 1)[1].split("Upload only the fully validated runtime artifact", 1)[0]
    required = (
        "installed_conda_list.json",
        "installed_conda_explicit.txt",
        "logs/environment_creation.stdout.txt",
        "logs/environment_creation.stderr.txt",
        "logs/conda_explicit.stderr.txt",
    )
    assert all(f"phase4c_stage1b6j_r3_artifact/{path}" in failure_step for path in required)
    assert all(path not in failure_step for path in ("runtime.tar.gz", "phase4c_stage1b6j_r3_scientific_payload.zip"))


def test_52_r3i_builder_materializes_install_evidence_at_receipt_gate() -> None:
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    receipt_start = builder.index("$ReceiptAudit =")
    evidence_before_gate = ("environment_creation.stdout.txt", "environment_creation.stderr.txt", "installed_conda_explicit.txt", "conda_explicit.stderr.txt")
    assert all(builder.index(path) < receipt_start for path in evidence_before_gate)
    failure_block = builder[receipt_start:builder.index("$ActivePrefix =", receipt_start)]
    assert "installed_conda_list.json" in failure_block
    assert "--installed-list-output" in failure_block
    assert failure_block.index("Copy-Item -LiteralPath $Logs") < failure_block.index("throw 'STAGE1B6J")


def test_53_r3j_receipt_inventory_is_conda_only_and_survives_rejection(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    prefix = tmp_path / "candidate"
    conda_meta = prefix / "conda-meta"
    audit_output = tmp_path / "github_receipt_and_file_ownership.json"
    inventory_output = tmp_path / "installed_conda_list.json"
    conda_meta.mkdir(parents=True)
    plan.write_text(
        json.dumps(
            {
                "packages": [
                    package("scipy", "1.18.0", "py312h9b3c559_0"),
                    package("pyarrow-core", "25.0.0", "py312h12c7521_0_cpu"),
                ]
            }
        ),
        encoding="utf-8",
    )
    scipy_receipt = {
        **package("scipy", "1.18.0", "py312h9b3c559_0"),
        "receipt_path": str(conda_meta / "scipy.json"),
        "files": ["Lib/site-packages/scipy-1.18.0.dist-info/RECORD"],
    }
    (conda_meta / "scipy.json").write_text(json.dumps(scipy_receipt), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "r3_audit.py"),
            "receipts",
            "--plan",
            str(plan),
            "--prefix",
            str(prefix),
            "--output",
            str(audit_output),
            "--installed-list-output",
            str(inventory_output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert audit_output.is_file()
    assert inventory_output.is_file()
    inventory = json.loads(inventory_output.read_text(encoding="utf-8"))
    assert inventory == receipt_inventory_records([scipy_receipt])
    assert inventory[0]["channel"] == "conda-forge"
    assert inventory[0]["build_string"] == "py312h9b3c559_0"
    assert all(row["platform"] == "win-64" for row in inventory)
    assert all(row["channel"] != "pypi" for row in inventory)


def test_54_r3j_builder_cannot_run_destructive_interoperable_inventory() -> None:
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    assert "$env:CONDA_PREFIX_DATA_INTEROPERABILITY = 'false'" in builder
    assert builder.index("CONDA_PREFIX_DATA_INTEROPERABILITY") < builder.index("$dryRunArgs =")
    assert "@('list', '--prefix', $Candidate, '--json')" not in builder
    receipt_start = builder.index("$ReceiptAudit =")
    receipt_block = builder[receipt_start:builder.index("$ActivePrefix =", receipt_start)]
    assert "--installed-list-output" in receipt_block
    assert receipt_block.index("--installed-list-output") < receipt_block.index("if ($ReceiptAuditResult.ExitCode -ne 0)")


def test_55_r3k_builder_preserves_dll_probe_evidence_summarizes_and_fails_closed() -> None:
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    dll_start = builder.index("$dllPaths = @()")
    imports_start = builder.index("$importPaths = @()", dll_start)
    dll_block = builder[dll_start:imports_start]

    for required in (
        "Invoke-Probe $CandidatePython $mode 'dll' $path -AllowFailure",
        "if ($dllProbeResult.ExitCode -ne 0)",
        "external_numerical_modules",
        "mkl_or_intel_thread_modules",
        "R3 DLL-probe rejection summary:",
        "ConvertTo-Json -Compress",
        "STAGE1B6J_R3_GITHUB_DLL_RESOLUTION_PROBE_REJECTED",
    ):
        assert required in dll_block
    assert dll_block.index("-AllowFailure") < dll_block.index("if ($dllProbeResult.ExitCode -ne 0)")
    assert dll_block.index("R3 DLL-probe rejection summary:") < dll_block.index("throw 'STAGE1B6J")


def test_56_r3k_failure_artifact_uploads_dll_probe_record_and_streams() -> None:
    workflow = (ROOT / ".github/workflows/phase4c-stage1b6j-r3-openblas-runtime.yml").read_text(encoding="utf-8")
    failure_step = workflow.split("Upload package-plan failure diagnostics", 1)[1].split(
        "Upload only the fully validated runtime artifact", 1
    )[0]
    for path in (
        "probe_records/github_dll_*.json",
        "probe_records/github_dll_*.json.stdout.txt",
        "probe_records/github_dll_*.json.stderr.txt",
    ):
        assert f"phase4c_stage1b6j_r3_artifact/{path}" in failure_step
    assert "runtime.tar.gz" not in failure_step


def test_57_r3l_windows_module_enumerator_uses_pointer_sized_api_types() -> None:
    source = (TOOLS / "r3_runtime_probe.py").read_text(encoding="utf-8")
    for required in (
        'ctypes.WinDLL("kernel32", use_last_error=True)',
        'ctypes.WinDLL("psapi", use_last_error=True)',
        "get_current_process.restype = wintypes.HANDLE",
        "ctypes.POINTER(wintypes.HMODULE)",
        "ctypes.POINTER(wintypes.DWORD)",
        "get_module_filename.argtypes",
        "ctypes.WinError(ctypes.get_last_error())",
    ):
        assert required in source

    if sys.platform == "win32":
        from r3_runtime_probe import loaded_modules

        modules = loaded_modules()
        assert modules
        assert any(Path(path).name.lower().startswith("python") for path in modules)


def test_58_r3m_activated_probe_uses_cmd_native_raw_quoting() -> None:
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    native_start = builder.index("function Invoke-NativeCaptured")
    probe_start = builder.index("function Invoke-Probe", native_start)
    native_block = builder[native_start:probe_start]
    probe_end = builder.index("function Merge-Probes", probe_start)
    probe_block = builder[probe_start:probe_end]

    for required in (
        "[string]$RawCommandLine = ''",
        "$start.Arguments = $RawCommandLine",
        "$cmdCommandLine = '/d /s /c \"' + $command + '\"'",
        "Invoke-NativeCaptured $CmdExe @()",
        "-RawCommandLine $cmdCommandLine",
    ):
        assert required in builder
    assert native_block.index("$start.Arguments = $RawCommandLine") < native_block.index("$start.ArgumentList.Add")
    assert "Invoke-NativeCaptured $CmdExe @('/d', '/s', '/c', $command)" not in probe_block


def test_59_r3n_activated_probe_uses_trusted_conda_activation_batch() -> None:
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    probe_start = builder.index("function Invoke-Probe")
    probe_end = builder.index("function Merge-Probes", probe_start)
    probe_block = builder[probe_start:probe_end]

    for required in (
        "Join-Path $env:CONDA 'condabin\\conda.bat'",
        "Test-Path -LiteralPath $CondaBat -PathType Leaf",
        "Test-R3PathUnderRoot -Path $CondaBat -Root $env:CONDA",
        "trusted conda activation batch is unavailable",
        'call `"$CondaBat`" activate `"$ActivePrefix`"',
        "conda_activation_batch",
        "expected_setup_miniconda_root = $env:CONDA",
    ):
        assert required in builder
    assert "$ActivePrefix\\Scripts\\activate.bat" not in probe_block
    assert probe_block.index('$CondaBat`" activate') < probe_block.index('$Python`"')


def test_60_r3o_builder_preserves_import_probe_evidence_summarizes_and_fails_closed() -> None:
    builder = (TOOLS / "build_r3_openblas.ps1").read_text(encoding="utf-8")
    import_start = builder.index("$importPaths = @()")
    blas_start = builder.index("$blasPaths = @()", import_start)
    import_block = builder[import_start:blas_start]

    for required in (
        "Invoke-Probe $CandidatePython $mode 'import' $path $module -AllowFailure",
        "if ($importProbeResult.ExitCode -ne 0)",
        "mode = $mode",
        "module = $module",
        "iteration = $iteration",
        "exit_code = $importProbeResult.ExitCode",
        "record_preserved = Test-Path",
        "stdout_path = $path + '.stdout.txt'",
        "stderr_path = $path + '.stderr.txt'",
        "stderr_tail = $stderrTail",
        "R3 import-probe rejection summary:",
        "STAGE1B6J_R3_GITHUB_IMPORT_SMOKE_PROBE_REJECTED",
    ):
        assert required in import_block
    assert import_block.index("-AllowFailure") < import_block.index("if ($importProbeResult.ExitCode -ne 0)")
    assert import_block.index("R3 import-probe rejection summary:") < import_block.index("throw 'STAGE1B6J")


def test_61_r3o_failure_artifact_uploads_import_probe_records_and_streams() -> None:
    workflow = (ROOT / ".github/workflows/phase4c-stage1b6j-r3-openblas-runtime.yml").read_text(encoding="utf-8")
    failure_step = workflow.split("Upload package-plan failure diagnostics", 1)[1].split(
        "Upload only the fully validated runtime artifact", 1
    )[0]
    for path in (
        "probe_records/github_import_*.json",
        "probe_records/github_import_*.json.stdout.txt",
        "probe_records/github_import_*.json.stderr.txt",
    ):
        assert f"phase4c_stage1b6j_r3_artifact/{path}" in failure_step
    assert "runtime.tar.gz" not in failure_step
