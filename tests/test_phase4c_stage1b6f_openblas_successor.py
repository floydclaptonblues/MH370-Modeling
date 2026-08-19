from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "ipopt_openblas_successor"


def load_audit():
    spec = importlib.util.spec_from_file_location("successor_audit", TOOLS / "successor_audit.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_01_successor_uses_explicit_openblas_constraints() -> None:
    source = (TOOLS / "build_successor.ps1").read_text(encoding="utf-8")
    for constraint in (
        "libblas=*=*openblas",
        "libcblas=*=*openblas",
        "liblapack=*=*openblas",
        "libopenblas",
    ):
        assert constraint in source
    for package in ("mkl", "mkl-devel", "mkl-include", "mkl-service"):
        assert package in source


def test_02_numerical_core_versions_remain_exact() -> None:
    source = (TOOLS / "build_successor.ps1").read_text(encoding="utf-8")
    for specification in (
        "python=3.12.13",
        "numpy=2.5.2",
        "scipy=1.18.0",
        "cyipopt=1.7.0",
        "ipopt=3.14.19",
        "mumps-seq=5.8.2",
    ):
        assert specification in source


def test_03_two_dry_plans_are_compared_before_creation() -> None:
    source = (TOOLS / "build_successor.ps1").read_text(encoding="utf-8")
    first = source.index("$DryRun1")
    second = source.index("$DryRun2")
    compare = source.index("'compare'")
    create = source.index("$CreateArgs")
    assert first < second < compare < create


def test_04_plan_vs_installed_requires_exact_explicit_url_match(tmp_path: Path) -> None:
    audit = load_audit()
    plan = tmp_path / "plan.json"
    explicit = tmp_path / "explicit.txt"
    prefix = tmp_path / "prefix"
    metadata = prefix / "conda-meta"
    metadata.mkdir(parents=True)
    packages = []
    for name, build in (
        ("libblas", "9_openblas"),
        ("libcblas", "9_openblas"),
        ("liblapack", "9_openblas"),
        ("libopenblas", "openmp_0"),
    ):
        url = f"https://conda.anaconda.org/conda-forge/win-64/{name}-1.0-{build}.conda"
        packages.append(
            {"name": name, "version": "1.0", "build": build, "url": url, "md5": "a" * 32}
        )
        (metadata / f"{name}-1.0-{build}.json").write_text(
            json.dumps({"name": name, "version": "1.0", "build": build}), encoding="utf-8"
        )
    plan.write_text(json.dumps({"packages": packages}), encoding="utf-8")
    explicit.write_text(
        "@EXPLICIT\n" + "\n".join(row["url"] + "#" + row["md5"] for row in packages) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "receipts.json"
    args = type("Args", (), {"plan": plan, "explicit": explicit, "prefix": prefix, "output": output})()
    assert audit.receipts_command(args) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["plan_vs_explicit"]["passed"]


def test_05_mkl_receipt_is_rejected(tmp_path: Path) -> None:
    audit = load_audit()
    plan = tmp_path / "plan.json"
    explicit = tmp_path / "explicit.txt"
    prefix = tmp_path / "prefix"
    metadata = prefix / "conda-meta"
    metadata.mkdir(parents=True)
    packages = []
    for name, build in (
        ("libblas", "9_openblas"),
        ("libcblas", "9_openblas"),
        ("liblapack", "9_openblas"),
        ("libopenblas", "openmp_0"),
        ("mkl", "0"),
    ):
        url = f"https://conda.anaconda.org/conda-forge/win-64/{name}-1.0-{build}.conda"
        packages.append(
            {"name": name, "version": "1.0", "build": build, "url": url, "md5": "b" * 32}
        )
        (metadata / f"{name}-1.0-{build}.json").write_text(
            json.dumps({"name": name, "version": "1.0", "build": build}), encoding="utf-8"
        )
    plan.write_text(json.dumps({"packages": packages}), encoding="utf-8")
    explicit.write_text(
        "@EXPLICIT\n" + "\n".join(row["url"] + "#" + row["md5"] for row in packages) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "receipts.json"
    args = type("Args", (), {"plan": plan, "explicit": explicit, "prefix": prefix, "output": output})()
    assert audit.receipts_command(args) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["prohibited_mkl_receipts"] == ["mkl"]


def test_06_runtime_probe_cannot_optimize_or_import_mh370() -> None:
    source = (TOOLS / "runtime_probe.py").read_text(encoding="utf-8")
    assert "cyipopt.Problem" not in source
    assert "problem.solve(" not in source
    assert "scipy.optimize" not in source
    assert ".minimize(" not in source
    assert "mh370_endpoint" not in source
    assert '"optimization_executed": False' in source
    assert '"mh370_model_imported_or_executed": False' in source


def test_07_runtime_gate_requires_openblas_and_rejects_mkl() -> None:
    source = (TOOLS / "runtime_probe.py").read_text(encoding="utf-8")
    assert '"openblas" in Path(path).name.lower()' in source
    assert 'Path(path).name.lower().startswith("mkl")' in source
    assert "passed = bool(openblas) and not mkl" in source
    assert "ipopt-3.dll" in source and "dmumps.dll" in source


def test_08_workflow_uploads_only_validated_successor_on_success() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-ipopt-openblas-successor.yml").read_text(
        encoding="utf-8"
    )
    assert "workflow_dispatch" in workflow
    assert "codex/stage1b6f-openblas-successor" in workflow
    assert "if: ${{ success() }}" in workflow
    assert "phase4c_stage1b6f_openblas_successor_bundle.zip.sha256.txt" in workflow
    assert "!phase4c_stage1b6f_openblas_successor_output/**/*.tar.gz" in workflow


def test_09_mkl_include_is_part_of_plan_and_receipt_rejection_gates() -> None:
    source = (TOOLS / "successor_audit.py").read_text(encoding="utf-8")
    assert '"mkl-include"' in source
    assert 'result["passed"] = bool(result["passed"] and not prohibited)' in source


def test_10_explicit_transaction_manifest_requests_package_hashes() -> None:
    source = (TOOLS / "build_successor.ps1").read_text(encoding="utf-8")
    assert "@('list', '--prefix', $Candidate, '--explicit', '--md5')" in source
