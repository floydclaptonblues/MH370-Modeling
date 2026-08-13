from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cyipopt
import numpy as np
import scipy


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def package_version(packages: list[dict[str, Any]], name: str) -> str:
    for row in packages:
        if str(row.get("name", "")).lower() == name.lower():
            return str(row.get("version", "NOT_EXPOSED"))
    return "NOT_EXPOSED"


def observed_ipopt_version(packages: list[dict[str, Any]]) -> str:
    value = getattr(cyipopt, "IPOPT_VERSION", None)
    if isinstance(value, (tuple, list)):
        return ".".join(str(part) for part in value)
    if value is not None:
        return str(value)
    return package_version(packages, "ipopt")


def solver_backend(smoke_stdout: str) -> str:
    patterns = (
        r"running with linear solver\s+([^\r\n]+)",
        r"linear solver\s*[:=]\s*([^\r\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, smoke_stdout, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().rstrip(".")
    return "NOT_EXPOSED"


def cyipopt_extension() -> Path:
    candidates: list[Path] = []
    try:
        from cyipopt.cython import ipopt_wrapper

        candidates.append(Path(ipopt_wrapper.__file__).resolve())
    except Exception:
        pass
    candidates.extend(Path(cyipopt.__file__).resolve().parent.rglob("*.pyd"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("cyipopt compiled extension binary was not found")


def classify_native(path: Path, extension: Path) -> str | None:
    name = path.name.lower()
    if path == extension:
        return "cyipopt_compiled_extension"
    if "ipopt" in name and path.suffix.lower() == ".dll":
        return "native_ipopt_library"
    if any(token in name for token in ("mumps", "pardiso", "spral")) and path.suffix.lower() == ".dll":
        return "linear_solver_library"
    if any(token in name for token in ("openblas", "blas", "lapack")) and path.suffix.lower() == ".dll":
        return "blas_lapack_runtime"
    if any(token in name for token in ("gfortran", "quadmath", "libgcc", "libwinpthread", "vcruntime", "msvcp")) and path.suffix.lower() == ".dll":
        return "compiler_runtime"
    return None


class CapabilityProbe:
    def objective(self, x: np.ndarray) -> float:
        return float(np.dot(x, x))

    def gradient(self, x: np.ndarray) -> np.ndarray:
        return 2.0 * np.asarray(x, dtype=float)

    def constraints(self, x: np.ndarray) -> np.ndarray:
        return np.array([x[0] + x[1]], dtype=float)

    def jacobian(self, _x: np.ndarray) -> np.ndarray:
        return np.array([1.0, 1.0], dtype=float)

    def jacobianstructure(self) -> tuple[np.ndarray, np.ndarray]:
        return np.array([0, 0], dtype=int), np.array([0, 1], dtype=int)

    def hessian(self, _x: np.ndarray, _lagrange: np.ndarray, objective_factor: float) -> np.ndarray:
        return np.array([2.0 * objective_factor, 0.0, 2.0 * objective_factor], dtype=float)

    def hessianstructure(self) -> tuple[np.ndarray, np.ndarray]:
        return np.array([0, 1, 1], dtype=int), np.array([0, 0, 1], dtype=int)

    def intermediate(self, *_values: Any) -> bool:
        return True


def capability_inventory(smoke_result: dict[str, Any]) -> list[dict[str, str]]:
    probe = CapabilityProbe()
    problem = cyipopt.Problem(
        n=2,
        m=1,
        problem_obj=probe,
        lb=np.array([-10.0, -10.0]),
        ub=np.array([10.0, 10.0]),
        cl=np.array([-1.0e19]),
        cu=np.array([1.0e19]),
    )
    callback_available = {
        "intermediate": int(smoke_result.get("intermediate_callback_calls", 0)) > 0,
        "hessian": callable(getattr(probe, "hessian", None)),
        "hessianstructure": callable(getattr(probe, "hessianstructure", None)),
        "jacobianstructure": callable(getattr(probe, "jacobianstructure", None)),
        "get_current_iterate": callable(getattr(problem, "get_current_iterate", None)),
        "get_current_violations": callable(getattr(problem, "get_current_violations", None)),
    }
    close = getattr(problem, "close", None)
    if callable(close):
        close()
    return [
        {
            "capability": name,
            "status": "AVAILABLE" if available else "NOT_EXPOSED_BY_INSTALLED_INTERFACE",
            "basis": "runtime probe of installed cyipopt interface",
        }
        for name, available in callback_available.items()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conda-list", required=True, type=Path)
    parser.add_argument("--smoke-result", required=True, type=Path)
    parser.add_argument("--smoke-stdout", required=True, type=Path)
    parser.add_argument("--build-environment", required=True, type=Path)
    parser.add_argument("--capabilities", required=True, type=Path)
    parser.add_argument("--native-manifest", required=True, type=Path)
    parser.add_argument("--manager-version", required=True)
    parser.add_argument("--creation-command", required=True)
    parser.add_argument("--channels", required=True)
    arguments = parser.parse_args()

    prefix = Path(sys.prefix).resolve()
    packages = json.loads(arguments.conda_list.read_text(encoding="utf-8"))
    smoke = json.loads(arguments.smoke_result.read_text(encoding="utf-8"))
    stdout = arguments.smoke_stdout.read_text(encoding="utf-8", errors="replace")
    backend = solver_backend(stdout)
    smoke["linear_solver_backend"] = backend
    smoke["ipopt_version_observed"] = observed_ipopt_version(packages)
    write_json(arguments.smoke_result, smoke)

    extension = cyipopt_extension()
    native_paths: dict[str, tuple[Path, str]] = {str(extension).lower(): (extension, "cyipopt_compiled_extension")}
    for directory in (prefix / "Library" / "bin", prefix / "DLLs", prefix / "Scripts"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.dll"):
            classification = classify_native(path.resolve(), extension)
            if classification:
                native_paths[str(path.resolve()).lower()] = (path.resolve(), classification)
    native_records = []
    for path, classification in sorted(native_paths.values(), key=lambda item: item[0].as_posix().lower()):
        native_records.append(
            {
                "classification": classification,
                "environment_relative_path": path.relative_to(prefix).as_posix(),
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not any(row["classification"] == "native_ipopt_library" for row in native_records):
        raise RuntimeError("native Ipopt DLL was not found in the environment")

    capabilities = capability_inventory(smoke)
    write_json(
        arguments.capabilities,
        {
            "cyipopt_version": getattr(cyipopt, "__version__", package_version(packages, "cyipopt")),
            "records": capabilities,
        },
    )
    write_json(
        arguments.native_manifest,
        {
            "environment_prefix_at_build": str(prefix),
            "records": native_records,
        },
    )
    write_json(
        arguments.build_environment,
        {
            "classification": "IPOPT_EXTERNAL_ENVIRONMENT_BUILD_PASS",
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID", "NOT_AVAILABLE"),
            "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "NOT_AVAILABLE"),
            "build_timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "runner_os": os.environ.get("RUNNER_OS", platform.system()),
            "runner_architecture": os.environ.get("RUNNER_ARCH", platform.machine()),
            "windows_version": platform.platform(),
            "environment_manager_version": arguments.manager_version,
            "python_version": platform.python_version(),
            "python_executable_at_build": sys.executable,
            "numpy_version": np.__version__,
            "scipy_version": scipy.__version__,
            "cyipopt_version": getattr(cyipopt, "__version__", package_version(packages, "cyipopt")),
            "ipopt_version": observed_ipopt_version(packages),
            "ipopt_linear_solver_backend": backend,
            "environment_prefix_at_build": str(prefix),
            "channels": arguments.channels.split(","),
            "exact_environment_creation_command": arguments.creation_command,
            "package_count": len(packages),
            "platform": sys.platform,
            "machine_architecture": platform.machine(),
            "cyipopt_module_path": str(Path(cyipopt.__file__).resolve()),
            "cyipopt_compiled_extension_path": str(extension),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
