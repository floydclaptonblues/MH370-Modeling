from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def loaded_modules() -> list[str]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    enum_process_modules = psapi.EnumProcessModules
    enum_process_modules.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    enum_process_modules.restype = wintypes.BOOL
    get_module_filename = psapi.GetModuleFileNameExW
    get_module_filename.argtypes = [
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_module_filename.restype = wintypes.DWORD

    process = get_current_process()
    handles = (wintypes.HMODULE * 4096)()
    needed = wintypes.DWORD()
    if not enum_process_modules(process, handles, ctypes.sizeof(handles), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    paths = []
    count = min(int(needed.value // ctypes.sizeof(wintypes.HMODULE)), len(handles))
    for index in range(count):
        buffer = ctypes.create_unicode_buffer(32768)
        if get_module_filename(process, handles[index], buffer, len(buffer)):
            paths.append(str(Path(buffer.value).resolve()))
    return sorted(set(paths), key=str.lower)


def base(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "python_version": platform.python_version(),
        "python_executable": str(Path(sys.executable).resolve()),
        "cwd": str(Path.cwd().resolve()),
        "path": os.environ.get("PATH", ""),
        "conda_prefix": os.environ.get("CONDA_PREFIX"),
    }


def import_probe(module: str, project_root: Path | None, mode: str) -> dict[str, Any]:
    if project_root is not None:
        for candidate in (project_root, project_root / "src"):
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
    if module == "callback_stack":
        __import__("scripts.run_phase4c_stage1b6j")
    else:
        __import__(module)
    modules = loaded_modules()
    return {
        **base(mode),
        "classification": "STAGE1B6J_R3_IMPORT_SMOKE_PASS",
        "passed": True,
        "requested_import": module,
        "loaded_modules": modules,
    }


def blas_probe(mode: str) -> dict[str, Any]:
    import numpy as np
    import scipy
    import scipy.linalg

    left = np.linspace(-1.0, 1.0, 128 * 128, dtype=np.float64).reshape(128, 128)
    right = np.linspace(0.5, 2.5, 128 * 128, dtype=np.float64).reshape(128, 128)
    product = left @ right
    matrix = np.eye(64, dtype=np.float64) * 8.0 + np.fromfunction(lambda i, j: ((i + 2 * j) % 11) / 1000.0, (64, 64), dtype=float)
    matrix = matrix @ matrix.T
    rhs = np.linspace(-2.0, 3.0, 64, dtype=np.float64)
    solution = scipy.linalg.solve(matrix, rhs, assume_a="pos")
    singular = scipy.linalg.svdvals(left[:64, :64])
    eigen = scipy.linalg.eigvalsh(matrix)
    values = np.concatenate((product.ravel(), solution, singular, eigen))
    if not np.all(np.isfinite(values)):
        raise RuntimeError("non-finite BLAS/LAPACK output")
    signature = hashlib.sha256(np.round(values, decimals=12).astype("<f8", copy=False).tobytes()).hexdigest()
    return {
        **base(mode),
        "classification": "STAGE1B6J_R3_OPENBLAS_EXECUTION_PASS",
        "passed": True,
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
        "signature_sha256_rounded_12dp": signature,
        "product_sum": float(np.sum(product)),
        "solve_residual_infinity": float(np.linalg.norm(matrix @ solution - rhs, ord=np.inf)),
        "largest_singular_value": float(singular[0]),
        "smallest_eigenvalue": float(eigen[0]),
        "loaded_modules": loaded_modules(),
    }


class TinyProblem:
    def objective(self, x: Any) -> float:
        return float((x[0] - 1.0) ** 2 + (x[1] - 2.0) ** 2)

    def gradient(self, x: Any) -> Any:
        import numpy as np
        return np.array([2.0 * (x[0] - 1.0), 2.0 * (x[1] - 2.0)], dtype=float)

    def constraints(self, x: Any) -> Any:
        import numpy as np
        return np.array([x[0] + x[1]], dtype=float)

    def jacobian(self, _x: Any) -> Any:
        import numpy as np
        return np.array([1.0, 1.0], dtype=float)


def ipopt_probe(mode: str) -> dict[str, Any]:
    import cyipopt
    import numpy as np

    model = TinyProblem()
    problem = cyipopt.Problem(
        n=2, m=1, problem_obj=model,
        lb=np.array([-10.0, -10.0]), ub=np.array([10.0, 10.0]),
        cl=np.array([3.0]), cu=np.array([1.0e19]),
    )
    problem.add_option("hessian_approximation", "limited-memory")
    problem.add_option("tol", 1.0e-10)
    problem.add_option("constr_viol_tol", 1.0e-10)
    problem.add_option("max_iter", 200)
    problem.add_option("print_level", 0)
    problem.add_option("sb", "yes")
    solution, info = problem.solve(np.array([0.0, 0.0], dtype=float))
    solution = np.asarray(solution, dtype=float)
    coordinate_error = float(np.max(np.abs(solution - np.array([1.0, 2.0]))))
    objective = float(model.objective(solution))
    violation = float(max(0.0, 3.0 - float(np.sum(solution))))
    status = int(info.get("status", -999))
    passed = status in (0, 1) and coordinate_error <= 1.0e-5 and objective <= 1.0e-9 and violation <= 1.0e-8
    return {
        **base(mode),
        "classification": "STAGE1B6J_R3_IPOPT_SMOKE_PASS" if passed else "STAGE1B6J_R3_IPOPT_SMOKE_FAILURE",
        "passed": passed,
        "status": status,
        "solution": solution.tolist(),
        "coordinate_error": coordinate_error,
        "objective": objective,
        "constraint_violation": violation,
        "cyipopt_version": cyipopt.__version__,
        "loaded_modules": loaded_modules(),
    }


def dll_probe(mode: str) -> dict[str, Any]:
    import cyipopt
    import numpy
    import scipy

    modules = loaded_modules()
    prefix = Path(sys.executable).resolve().parent
    numerical_tokens = ("cyipopt", "ipopt", "mumps", "openblas", "blas", "lapack", "gfortran", "libomp", "iomp")
    numerical = [path for path in modules if any(token in Path(path).name.lower() for token in numerical_tokens)]
    external = [path for path in numerical if not Path(path).resolve().is_relative_to(prefix)]
    contaminated = [path for path in modules if any(token in Path(path).name.lower() for token in ("mkl", "mkl_intel_thread", "libiomp5md"))]
    passed = not external and not contaminated
    return {
        **base(mode),
        "classification": "STAGE1B6J_R3_DLL_RESOLUTION_PASS" if passed else "STAGE1B6J_R3_DLL_RESOLUTION_FAILURE",
        "passed": passed,
        "prefix": str(prefix),
        "numpy_version": numpy.__version__,
        "scipy_version": scipy.__version__,
        "cyipopt_version": cyipopt.__version__,
        "loaded_modules": modules,
        "numerical_modules": numerical,
        "external_numerical_modules": external,
        "mkl_or_intel_thread_modules": contaminated,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("import", "blas", "ipopt", "dll"), required=True)
    parser.add_argument("--mode", choices=("raw", "activated", "relocated", "local_raw", "local_activated"), required=True)
    parser.add_argument("--module", default="numpy")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.kind == "import":
        result = import_probe(args.module, args.project_root.resolve() if args.project_root else None, args.mode)
    elif args.kind == "blas":
        result = blas_probe(args.mode)
    elif args.kind == "ipopt":
        result = ipopt_probe(args.mode)
    else:
        result = dll_probe(args.mode)
    write_json(args.output, result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

