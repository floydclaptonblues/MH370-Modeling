from __future__ import annotations

import argparse
import json
import platform
import sys
import traceback
from pathlib import Path
from typing import Any

import cyipopt
import numpy as np
import scipy


EXPECTED = np.array([1.0, 2.0], dtype=float)
SOLUTION_TOLERANCE = 1.0e-7
CONSTRAINT_TOLERANCE = 1.0e-8


def json_safe(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_safe(value), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


class SmokeProblem:
    def __init__(self) -> None:
        self.intermediate_calls = 0
        self.last_iteration = None

    def objective(self, x: np.ndarray) -> float:
        return float((x[0] - 1.0) ** 2 + (x[1] - 2.0) ** 2)

    def gradient(self, x: np.ndarray) -> np.ndarray:
        return np.array([2.0 * (x[0] - 1.0), 2.0 * (x[1] - 2.0)], dtype=float)

    def constraints(self, x: np.ndarray) -> np.ndarray:
        return np.array([x[0] + x[1]], dtype=float)

    def jacobian(self, _x: np.ndarray) -> np.ndarray:
        return np.array([1.0, 1.0], dtype=float)

    def intermediate(
        self,
        _algorithm_mode: int,
        iteration_count: int,
        _objective_value: float,
        _primal_infeasibility: float,
        _dual_infeasibility: float,
        _barrier_parameter: float,
        _step_norm: float,
        _regularization_size: float,
        _dual_step_size: float,
        _primal_step_size: float,
        _line_search_trials: int,
    ) -> bool:
        self.intermediate_calls += 1
        self.last_iteration = int(iteration_count)
        return True


def ipopt_version() -> str:
    value = getattr(cyipopt, "IPOPT_VERSION", None)
    if value is None:
        return "NOT_EXPOSED"
    if isinstance(value, (tuple, list)):
        return ".".join(str(part) for part in value)
    return str(value)


def run(result_path: Path) -> int:
    definition = {
        "objective": "(x-1)^2 + (y-2)^2",
        "constraint": "x+y >= 3",
        "bounds": [[-10.0, 10.0], [-10.0, 10.0]],
        "starting_point": [0.0, 0.0],
        "analytic_solution": EXPECTED.tolist(),
        "analytic_objective": 0.0,
        "hessian_mode": "limited-memory",
        "maximum_coordinate_error_tolerance": SOLUTION_TOLERANCE,
        "constraint_violation_tolerance": CONSTRAINT_TOLERANCE,
    }
    base = {
        "definition": definition,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
        "cyipopt_version": getattr(cyipopt, "__version__", "NOT_EXPOSED"),
        "cyipopt_module_path": str(Path(cyipopt.__file__).resolve()),
        "ipopt_version": ipopt_version(),
    }
    try:
        model = SmokeProblem()
        problem = cyipopt.Problem(
            n=2,
            m=1,
            problem_obj=model,
            lb=np.array([-10.0, -10.0], dtype=float),
            ub=np.array([10.0, 10.0], dtype=float),
            cl=np.array([3.0], dtype=float),
            cu=np.array([1.0e19], dtype=float),
        )
        problem.add_option("hessian_approximation", "limited-memory")
        problem.add_option("tol", 1.0e-10)
        problem.add_option("constr_viol_tol", 1.0e-10)
        problem.add_option("acceptable_tol", 1.0e-10)
        problem.add_option("max_iter", 200)
        problem.add_option("print_level", 5)
        solution, info = problem.solve(np.array([0.0, 0.0], dtype=float))
        solution = np.asarray(solution, dtype=float)
        coordinate_error = float(np.max(np.abs(solution - EXPECTED)))
        constraint_value = float(solution[0] + solution[1])
        constraint_violation = float(max(0.0, 3.0 - constraint_value))
        objective = float(model.objective(solution))
        status = int(info.get("status", -999))
        normal_termination = status in (0, 1)
        passed = bool(
            normal_termination
            and coordinate_error <= SOLUTION_TOLERANCE
            and constraint_violation <= CONSTRAINT_TOLERANCE
        )
        result = {
            **base,
            "classification": "IPOPT_EXTERNAL_SMOKE_TEST_PASS" if passed else "IPOPT_EXTERNAL_SMOKE_TEST_FAILURE",
            "termination_status_code": status,
            "termination_status": json_safe(info.get("status_msg", "NOT_EXPOSED")),
            "iteration_count": model.last_iteration,
            "intermediate_callback_calls": model.intermediate_calls,
            "computed_solution": solution.tolist(),
            "maximum_coordinate_error": coordinate_error,
            "objective": objective,
            "constraint_value": constraint_value,
            "constraint_residual": constraint_value - 3.0,
            "constraint_violation": constraint_violation,
            "solver_info": json_safe(info),
        }
        write_json(result_path, result)
        return 0 if passed else 1
    except Exception as exception:
        write_json(
            result_path,
            {
                **base,
                "classification": "IPOPT_EXTERNAL_SMOKE_TEST_FAILURE",
                "termination_status": "EXCEPTION",
                "exception_type": type(exception).__name__,
                "exception": str(exception),
                "traceback": traceback.format_exc(),
            },
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True, type=Path)
    arguments = parser.parse_args()
    return run(arguments.result.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
