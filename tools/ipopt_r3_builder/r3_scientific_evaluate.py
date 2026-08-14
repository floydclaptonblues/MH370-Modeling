from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
from pathlib import Path
from typing import Any

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[name] = "1"

import numpy as np


REFERENCE_SCALARS = {
    "S0": {"state_sha256": "07fde7ae10ec39bd6c348f0e4bcc2eca13f568546e77f3bde38de1c7addbd9c5", "objective": 24.576849498224718, "canonical_kkt": 0.021664807982674006},
    "WARM_R_0.00015": {"state_sha256": "236f70bf225d6363928d69c59fe2d248f9f222ccbf68cb6bae6bea2d88a85914", "objective": 24.57684776240326, "canonical_kkt": 0.009419975704943129},
    "THRESHOLD": {"state_sha256": "9497806504de015d986a947916ad3ad41d9beba4ca0cced486a5cc78cc084ece", "objective": 24.576847945952245, "canonical_kkt": 0.009997800935424572},
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def array_hash(value: Any) -> str:
    array = np.ascontiguousarray(np.asarray(value, dtype=np.float64))
    return hashlib.sha256(array.tobytes()).hexdigest()


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite scientific output")
    return value


def load_states(root: Path) -> tuple[Any, dict[str, np.ndarray], dict[str, Any]]:
    for candidate in (root, root / "src"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    from mh370_endpoint.config import load_mapping
    from mh370_endpoint.joint_flight.stage1b6i_confirmation import recover_threshold_reference, recover_warm_start
    from scripts.run_phase4c_stage1b6e1 import load_problem

    config = load_mapping(root / "configs/phase4c_stage1b6e1.yaml")
    objective, retained, _baseline, _layout = load_problem(root, config)
    warm, _warm_row = recover_warm_start(root / "results/phase4c_stage1b6h/fixed_ray_exact_probes.parquet")
    threshold, _threshold_row = recover_threshold_reference(root / "results/phase4c_stage1b6h/threshold_candidate.json")
    prior = load_mapping(root / "configs/phase4c_stage1b6e.yaml")
    return objective, {"S0": retained, "WARM_R_0.00015": warm, "THRESHOLD": threshold}, prior


def evaluate(root: Path) -> dict[str, Any]:
    objective, states, prior = load_states(root)
    from mh370_endpoint.joint_flight.stage1b6f_mode_a import canonical_state_record
    constraints = prior["constraints"]
    output: dict[str, Any] = {}
    for state_id, point in states.items():
        record, details = canonical_state_record(
            objective,
            point,
            state_id=state_id,
            nonlinear_lower_bound=float(constraints["nonlinear_lower_bound"]),
            active_tolerance=float(constraints["active_margin_tolerance"]),
            near_active_tolerance=float(constraints["near_active_margin_tolerance"]),
            constraint_jacobian_relative_step=float(constraints["finite_difference_relative_step"]),
        )
        evaluation = details["evaluation"]
        endpoint_numeric = {
            key: float(value) for key, value in details["endpoint"].items()
            if isinstance(value, (int, float, np.number))
        }
        arrays = {
            "reduced_state": np.asarray(point, dtype=np.float64),
            "physical_state": np.asarray(details["physical"], dtype=np.float64),
            "residual_vector": np.asarray(evaluation["residual"], dtype=np.float64),
            "residual_jacobian": np.asarray(evaluation["jacobian"].toarray(), dtype=np.float64),
            "objective_gradient": np.asarray(evaluation["gradient"], dtype=np.float64),
            "constraints": np.asarray(details["constraints"], dtype=np.float64),
            "constraint_jacobian": np.asarray(details["constraint_jacobian"], dtype=np.float64),
        }
        output[state_id] = {
            "record": json_safe(record),
            "arrays": {name: value.tolist() for name, value in arrays.items()},
            "array_hashes": {name: array_hash(value) for name, value in arrays.items()},
            "endpoint": endpoint_numeric,
        }
    return {
        "schema_version": "1",
        "evaluation_only": True,
        "optimization_executed": False,
        "state_count": len(output),
        "python": platform.python_version(),
        "executable": str(Path(sys.executable).resolve()),
        "states": output,
    }


def maximum_differences(observed: Any, reference: Any) -> tuple[float, float]:
    left = np.asarray(observed, dtype=np.float64)
    right = np.asarray(reference, dtype=np.float64)
    if left.shape != right.shape:
        return math.inf, math.inf
    absolute = np.abs(left - right)
    relative = absolute / np.maximum(np.abs(right), np.finfo(np.float64).tiny)
    return float(np.max(absolute, initial=0.0)), float(np.max(relative, initial=0.0))


def limit(tolerances: dict[str, Any], key: str, reference: Any) -> float:
    absolute = float(tolerances["absolute_tolerances"][key])
    relative = float(tolerances["relative_tolerances"].get(key, 0.0))
    magnitude = float(np.max(np.abs(np.asarray(reference, dtype=np.float64)), initial=0.0))
    return absolute + relative * magnitude


def compare(root: Path, observed: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    from mh370_endpoint.joint_flight.stage1b4b import endpoint_distance_m

    tolerances = json.loads((root / "provenance/phase4c_stage1b6f_bootstrap_tolerances.json").read_text(encoding="utf-8"))
    if tolerances.get("post_result_relaxation_permitted") is not False:
        raise RuntimeError("STAGE1B6J_R3_REFERENCE_TOLERANCE_UNRESOLVED")
    key_map = {
        "reduced_state": "reduced_state_max",
        "physical_state": "physical_state_max",
        "residual_vector": "residual_vector_max",
        "objective_gradient": "gradient_max",
        "constraints": "constraints_max",
        "constraint_jacobian": "constraint_jacobian_max",
    }
    state_results = {}
    global_abs = 0.0
    global_rel = 0.0
    for state_id, expected_scalar in REFERENCE_SCALARS.items():
        actual = observed["states"][state_id]
        baseline = reference["states"][state_id]
        metrics: dict[str, Any] = {}
        for name, key in key_map.items():
            absolute, relative = maximum_differences(actual["arrays"][name], baseline["arrays"][name])
            threshold = limit(tolerances, key, baseline["arrays"][name])
            metrics[name] = {"maximum_absolute_difference": absolute, "maximum_relative_difference": relative, "authorized_limit": threshold, "passed": absolute <= threshold}
            global_abs, global_rel = max(global_abs, absolute), max(global_rel, relative)
        residual_jacobian_abs, residual_jacobian_rel = maximum_differences(actual["arrays"]["residual_jacobian"], baseline["arrays"]["residual_jacobian"])
        residual_jacobian_hash_equal = actual["array_hashes"]["residual_jacobian"] == baseline["array_hashes"]["residual_jacobian"]
        metrics["residual_jacobian"] = {
            "maximum_absolute_difference": residual_jacobian_abs,
            "maximum_relative_difference": residual_jacobian_rel,
            "authorized_limit": 0.0,
            "basis": "no separate pre-existing residual-Jacobian tolerance; require exact protected hash",
            "passed": residual_jacobian_hash_equal,
        }
        endpoint_distance = float(endpoint_distance_m(baseline["endpoint"], actual["endpoint"]))
        endpoint_limit = float(tolerances["absolute_tolerances"]["endpoint_displacement_m"])
        metrics["endpoint"] = {"distance_m": endpoint_distance, "authorized_limit_m": endpoint_limit, "passed": endpoint_distance <= endpoint_limit}
        scalar_checks = {}
        for scalar, tolerance_key in (("objective", "objective"), ("canonical_kkt", "canonical_kkt")):
            observed_value = float(actual["record"][scalar])
            reference_value = float(expected_scalar[scalar])
            difference = abs(observed_value - reference_value)
            authorized = float(tolerances["absolute_tolerances"][tolerance_key]) + float(tolerances["relative_tolerances"][tolerance_key]) * abs(reference_value)
            scalar_checks[scalar] = {"observed": observed_value, "reference": reference_value, "absolute_difference": difference, "authorized_limit": authorized, "passed": difference <= authorized}
            global_abs = max(global_abs, difference)
            global_rel = max(global_rel, difference / max(abs(reference_value), np.finfo(np.float64).tiny))
        identity = actual["record"]["state_sha256"] == expected_scalar["state_sha256"] == baseline["record"]["state_sha256"]
        passed = identity and all(item["passed"] for item in metrics.values()) and all(item["passed"] for item in scalar_checks.values())
        state_results[state_id] = {"passed": passed, "state_identity_passed": identity, "metrics": metrics, "scalars": scalar_checks}
    passed = all(row["passed"] for row in state_results.values())
    return {
        "classification": "STAGE1B6J_R3_SCIENTIFIC_EQUIVALENCE_PASS" if passed else "STAGE1B6J_R3_SCIENTIFIC_EQUIVALENCE_FAILURE",
        "passed": passed,
        "evaluation_only": True,
        "optimization_executed": False,
        "tolerances": tolerances,
        "states": state_results,
        "maximum_absolute_difference": global_abs,
        "maximum_relative_difference": global_rel,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-reference", action="store_true")
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    observed = evaluate(root)
    if args.capture_reference:
        observed["classification"] = "STAGE1B6J_R3_FROZEN_REFERENCE_ARRAYS_CAPTURED"
        write_json(args.output, observed)
        return 0
    if args.reference is None:
        parser.error("--reference is required unless --capture-reference is used")
    result = compare(root, observed, json.loads(args.reference.read_text(encoding="utf-8")))
    result["observed"] = observed
    write_json(args.output, result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
