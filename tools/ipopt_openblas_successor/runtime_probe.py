from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    count = min(needed.value // ctypes.sizeof(wintypes.HMODULE), len(handles))
    paths: list[str] = []
    for handle in handles[:count]:
        buffer = ctypes.create_unicode_buffer(32768)
        if get_module_filename(process, handle, buffer, len(buffer)):
            paths.append(str(Path(buffer.value).resolve()))
    return sorted(set(paths), key=str.lower)


def inside(path: str, prefix: Path) -> bool:
    return Path(path).resolve().is_relative_to(prefix.resolve())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", required=True)
    args = parser.parse_args()

    prefix = Path(sys.executable).resolve().parent
    library_bin = prefix / "Library" / "bin"
    if os.name == "nt":
        os.add_dll_directory(str(library_bin))

    ipopt_dll = library_bin / "ipopt-3.dll"
    mumps_dll = library_bin / "dmumps.dll"
    if not ipopt_dll.is_file() or not mumps_dll.is_file():
        raise FileNotFoundError("Project-local Ipopt or MUMPS DLL is missing")
    ctypes.WinDLL(str(mumps_dll))
    ctypes.WinDLL(str(ipopt_dll))

    import cyipopt
    import numpy as np
    import scipy
    import scipy.linalg

    left = np.arange(4096, dtype=np.float64).reshape(64, 64) / 4096.0
    product = left @ left.T
    matrix = product + np.eye(64, dtype=np.float64) * 4.0
    solution = scipy.linalg.solve(matrix, np.ones(64, dtype=np.float64), assume_a="pos")
    if not np.all(np.isfinite(solution)):
        raise RuntimeError("BLAS/LAPACK numerical probe produced non-finite values")

    numpy_config_stream = io.StringIO()
    scipy_config_stream = io.StringIO()
    with contextlib.redirect_stdout(numpy_config_stream):
        np.show_config()
    with contextlib.redirect_stdout(scipy_config_stream):
        scipy.show_config()

    modules = loaded_modules()
    openblas = [path for path in modules if "openblas" in Path(path).name.lower()]
    mkl = [path for path in modules if Path(path).name.lower().startswith("mkl")]
    numerical_tokens = ("openblas", "blas", "lapack", "ipopt", "mumps", "cyipopt", "gfortran")
    numerical = [path for path in modules if any(token in Path(path).name.lower() for token in numerical_tokens)]
    external = [path for path in numerical if not inside(path, prefix)]

    module_paths = {
        "numpy": str(Path(np.__file__).resolve()),
        "scipy": str(Path(scipy.__file__).resolve()),
        "cyipopt": str(Path(cyipopt.__file__).resolve()),
    }
    paths_inside = {name: inside(path, prefix) for name, path in module_paths.items()}
    passed = bool(openblas) and not mkl and not external and all(paths_inside.values())
    result = {
        "classification": (
            "STAGE1B6F_OPENBLAS_SUCCESSOR_RUNTIME_PASS"
            if passed
            else "STAGE1B6F_OPENBLAS_SUCCESSOR_RUNTIME_LINKAGE_REJECTED"
        ),
        "passed": passed,
        "mode": args.mode,
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "prefix": str(prefix),
        "module_versions": {
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "cyipopt": cyipopt.__version__,
            "ipopt": ".".join(str(value) for value in cyipopt.IPOPT_VERSION),
        },
        "module_paths": module_paths,
        "module_paths_inside_prefix": paths_inside,
        "ipopt_dll": str(ipopt_dll.resolve()),
        "mumps_dll": str(mumps_dll.resolve()),
        "openblas_dlls": openblas,
        "mkl_dlls": mkl,
        "external_numerical_modules": external,
        "numerical_modules": numerical,
        "numpy_show_config": numpy_config_stream.getvalue(),
        "scipy_show_config": scipy_config_stream.getvalue(),
        "solve_residual_infinity": float(np.linalg.norm(matrix @ solution - np.ones(64), ord=np.inf)),
        "optimization_executed": False,
        "mh370_model_imported_or_executed": False,
    }
    write_json(args.output, result)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
