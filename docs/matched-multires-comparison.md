# Compare frozen reconstruction runs

`tools/compare_multires_runs.py` compares saved controller results without
conversion, CAD access or new acceptance decisions. For example:

```powershell
python tools/compare_multires_runs.py `
  benchmarks/multires-1000/experiments/controller-v2/c84-inner.json `
  benchmarks/multires-1000/experiments/controller-v2/v41-inner.json `
  --manifest benchmarks/multires-1000/controller-cohorts/target-controls-three.json `
  --output benchmarks/multires-1000/experiments/controller-v2/new-matched-report.json
```

Adjacent `.contract.json` files are required by default; explicit contract paths
can be supplied. The manifest's actual SHA-256 must match both contracts. Each
result fingerprint must reproduce from its exact resolved fixture/settings and
recorded run identity. Grader, tool sources, native runtimes/dependencies,
platform, process limits, profile and deadlines must match. Only converter
revision/manifest and its containing live-lock catalog identity may differ;
the verification policy itself must remain identical.

Missing, duplicate, unscored, unready, foreign-input or mismatched-contract
records refuse comparison. A partially completed large run cannot silently
become a passing intersection: freeze an explicit smaller cohort and finish it.
All ten variants and every member of a geometry family must retain their split.

Reports preserve original strict booleans and list gains and lost successes.
They include per-case observations, source-model/preset groups, existing failure
categories, deviations, dimensions, volume, fallback facets, topology checks,
crashes/timeouts, latency and sampled process-tree RSS. Missing observations
remain absent rather than zero. All-ten source success stays null until ten
distinct presets have been scored. Completing a three-case cohort does not
establish the full 1,000-source / 10,000-case baseline.

Reused timings remain historical and are separately labelled. Matched fresh
performance distributions use only pairs where both executions were fresh;
they never treat cache lookup time as reconstruction time. One measured cohort
does not establish a broad latency improvement. Report metadata uses hashes;
paths/free-text errors are omitted and human identifiers are anonymized. Existing
content-addressed IDs remain readable. Source records and reports are never
overwritten.

This is evidence postprocessing, not an independent cryptographic attestation,
native isolation recheck or permission to promote. The integration owner still
reviews regressions, visual fidelity and broader qualification before promotion.

```powershell
python -m pytest tests/test_compare_multires_runs.py -q
python -m ruff check tools/compare_multires_runs.py tests/test_compare_multires_runs.py
```
