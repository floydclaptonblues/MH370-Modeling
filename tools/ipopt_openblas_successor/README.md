# Stage 1B.6F OpenBLAS successor bundle

This builder creates a fresh Windows x86-64 Conda prefix for the Stage 1B.6F
runtime. It preserves the Run 8 numerical-core versions while explicitly
selecting the conda-forge OpenBLAS variants.

The build fails closed unless two independent dry solves agree, the package
plan contains OpenBLAS-backed `libblas`, `libcblas`, and `liblapack`, the plan
contains `libopenblas`, no MKL backend package is present, the installed
explicit package URLs match the plan exactly, all OpenBLAS backend receipts
exist, and live NumPy/SciPy execution loads `openblas.dll` from the fresh
prefix without loading an MKL DLL.

The runtime probe imports NumPy, SciPy, and cyipopt, loads project-local Ipopt
and MUMPS DLLs, and exercises BLAS/LAPACK only. It does not construct an Ipopt
problem, call `solve`, import the MH370 model, or run scientific optimization.

The immutable output is:

`phase4c_stage1b6f_openblas_successor_bundle.zip`

with a sibling SHA-256 companion file. Run 8 is not modified or overwritten.
