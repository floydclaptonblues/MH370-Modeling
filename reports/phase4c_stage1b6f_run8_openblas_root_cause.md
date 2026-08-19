# Phase 4C Stage 1B.6F Run 8 OpenBLAS root-cause audit

Classification: **ROOT_CAUSE_SPECIFICATION**
Specific cause: **ROOT_CAUSE_OPENBLAS_NOT_EXPLICITLY_CONSTRAINED**

Run 8 was built by workflow run `31669710941` at commit
`e4e355d6eb4bd43c66fd314094ee9201dfb88a34`. The authoritative builder
requested exact versions for Python, NumPy, SciPy, cyipopt, Ipopt, and
MUMPS, but it did not request a BLAS/LAPACK variant or `libopenblas`.
Strict conda-forge channel policy therefore did not select OpenBLAS by itself.

| | REQUESTED | PLANNED | INSTALLED | LOADED |
|---|---|---|---|---|
| BLAS backend | Unspecified | MKL | MKL | MKL |
| libblas | Unspecified | 3.11.0 `9_h8455456_mkl` | same | `libblas.dll` → MKL |
| libcblas | Unspecified | 3.11.0 `9_h2a3cdd5_mkl` | same | `libcblas.dll` → MKL |
| liblapack | Unspecified | 3.11.0 `9_hf9ab0e9_mkl` | same | `liblapack.dll` → MKL |
| libopenblas | Unspecified | Absent | Absent | Absent |
| MKL | Unspecified | 2026.1.0 `hac47afa_234` | same | `mkl_rt.3.dll`, `mkl_core.3.dll`, `mkl_intel_thread.3.dll` |

The Run 8 transaction output planned MKL, its Conda inventory and package
receipts installed MKL, its bundle included the MKL-backed BLAS/LAPACK files,
and the recovered prefix loaded those project-local MKL DLLs. Recovery did not
contaminate an OpenBLAS bundle; the bundle was MKL-backed before recovery.

The successor is parallel and non-mutating. It adds exact OpenBLAS variant
constraints, rejects MKL in the plan and receipts, compares two independent dry
solves, requires exact planned-versus-installed explicit package URLs, checks
the OpenBLAS backend receipts, and requires a project-local `openblas.dll` at
runtime with no loaded MKL DLL. Its probe does not construct or solve an Ipopt
problem and does not import or execute the MH370 model.
