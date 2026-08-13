PHASE 4C STAGE 1B.6F OFFLINE TARGET RECOVERY

This bundle is the isolated Windows x86-64 benchmark runtime for the canonical
lambda=0.04 retained-checkpoint reproduction and the later Stage 1B.6F Ipopt
solver-family benchmark.

Verified numerical core contract:
- Python 3.12.13
- NumPy 2.5.2
- SciPy 1.18.0
- cyipopt 1.7.0
- Ipopt 3.14.19
- mumps-seq / MUMPS 5.8.2

Canonical-runtime additions from the audited repository dependency closure:
- pandas
- pyarrow
- pyyaml (import name: yaml)
- pyproj
- pytest (runner/focused-test dependency)

Dependency-audit manifest authority:
phase4c_stage1b6f_benchmark_runtime_dependency_manifest.json
SHA-256:
a83b8013db1525904ca743a5858b028038d25c0230ddb696c8eefbb0f498daff

The builder must fail closed unless the exact numerical-core versions resolve,
all five audited runtime imports work from the isolated prefix, the Ipopt smoke
test passes, the packed environment relocates into a new prefix, the runtime
import gate passes again after relocation, and the relocated Ipopt smoke test
passes.

1. Download the GitHub Actions artifact named:
   phase4c-stage1b6f-ipopt-env-bundle

2. Extract the GitHub Actions wrapper artifact. Verify the actual transfer ZIP
   against its companion file:
   phase4c_stage1b6f_ipopt_env_bundle.zip.sha256.txt

3. Place the verified transfer ZIP on the locked MH370 Windows machine under:
   tools/ipopt_bootstrap_inbox/

4. Rerun ONLY:
   PHASE 4C — STAGE 1B.6F-BOOTSTRAP-RECOVERY

5. The target recovery stage must verify the outer and inner hashes, extract
   the packed environment project-locally into tools/ipopt_benchmark_env/,
   perform the conda-pack relocation/fixup (including conda-unpack), confirm
   that the numerical core and pandas/pyarrow/pyyaml/pyproj/pytest all resolve
   from the recovered prefix, import cyipopt, rerun smoke_test.py, and reproduce
   the canonical lambda=0.04 retained checkpoint within the already declared
   repository reproduction tolerances.

6. Do NOT run Ipopt MODE A, MODE B, or MODE C and do NOT begin any MH370
   optimization unless the target recovery stage independently establishes:
   STAGE1B6F_READY_TO_RESUME = YES
