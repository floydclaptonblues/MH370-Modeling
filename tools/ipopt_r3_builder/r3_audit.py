from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


EXPECTED = {
    "python": "3.12.13",
    "numpy": "2.5.2",
    "scipy": "1.18.0",
    "cyipopt": "1.7.0",
    "ipopt": "3.14.19",
    "mumps-seq": "5.8.2",
    "pandas": "3.0.5",
    "pyarrow": "25.0.0",
    "pyproj": "3.7.2",
    "pyyaml": "6.0.3",
    "pytest": "9.1.1",
}
PROHIBITED = {"mkl", "mkl-devel", "mkl-service", "intel-openmp"}
NUMERICAL = {
    "numpy", "scipy", "cyipopt", "ipopt", "mumps-seq", "libblas", "libcblas",
    "liblapack", "libopenblas", "libopenblas-ilp64", "libgfortran", "libgfortran5",
    "llvm-openmp", "intel-openmp", "mkl", "mkl-devel", "mkl-service",
}
CRITICAL_DLL_TOKENS = (
    "iomp", "libomp", "openblas", "blas", "lapack", "ipopt", "mumps", "gfortran",
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def classify_dry_run(exit_code: int, payload: Any | None, stderr: str) -> str:
    if exit_code == 0 and isinstance(payload, dict) and payload.get("success") is not False:
        return "STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_PASS"
    text = (stderr + " " + (json.dumps(payload) if payload is not None else "")).lower()
    transport_tokens = (
        "connection", "ssl", "timed out", "timeout", "http ", "proxy", "repodata",
        "could not resolve", "name resolution", "download", "metadata transport",
    )
    if any(token in text for token in transport_tokens):
        return "STAGE1B6J_R3_GITHUB_METADATA_TRANSPORT_FAILURE"
    if any(name in text and version in text for name, version in EXPECTED.items()):
        return "STAGE1B6J_OPENBLAS_PLAN_CORE_VERSION_DRIFT_REQUIRED"
    return "STAGE1B6J_OPENBLAS_PLAN_UNSATISFIABLE"


def canonical_build(row: dict[str, Any]) -> str:
    build = str(row.get("build") or "")
    build_string = str(row.get("build_string") or "")
    if build and build_string and build != build_string:
        raise ValueError(
            "STAGE1B6J_R3_CONDA_BUILD_FIELD_CONFLICT: "
            f"build={build!r} build_string={build_string!r} for "
            f"{row.get('name')} {row.get('version')}"
        )
    return build or build_string


def _plan_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("name", "")).lower(),
        str(row.get("version", "")),
        canonical_build(row),
    )


def _has_metadata_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _merge_plan_rows(
    link: dict[str, Any], rich: dict[str, Any], metadata_source: str
) -> dict[str, Any]:
    merged = dict(link)
    rich_fields = {
        "depends", "constrains", "url", "sha256", "fn", "license", "track_features",
    }
    for name, value in rich.items():
        if name in rich_fields or name not in merged or not _has_metadata_value(merged[name]):
            merged[name] = value
    key = _plan_key(link)
    if _plan_key(rich) != key:
        raise ValueError(
            "STAGE1B6J_R3_CONDA_BUILD_FIELD_CONFLICT: "
            f"LINK and metadata records disagree: LINK={key!r} metadata={_plan_key(rich)!r}"
        )
    merged["name"], merged["version"], merged["build"] = key
    merged["metadata_source"] = metadata_source
    merged["dependency_metadata_available"] = "depends" in rich
    merged["depends"] = list(rich.get("depends") or []) if "depends" in rich else None
    return merged


def _exact_package_cache_metadata(
    link: dict[str, Any], package_cache_dirs: Iterable[Path]
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    key = _plan_key(link)
    dist_name = str(link.get("dist_name") or "-").strip()
    if not dist_name or dist_name == "-":
        dist_name = "-".join(key)
    rejected: list[dict[str, Any]] = []
    for cache_dir in package_cache_dirs:
        info = Path(cache_dir) / dist_name / "info"
        for filename, kind in (
            ("repodata_record.json", "REPODATA_RECORD"),
            ("index.json", "INDEX"),
        ):
            path = info / filename
            if not path.is_file():
                continue
            try:
                candidate = read_json(path)
                observed = _plan_key(candidate)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                rejected.append({"path": str(path), "reason": type(exc).__name__, "detail": str(exc)})
                continue
            if observed != key:
                rejected.append(
                    {
                        "path": str(path),
                        "reason": "EXACT_DISTRIBUTION_MISMATCH",
                        "expected": list(key),
                        "observed": list(observed),
                    }
                )
                continue
            candidate = dict(candidate)
            candidate["package_cache_metadata_path"] = str(path.resolve())
            candidate["package_cache_metadata_kind"] = kind
            return candidate, kind, rejected
    return None, None, rejected


def plan_records(
    payload: dict[str, Any], package_cache_dirs: Iterable[Path] = ()
) -> list[dict[str, Any]]:
    actions = payload.get("actions") or {}
    link_rows = list(actions.get("LINK") or [])
    fetch_rows = list(actions.get("FETCH") or [])
    membership = link_rows or fetch_rows

    fetch_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    fetch_by_name_version: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw in fetch_rows:
        row = dict(raw)
        key = _plan_key(row)
        fetch_by_key[key] = row
        fetch_by_name_version[key[:2]].append(row)

    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    cache_dirs = tuple(Path(path) for path in package_cache_dirs)
    for raw in membership:
        link = dict(raw)
        key = _plan_key(link)
        fetch = fetch_by_key.get(key)
        if fetch is not None:
            merged = _merge_plan_rows(link, fetch, "LINK+FETCH" if link_rows else "FETCH")
        else:
            conflicting_fetch = fetch_by_name_version.get(key[:2], [])
            if conflicting_fetch:
                observed = sorted({_plan_key(row)[2] for row in conflicting_fetch})
                raise ValueError(
                    "STAGE1B6J_R3_CONDA_BUILD_FIELD_CONFLICT: "
                    f"LINK build {key[2]!r} does not match FETCH build(s) {observed!r} "
                    f"for {key[0]} {key[1]}"
                )
            if "depends" in link:
                merged = dict(link)
                merged["name"], merged["version"], merged["build"] = key
                merged["metadata_source"] = "LINK"
                merged["dependency_metadata_available"] = True
                merged["depends"] = list(link.get("depends") or [])
            else:
                cache, cache_kind, rejected = _exact_package_cache_metadata(link, cache_dirs)
                if cache is not None:
                    merged = _merge_plan_rows(link, cache, "LINK+PACKAGE_CACHE")
                    merged["package_cache_metadata_kind"] = cache_kind
                    merged["package_cache_rejections"] = rejected
                else:
                    merged = dict(link)
                    merged["name"], merged["version"], merged["build"] = key
                    merged["metadata_source"] = "LINK_ONLY"
                    merged["dependency_metadata_available"] = False
                    merged["depends"] = None
                    merged["package_cache_rejections"] = rejected
        records[key] = merged
    return [records[key] for key in sorted(records)]


def _version_tokens(value: str) -> tuple[Any, ...]:
    result: list[Any] = []
    for token in re.split(r"([0-9]+)", value.lower().replace("_", ".")):
        if not token:
            continue
        result.append(int(token) if token.isdigit() else token)
    return tuple(result)


def _compare(left: str, right: str) -> int:
    a, b = _version_tokens(left), _version_tokens(right)
    for x, y in zip(a, b):
        if type(x) is type(y):
            if x < y:
                return -1
            if x > y:
                return 1
        else:
            sx, sy = str(x), str(y)
            if sx < sy:
                return -1
            if sx > sy:
                return 1
    return (len(a) > len(b)) - (len(a) < len(b))


def _version_satisfies(version: str, expression: str) -> bool:
    expression = expression.strip()
    if not expression or expression == "*":
        return True
    for alternative in expression.split("|"):
        okay = True
        for clause in alternative.split(","):
            clause = clause.strip()
            match = re.match(r"^(>=|<=|!=|==|>|<|=)?\s*(.+)$", clause)
            if not match:
                okay = False
                continue
            op, wanted = match.group(1) or "=", match.group(2)
            if wanted.endswith(".*"):
                equal = version.startswith(wanted[:-1])
                comparison = 0 if equal else _compare(version, wanted[:-2])
            elif "*" in wanted:
                equal = fnmatch.fnmatch(version, wanted)
                comparison = 0 if equal else 1
            else:
                comparison = _compare(version, wanted)
                equal = comparison == 0
            okay = okay and {
                "=": equal, "==": equal, "!=": not equal, ">": comparison > 0,
                ">=": comparison >= 0, "<": comparison < 0, "<=": comparison <= 0,
            }[op]
        if okay:
            return True
    return False


def dependency_satisfied(dependency: str, packages: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    parts = dependency.split()
    if not parts:
        return True, "EMPTY"
    name = parts[0].lower()
    if name.startswith("__"):
        return True, "VIRTUAL_PACKAGE"
    target = packages.get(name)
    if target is None:
        return False, "PACKAGE_MISSING"
    version_expression = parts[1] if len(parts) >= 2 else "*"
    build_expression = parts[2] if len(parts) >= 3 else "*"
    version_ok = _version_satisfies(str(target["version"]), version_expression)
    build_ok = fnmatch.fnmatch(str(target.get("build", "")), build_expression)
    return version_ok and build_ok, "SATISFIED" if version_ok and build_ok else "VERSION_OR_BUILD_MISMATCH"


def audit_plan(
    payload: dict[str, Any], package_cache_dirs: Iterable[Path] = ()
) -> dict[str, Any]:
    rows = plan_records(payload, package_cache_dirs)
    packages = {str(row["name"]): row for row in rows}
    core = {
        name: {
            "expected": version,
            "observed": packages.get(name, {}).get("version"),
            "build": packages.get(name, {}).get("build"),
            "channel": packages.get(name, {}).get("channel"),
            "url": packages.get(name, {}).get("url"),
            "passed": name in packages and str(packages[name].get("version")) == version,
        }
        for name, version in EXPECTED.items()
    }
    prohibited = sorted(name for name in packages if name in PROHIBITED)
    blas = {
        name: {
            "version": packages.get(name, {}).get("version"),
            "build": packages.get(name, {}).get("build"),
            "build_number": packages.get(name, {}).get("build_number"),
            "url": packages.get(name, {}).get("url"),
            "depends": packages.get(name, {}).get("depends"),
            "openblas_variant": "openblas" in str(packages.get(name, {}).get("build", "")).lower(),
        }
        for name in ("libblas", "libcblas", "liblapack")
    }
    dependency_rows: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("dependency_metadata_available"):
            continue
        for dependency in row.get("depends") or []:
            passed, reason = dependency_satisfied(str(dependency), packages)
            dependency_rows.append({"package": row["name"], "dependency": dependency, "passed": passed, "reason": reason})
    unsatisfied = [row for row in dependency_rows if not row["passed"]]
    incomplete_metadata = [
        {
            "name": row["name"],
            "version": row["version"],
            "build": row["build"],
            "metadata_source": row.get("metadata_source"),
            "package_cache_rejections": row.get("package_cache_rejections", []),
        }
        for row in rows
        if not row.get("dependency_metadata_available")
    ]
    dependency_metadata_complete = not incomplete_metadata
    scipy = packages.get("scipy", {})
    scipy_conda = bool(
        scipy
        and str(scipy.get("version")) == EXPECTED["scipy"]
        and "conda-forge" in (str(scipy.get("channel", "")) + str(scipy.get("url", ""))).lower()
        and str(scipy.get("build", "")) != "pypi_0"
    )
    all_conda_forge = all(
        "conda-forge" in (str(row.get("channel", "")) + str(row.get("url", ""))).lower()
        for row in rows
    )
    passed = bool(
        rows and all(item["passed"] for item in core.values()) and not prohibited
        and all(item["openblas_variant"] for item in blas.values())
        and ("libopenblas" in packages or "libopenblas-ilp64" in packages)
        and scipy_conda and all_conda_forge and dependency_metadata_complete and not unsatisfied
    )
    classification = (
        "STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_PASS"
        if passed
        else (
            "STAGE1B6J_R3_PLAN_DEPENDENCY_METADATA_INCOMPLETE"
            if not dependency_metadata_complete
            else "STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_FAILURE"
        )
    )
    return {
        "classification": classification,
        "passed": passed,
        "package_count": len(rows),
        "records_total": len(rows),
        "records_with_dependency_metadata": len(rows) - len(incomplete_metadata),
        "dependency_metadata_complete": dependency_metadata_complete,
        "dependency_metadata_incomplete_records": incomplete_metadata,
        "packages": rows,
        "core_versions": core,
        "prohibited_packages": prohibited,
        "blas_family": blas,
        "libopenblas": packages.get("libopenblas") or packages.get("libopenblas-ilp64"),
        "openmp_packages": [row for row in rows if "openmp" in row["name"]],
        "scipy_genuine_conda_forge_record": scipy_conda,
        "all_packages_conda_forge": all_conda_forge,
        "dependency_checks_total": len(dependency_rows),
        "dependency_checks": dependency_rows,
        "unsatisfied_dependencies": unsatisfied,
    }


def _package_diagnostic(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "name": row.get("name"),
        "version": row.get("version"),
        "build": row.get("build"),
        "channel": row.get("channel"),
        "url": row.get("url"),
    }


def plan_rejection_summary(audit: dict[str, Any]) -> dict[str, Any]:
    packages = {str(row["name"]): row for row in audit.get("packages", [])}
    core_failures = [
        {
            "name": name,
            "expected": row.get("expected"),
            "observed": row.get("observed"),
            "build": row.get("build"),
            "channel": row.get("channel"),
            "url": row.get("url"),
        }
        for name, row in sorted(audit.get("core_versions", {}).items())
        if not row.get("passed")
    ]
    blas_failures = [
        {
            "name": name,
            "observed_build": row.get("build"),
            "version": row.get("version"),
            "url": row.get("url"),
        }
        for name, row in sorted(audit.get("blas_family", {}).items())
        if not row.get("openblas_variant")
    ]
    non_conda_forge = [
        _package_diagnostic(row)
        for row in audit.get("packages", [])
        if "conda-forge" not in (str(row.get("channel", "")) + str(row.get("url", ""))).lower()
    ]
    openmp_packages = [
        _package_diagnostic(row) for row in audit.get("openmp_packages", [])
    ]
    unsatisfied = []
    for row in audit.get("unsatisfied_dependencies", []):
        dependency = str(row.get("dependency", ""))
        target_name = dependency.split()[0].lower() if dependency.split() else ""
        unsatisfied.append(
            {
                "package": row.get("package"),
                "dependency": row.get("dependency"),
                "reason": row.get("reason"),
                "observed_target": _package_diagnostic(packages.get(target_name)),
            }
        )

    failure_gates = []
    if core_failures:
        failure_gates.append("core_version_mismatch")
    if audit.get("prohibited_packages"):
        failure_gates.append("prohibited_package")
    if blas_failures:
        failure_gates.append("blas_variant")
    if not audit.get("libopenblas"):
        failure_gates.append("libopenblas_missing")
    if not audit.get("scipy_genuine_conda_forge_record"):
        failure_gates.append("scipy_provenance")
    if not audit.get("all_packages_conda_forge"):
        failure_gates.append("non_conda_forge")
    if not audit.get("dependency_metadata_complete"):
        failure_gates.append("dependency_metadata_incomplete")
    if unsatisfied:
        failure_gates.append("dependency_checker")

    dependency_only = failure_gates == ["dependency_checker"]
    return {
        "classification": (
            "STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_PASS"
            if audit.get("passed")
            else "STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_AUDIT_REJECTED"
        ),
        "passed": bool(audit.get("passed")),
        "package_count": int(audit.get("package_count", 0)),
        "plan_classification": audit.get("classification"),
        "records_total": int(audit.get("records_total", 0)),
        "records_with_dependency_metadata": int(audit.get("records_with_dependency_metadata", 0)),
        "dependency_metadata_complete": bool(audit.get("dependency_metadata_complete")),
        "dependency_metadata_incomplete_records": list(audit.get("dependency_metadata_incomplete_records", [])),
        "failure_gates": failure_gates,
        "core_version_failures": core_failures,
        "prohibited_packages": list(audit.get("prohibited_packages", [])),
        "blas_variant_failures": blas_failures,
        "libopenblas_present": bool(audit.get("libopenblas")),
        "scipy_genuine_conda_forge_record": bool(audit.get("scipy_genuine_conda_forge_record")),
        "all_packages_conda_forge": bool(audit.get("all_packages_conda_forge")),
        "non_conda_forge_packages": non_conda_forge,
        "openmp_packages": openmp_packages,
        "dependency_checks_total": int(audit.get("dependency_checks_total", 0)),
        "unsatisfied_dependency_count": len(unsatisfied),
        "unsatisfied_dependencies": unsatisfied,
        "dependency_interpretation_classification": (
            "STAGE1B6J_R3_PACKAGE_AUDITOR_DEPENDENCY_SEMANTICS_MISMATCH_SUSPECTED"
            if dependency_only
            else None
        ),
    }


def receipt_records(prefix: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((prefix / "conda-meta").glob("*.json")):
        row = read_json(path)
        row["receipt_path"] = str(path.resolve())
        rows.append(row)
    return rows


def compare_plan_receipts(plan: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    planned = {(row["name"], row["version"], row["build"]) for row in plan["packages"]}
    installed = {(str(row.get("name", "")).lower(), str(row.get("version", "")), str(row.get("build", ""))) for row in receipts}
    missing = sorted(planned - installed)
    extra = sorted(installed - planned)
    return {"passed": not missing and not extra, "missing": missing, "extra": extra, "planned_count": len(planned), "installed_count": len(installed)}


def audit_ownership(prefix: Path, receipts: list[dict[str, Any]]) -> dict[str, Any]:
    owners: dict[str, list[str]] = defaultdict(list)
    for row in receipts:
        owner = f"{row.get('name')}-{row.get('version')}-{row.get('build')}"
        for relative in row.get("files", []):
            normalized = str(relative).replace("/", "\\").lower()
            owners[normalized].append(owner)
    critical = {
        path: values for path, values in owners.items()
        if path.endswith((".dll", ".pyd")) and any(token in Path(path).name.lower() for token in CRITICAL_DLL_TOKENS)
    }
    conflicts = {path: values for path, values in critical.items() if len(values) > 1}
    return {
        "passed": not conflicts,
        "critical_file_owners": critical,
        "critical_conflicts": conflicts,
        "libiomp5md_owners": owners.get("library\\bin\\libiomp5md.dll", []),
        "libomp_owners": owners.get("library\\bin\\libomp.dll", []),
        "prefix": str(prefix.resolve()),
    }


def audit_dll_resolution(report: dict[str, Any], prefix: Path) -> dict[str, Any]:
    root = prefix.resolve()
    numerical = [Path(path).resolve() for path in report.get("numerical_modules", [])]
    external = [str(path) for path in numerical if not path.is_relative_to(root)]
    contaminated = [
        str(path) for path in numerical
        if any(token in path.name.lower() for token in ("mkl", "mkl_intel_thread", "libiomp5md"))
    ]
    return {"passed": not external and not contaminated, "external": external, "contaminated": contaminated, "prefix": str(root)}


def verify_hash_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    failures = []
    for row in manifest.get("records", []):
        path = root / str(row["path"])
        if not path.is_file():
            failures.append({"path": row["path"], "reason": "MISSING"})
        elif path.stat().st_size != int(row["size_bytes"]):
            failures.append({"path": row["path"], "reason": "SIZE_MISMATCH"})
        elif sha256_file(path) != str(row["sha256"]).lower():
            failures.append({"path": row["path"], "reason": "SHA256_MISMATCH"})
    return {"passed": not failures, "expected": len(manifest.get("records", [])), "matched": len(manifest.get("records", [])) - len(failures), "failures": failures}


def protected_check(root: Path, manifest_path: Path) -> dict[str, Any]:
    document = read_json(manifest_path)
    rows = document.get("protected_records") or document.get("records") or []
    return verify_hash_manifest(root, {"records": rows})


def command_plan(args: argparse.Namespace) -> int:
    payload = read_json(args.dry_run)
    conda_info = read_json(args.conda_info) if args.conda_info is not None else {}
    package_cache_dirs = [Path(path) for path in conda_info.get("pkgs_dirs", [])]
    audit = audit_plan(payload, package_cache_dirs)
    write_json(args.output, audit)
    if args.rejection_summary is not None and not audit["passed"]:
        write_json(args.rejection_summary, plan_rejection_summary(audit))
    return 0 if audit["passed"] else 1


def command_classify(args: argparse.Namespace) -> int:
    payload = None
    if args.dry_run.is_file():
        try:
            payload = read_json(args.dry_run)
        except (OSError, json.JSONDecodeError):
            payload = None
    stderr = args.stderr.read_text(encoding="utf-8", errors="replace") if args.stderr.is_file() else ""
    classification = classify_dry_run(args.exit_code, payload, stderr)
    write_json(args.output, {"exit_code": args.exit_code, "classification": classification, "stderr": stderr, "parsed_json": payload is not None})
    return 0 if classification == "STAGE1B6J_R3_GITHUB_PACKAGE_PLAN_PASS" else 1


def command_receipts(args: argparse.Namespace) -> int:
    plan = read_json(args.plan)
    receipts = receipt_records(args.prefix)
    result = {
        "plan_vs_receipts": compare_plan_receipts(plan, receipts),
        "ownership": audit_ownership(args.prefix, receipts),
        "receipt_count": len(receipts),
        "receipts": receipts,
    }
    result["passed"] = result["plan_vs_receipts"]["passed"] and result["ownership"]["passed"]
    write_json(args.output, result)
    return 0 if result["passed"] else 1


def command_hash(args: argparse.Namespace) -> int:
    records = []
    for path in sorted(p for p in args.root.rglob("*") if p.is_file() and p.resolve() != args.output.resolve()):
        records.append({"path": path.relative_to(args.root).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
    write_json(args.output, {"schema_version": "1", "records": records})
    return 0


def command_protected(args: argparse.Namespace) -> int:
    result = protected_check(args.root, args.manifest)
    write_json(args.output, result)
    return 0 if result["passed"] else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--dry-run", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--rejection-summary", type=Path)
    plan.add_argument("--conda-info", type=Path)
    plan.set_defaults(func=command_plan)
    classify = sub.add_parser("classify")
    classify.add_argument("--dry-run", type=Path, required=True)
    classify.add_argument("--stderr", type=Path, required=True)
    classify.add_argument("--exit-code", type=int, required=True)
    classify.add_argument("--output", type=Path, required=True)
    classify.set_defaults(func=command_classify)
    receipts = sub.add_parser("receipts")
    receipts.add_argument("--plan", type=Path, required=True)
    receipts.add_argument("--prefix", type=Path, required=True)
    receipts.add_argument("--output", type=Path, required=True)
    receipts.set_defaults(func=command_receipts)
    hashes = sub.add_parser("hash-manifest")
    hashes.add_argument("--root", type=Path, required=True)
    hashes.add_argument("--output", type=Path, required=True)
    hashes.set_defaults(func=command_hash)
    protected = sub.add_parser("protected")
    protected.add_argument("--root", type=Path, required=True)
    protected.add_argument("--manifest", type=Path, required=True)
    protected.add_argument("--output", type=Path, required=True)
    protected.set_defaults(func=command_protected)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

