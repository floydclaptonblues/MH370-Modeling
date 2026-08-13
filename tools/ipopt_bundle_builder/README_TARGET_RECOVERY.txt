PHASE 4C STAGE 1B.6F OFFLINE TARGET RECOVERY

1. Download the GitHub Actions artifact named:
   phase4c-stage1b6f-ipopt-env-bundle

2. Verify the downloaded outer ZIP against:
   phase4c_stage1b6f_ipopt_env_bundle.zip.sha256.txt

3. Place the verified ZIP on the locked MH370 Windows machine at exactly:
   tools/ipopt_bootstrap_inbox/phase4c_stage1b6f_ipopt_env_bundle.zip

4. Rerun ONLY:
   PHASE 4C — STAGE 1B.6F-BOOTSTRAP-RECOVERY

5. The target recovery stage must verify the outer and inner hashes, extract
   the packed environment project-locally into tools/ipopt_benchmark_env/,
   perform the conda-pack relocation/fixup (including conda-unpack), import
   cyipopt, rerun smoke_test.py, and reproduce the canonical lambda=0.04
   retained checkpoint within the already predeclared tolerances.

6. Do NOT run Ipopt MODE A, MODE B, or MODE C and do NOT begin any MH370
   optimization unless the target recovery stage independently establishes:
   STAGE1B6F_READY_TO_RESUME = YES
