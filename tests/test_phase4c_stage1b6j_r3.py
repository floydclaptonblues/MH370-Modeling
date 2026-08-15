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
    audit_dll_resolution,
    audit_ownership,
    audit_plan,
    classify_dry_run,
    compare_plan_receipts,
    dependency_satisfied,
    plan_rejection_summary,
    plan_records,
    protected_check,
    sha256_file,
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


def test_27_r3e_plan_acceptance_semantics_are_unchanged() -> None:
    normalized = inspect.getsource(audit_plan).replace("\r\n", "\n").encode()
    assert hashlib.sha256(normalized).hexdigest() == "50e9536c2f7a291e0f1fc758d18b96724f07d9d078c07b6fefedf8116d9073c3"


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


def test_32_r3e_failure_artifact_contains_only_preinstall_diagnostics() -> None:
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
    assert "installed_conda" not in failure_step
    assert "runtime.tar.gz" not in failure_step

