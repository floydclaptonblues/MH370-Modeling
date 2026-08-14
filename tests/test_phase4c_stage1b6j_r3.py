from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "ipopt_r3_builder"
sys.path.insert(0, str(TOOLS))

from r3_audit import (  # noqa: E402
    EXPECTED,
    audit_dll_resolution,
    audit_ownership,
    audit_plan,
    classify_dry_run,
    compare_plan_receipts,
    dependency_satisfied,
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
