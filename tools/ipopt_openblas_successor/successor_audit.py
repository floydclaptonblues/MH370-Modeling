from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag

import sys


HERE = Path(__file__).resolve().parent
R3_TOOLS = HERE.parent / "ipopt_r3_builder"
sys.path.insert(0, str(R3_TOOLS))

from r3_audit import audit_plan  # noqa: E402


BACKEND_RECEIPTS = ("libblas", "libcblas", "liblapack", "libopenblas")
PROHIBITED = ("mkl", "mkl-devel", "mkl-include", "mkl-service")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def plan_command(args: argparse.Namespace) -> int:
    payload = read_json(args.dry_run)
    conda_info = read_json(args.conda_info)
    package_dirs = tuple(Path(path) for path in conda_info.get("pkgs_dirs", []))
    result = audit_plan(payload, package_dirs)
    prohibited = sorted(
        row["name"] for row in result["packages"] if str(row["name"]).lower() in PROHIBITED
    )
    result["successor_prohibited_mkl_packages"] = prohibited
    result["passed"] = bool(result["passed"] and not prohibited)
    result["classification"] = (
        "STAGE1B6F_OPENBLAS_SUCCESSOR_PLAN_PASS"
        if result["passed"]
        else "STAGE1B6F_OPENBLAS_SUCCESSOR_PLAN_REJECTED"
    )
    write_json(args.output, result)
    return 0 if result["passed"] else 1


def _explicit_records(path: Path) -> list[dict[str, str | None]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value.startswith(("https://", "http://")):
            continue
        url, fragment = urldefrag(value)
        records.append({"url": url, "hash": fragment.lower() or None})
    return sorted(records, key=lambda row: row["url"])


def receipts_command(args: argparse.Namespace) -> int:
    plan = read_json(args.plan)
    prefix = args.prefix.resolve()
    planned_by_url = {str(row["url"]): row for row in plan["packages"]}
    installed_records = _explicit_records(args.explicit)
    installed_by_url = {str(row["url"]): row for row in installed_records}
    planned_urls = sorted(planned_by_url)
    installed_urls = sorted(installed_by_url)
    missing_urls = sorted(set(planned_urls) - set(installed_urls))
    extra_urls = sorted(set(installed_urls) - set(planned_urls))
    mismatched_hashes = []
    for url in sorted(set(planned_urls) & set(installed_urls)):
        planned = planned_by_url[url]
        observed = installed_by_url[url]["hash"]
        allowed = {
            str(value).lower()
            for value in (planned.get("md5"), planned.get("sha256"))
            if value
        }
        if not observed or observed not in allowed:
            mismatched_hashes.append(
                {"url": url, "observed": observed, "allowed_plan_hashes": sorted(allowed)}
            )

    receipt_rows: dict[str, dict[str, Any]] = {}
    receipt_evidence: list[dict[str, Any]] = []
    for path in sorted((prefix / "conda-meta").glob("*.json")):
        row = read_json(path)
        name = str(row.get("name", "")).lower()
        receipt_rows[name] = row
        receipt_evidence.append(
            {
                "name": name,
                "version": row.get("version"),
                "build": row.get("build"),
                "path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )

    backend = {}
    for name in BACKEND_RECEIPTS:
        row = receipt_rows.get(name)
        backend[name] = {
            "present": row is not None,
            "version": None if row is None else row.get("version"),
            "build": None if row is None else row.get("build"),
            "openblas_variant": row is not None and (
                name == "libopenblas" or "openblas" in str(row.get("build", "")).lower()
            ),
        }

    prohibited = sorted(name for name in PROHIBITED if name in receipt_rows)
    exact_plan_match = (
        not missing_urls
        and not extra_urls
        and not mismatched_hashes
        and len(planned_urls) == len(installed_urls)
    )
    passed = exact_plan_match and all(row["present"] and row["openblas_variant"] for row in backend.values()) and not prohibited
    result = {
        "classification": (
            "STAGE1B6F_OPENBLAS_SUCCESSOR_RECEIPT_PASS"
            if passed
            else "STAGE1B6F_OPENBLAS_SUCCESSOR_RECEIPT_REJECTED"
        ),
        "passed": passed,
        "plan_vs_explicit": {
            "passed": exact_plan_match,
            "planned_count": len(planned_urls),
            "installed_count": len(installed_urls),
            "missing_urls": missing_urls,
            "extra_urls": extra_urls,
            "mismatched_hashes": mismatched_hashes,
        },
        "conda_meta_receipt_count": len(receipt_evidence),
        "backend_receipts": backend,
        "prohibited_mkl_receipts": prohibited,
        "receipt_evidence": receipt_evidence,
    }
    write_json(args.output, result)
    return 0 if passed else 1


def compare_command(args: argparse.Namespace) -> int:
    left = read_json(args.left)
    right = read_json(args.right)

    def canonical(plan: dict[str, Any]) -> list[dict[str, Any]]:
        keys = ("name", "version", "build", "channel", "subdir", "url", "sha256", "md5", "size")
        return sorted(
            ({key: row.get(key) for key in keys} for row in plan["packages"]),
            key=lambda row: (str(row["name"]), str(row["version"]), str(row["build"])),
        )

    left_rows = canonical(left)
    right_rows = canonical(right)
    passed = left_rows == right_rows
    result = {
        "classification": (
            "STAGE1B6F_OPENBLAS_SUCCESSOR_PLAN_REPRODUCIBILITY_PASS"
            if passed
            else "STAGE1B6F_OPENBLAS_SUCCESSOR_PLAN_REPRODUCIBILITY_REJECTED"
        ),
        "passed": passed,
        "left_package_count": len(left_rows),
        "right_package_count": len(right_rows),
        "determinism_scope": "exact package tuples, channels, URLs, package hashes, and package sizes",
    }
    write_json(args.output, result)
    return 0 if passed else 1


def hash_command(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    output = args.output.resolve()
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.resolve() != output:
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    write_json(
        output,
        {
            "classification": "STAGE1B6F_OPENBLAS_SUCCESSOR_HASH_MANIFEST_COMPLETE",
            "self_excluded": True,
            "record_count": len(records),
            "records": records,
        },
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--dry-run", type=Path, required=True)
    plan.add_argument("--conda-info", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.set_defaults(func=plan_command)
    receipts = sub.add_parser("receipts")
    receipts.add_argument("--plan", type=Path, required=True)
    receipts.add_argument("--explicit", type=Path, required=True)
    receipts.add_argument("--prefix", type=Path, required=True)
    receipts.add_argument("--output", type=Path, required=True)
    receipts.set_defaults(func=receipts_command)
    compare = sub.add_parser("compare")
    compare.add_argument("--left", type=Path, required=True)
    compare.add_argument("--right", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(func=compare_command)
    hashes = sub.add_parser("hash-manifest")
    hashes.add_argument("--root", type=Path, required=True)
    hashes.add_argument("--output", type=Path, required=True)
    hashes.set_defaults(func=hash_command)
    return result


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
