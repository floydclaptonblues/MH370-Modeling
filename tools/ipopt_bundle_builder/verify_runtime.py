from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import traceback
from pathlib import Path
from typing import Any

import cyipopt
import numpy as np
import pandas as pd
import pyarrow as pa
import pyproj
import pytest
import scipy
import yaml


DEPENDENCY_AUDIT_MANIFEST_SHA256 = "a83b8013db1525904ca743a5858b028038d25c0230ddb696c8eefbb0f498daff"
EXPECTED_CORE = {
    "python": "3.12.13",
    "numpy": "2.5.2",
    "scipy": "1.18.0",
    "cyipopt": "1.7.0",
    "ipopt": "3.14.19",
    "mumps-seq": "5.8.2",
}
REQUIRED_RUNTIME_IMPORTS = ("pandas", "pyarrow", "pyproj", "yaml", "pytest")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def package_version(packages: list[dict[str, Any]], name: str) -> str:
    for row in packages:
        if str(row.get("name", "")).lower() == name.lower():
            return str(row.get("version", "NOT_EXPOSED"))
    return "NOT_EXPOSED"


def ipopt_version() -> str:
    value = getattr(cyipopt, "IPOPT_VERSION", None)
    if isinstance(value, (tuple, list)):
        return ".".join(str(part) for part in value)
    if value is None:
        return "NOT_EXPOSED"
    return str(value)


def module_path(module: Any) -> str:
    value = getattr(module, "__file__", None)
    return str(Path(value).resolve()) if value else "NOT_EXPOSED"


def path_inside(path_text: str, prefix: Path) -> bool:
    if path_text == "NOT_EXPOSED":
        return False
    path = Path(path_text).resolve()
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conda-list", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()

    prefix = Path(sys.prefix).resolve()
    packages = json.loads(args.conda_list.read_text(encoding="utf-8"))

    try:
        observed_core = {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "cyipopt": getattr(cyipopt, "__version__", package_version(packages, "cyipopt")),
            "ipopt": ipopt_version(),
            "mumps-seq": package_version(packages, "mumps-seq"),
        }
        mismatches = {
            name: {"expected": expected, "observed": observed_core.get(name)}
            for name, expected in EXPECTED_CORE.items()
            if observed_core.get(name) != expected
        }

        runtime_versions = {
            "pandas": pd.__version__,
            "pyarrow": pa.__version__,
            "pyproj": pyproj.__version__,
            "yaml": getattr(yaml, "__version__", package_version(packages, "pyyaml")),
            "pytest": pytest.__version__,
        }
        runtime_package_versions = {
            "pandas": package_version(packages, "pandas"),
            "pyarrow": package_version(packages, "pyarrow"),
            "pyproj": package_version(packages, "pyproj"),
            "pyyaml": package_version(packages, "pyyaml"),
            "pytest": package_version(packages, "pytest"),
        }
        missing_packages = [name for name, version in runtime_package_versions.items() if version == "NOT_EXPOSED"]

        module_paths = {
            "numpy": module_path(np),
            "scipy": module_path(scipy),
            "cyipopt": module_path(cyipopt),
            "pandas": module_path(pd),
            "pyarrow": module_path(pa),
            "pyproj": module_path(pyproj),
            "yaml": module_path(yaml),
            "pytest": module_path(pytest),
        }
        paths_inside_prefix = {name: path_inside(path, prefix) for name, path in module_paths.items()}
        executable_inside_prefix = path_inside(str(Path(sys.executable).resolve()), prefix)

        # Exercise the audited runtime imports, not merely import their top-level modules.
        dataframe_probe = pd.DataFrame({"x": [1, 2]}).shape == (2, 1)
        arrow_probe = pa.table({"x": [1, 2]}).num_rows == 2
        geod_distance = float(pyproj.Geod(ellps="WGS84").inv(0.0, 0.0, 1.0, 1.0)[2])
        geod_probe = math.isfinite(geod_distance) and geod_distance > 0.0
        yaml_probe = yaml.safe_load("stage: 1") == {"stage": 1}
        pytest_probe = bool(pytest.__version__)
        runtime_probes = {
            "pandas_dataframe": dataframe_probe,
            "pyarrow_table": arrow_probe,
            "pyproj_geod": geod_probe,
            "pyyaml_safe_load": yaml_probe,
            "pytest_import": pytest_probe,
        }

        passed = bool(
            not mismatches
            and not missing_packages
            and executable_inside_prefix
            and all(paths_inside_prefix.values())
            and all(runtime_probes.values())
        )
        result = {
            "classification": "MH370_BENCHMARK_RUNTIME_DEPENDENCY_PASS" if passed else "MH370_BENCHMARK_RUNTIME_DEPENDENCY_FAILURE",
            "dependency_audit_manifest_sha256": DEPENDENCY_AUDIT_MANIFEST_SHA256,
            "environment_prefix": str(prefix),
            "python_executable": str(Path(sys.executable).resolve()),
            "python_executable_inside_prefix": executable_inside_prefix,
            "expected_core_versions": EXPECTED_CORE,
            "observed_core_versions": observed_core,
            "core_version_mismatches": mismatches,
            "required_runtime_imports": list(REQUIRED_RUNTIME_IMPORTS),
            "runtime_import_versions": runtime_versions,
            "runtime_conda_package_versions": runtime_package_versions,
            "missing_runtime_conda_packages": missing_packages,
            "module_paths": module_paths,
            "module_paths_inside_prefix": paths_inside_prefix,
            "runtime_functionality_probes": runtime_probes,
        }
        write_json(args.result, result)
        return 0 if passed else 1
    except Exception as exc:
        write_json(
            args.result,
            {
                "classification": "MH370_BENCHMARK_RUNTIME_DEPENDENCY_FAILURE",
                "dependency_audit_manifest_sha256": DEPENDENCY_AUDIT_MANIFEST_SHA256,
                "environment_prefix": str(prefix),
                "python_executable": str(Path(sys.executable).resolve()),
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
