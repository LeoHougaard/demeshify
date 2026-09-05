# Reconstruction robustness work

Baseline: commit `3cc52bb`. The existing suite passed 133 tests during the
read-only review. Archived corpus results are 60/60 geometric and 41/60 clean
for curated Fusion, 53/60 and 35/60 for stress Fusion, and 22/22 and 1/22 for
Voron. The two Fusion sets overlap by 49 cases.

All testing, CAD execution, profiling, and builds for this implementation run
on Leo's laptop over SSH. The desktop is used for source inspection and edits.

## Plan and acceptance

1. Reproduce the three verification failures on the laptop using the baseline:
   wrong STEP with matching preview, missing small through-hole, and a dropped
   disconnected component.
2. Verify geometry from the imported STEP, protect local errors and components,
   and share acceptance between application and benchmark. All three regression
   tests must pass without weakening existing validity or error requirements.
3. Make reference comparisons and benchmark cache identity explicit. Missing
   reference geometry must not count as a clean reconstruction.
4. Bound native recovery and feature-history work, retain diagnostics, and
   measure the complete conversion runtime.
5. Evaluate local tolerance and coordinated boundary changes on known Voron
   failures and working periodic controls. Retain only verified improvements.
6. Add input topology diagnostics and run the full suite plus matched corpus
   checks. Record remaining failures and distinguish corrected measurement from
   improvements in reconstructed geometry.

## Iterations

- Laptop: `Leo@10.1.39.104`, hostname `LEO-SB`, SSH identity
  `~/.ssh/leo_laptop_ed25519`. Work is isolated under
  `C:\Users\Leo\stl-robustness-20260905`; the laptop's existing dirty checkout
  remains untouched. Environments use `uv sync --locked`.
- Baseline command in the remote `baseline` directory:
  `.venv\Scripts\python.exe -m pytest tests/test_verification_regressions.py --tb=short`.
  All three tests failed as expected in 54.55 seconds.
- First evaluator candidate: all three regressions and 26 existing surface and
  reconstruction tests passed. Full suite then passed 138 tests in 300.11 seconds.
  The tiny component is recovered through the explicitly labelled carrier.
- Native supervisor and preflight candidate: 25 focused tests passed, including
  API conversion, process-tree termination, and conservative mesh cleanup.
- Periodic boundaries: a reversed-order pair of equal-circumference cylinder
  boundaries failed to reuse canonical edges. Position matching alone did not
  fix it. Coordinating seam endpoint vertices and preserving curve/wire direction
  produced a valid face using both canonical edges. All 24 boundary/B-rep tests
  passed. The seven-case Voron diagnostic comparison remained at 7/7 geometric
  and 1/7 clean for both versions. The six difficult cases still use full faceted
  recovery. This fixes a demonstrated boundary defect but does not establish
  improved analytic coverage on those real models.
- Verification explicitly uses absolute OCCT tessellation deflection. Its
  large-cylinder deflection test and isolated feature-history edit checks passed
  in the next ten-test control run.
- Disjoint closed bodies now fit at their own scale. Both a 0.02 mm cube and a
  0.002 mm cube beside a 10 mm cube produce two valid solids with twelve planar
  faces and no faceted fallback. Nine component, boundary, and preflight controls
  passed. Overlapping bounding boxes retain the existing joint path so nested
  cavity shells are not treated as independent solids.
- The component candidate passed the full suite, 148 tests in 272.97 seconds.
  Subsequent fixes preserve original STL triangle indices after cleanup and
  simplify the benchmark worker/cache handling. Their focused runs passed
  22 tests and 13 tests respectively. Ruff passed on the final source snapshot.
  SHA-256 comparison confirmed all 43 Python files in the laptop's final
  snapshot match the desktop source.
- Five real Fusion controls produced identical classifications in baseline and
  candidate, 3/5 geometric and 2/5 clean, with two timeouts. This preserves the
  selected working cases but provides no evidence of a broader success-rate gain.

| Evaluation | Baseline | Candidate |
| --- | --- | --- |
| Three false-success regressions | 3 failed | 3 passed |
| Seven selected Voron models | 7 geometric, 1 clean | 7 geometric, 1 clean |
| Five selected Fusion models | 3 geometric, 2 clean, 2 timeouts | 3 geometric, 2 clean, 2 timeouts |

Fusion case outcomes match individually. `129428_edce5555_0` and
`147141_495ac591_0` time out; `27029_3fd2328f_8` returns faceted recovery;
`133589_40359674_2` and `71503_271b24c0_0` are clean. The first two inputs contain
76,256 and 30,192 triangles respectively.

## Evaluation and reproduction

The baseline geometry is from `3cc52bb`. The `evaluation-baseline` checkout uses
that geometry code with the stronger evaluator so comparison failures cannot
be hidden by the old preview-based checks. `component-candidate` contains the
geometry changes and the same absolute-deflection evaluator. `final-candidate`
also contains the later overlay-index and benchmark-cache changes covered by
the focused tests.

Remote commands run from each checkout:

```powershell
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$work = 'C:\Users\Leo\stl-robustness-20260905'
$python = "$work\candidate\.venv\Scripts\python.exe"
& $python -m pytest --tb=short
& $python -m ruff check .
& $python tools/benchmark_surface_corpus.py "$work\corpus\fusion-controls\manifest.json" `
  --output "$work\results\fusion-controls-REPLACE_WITH_CHECKOUT.json" `
  --case-timeout 240 --workers 1 --no-resume `
  --artifacts-root "$work\results\fusion-controls-REPLACE_WITH_CHECKOUT-artifacts"
```

The Voron selection contains CW2 guidler, latch, cable door, chain anchor,
LED diffuser, and PCB spacer, plus Tap R8 center-left. Its diagnostics precede
the final component-scale change. Full curated60, stress60, and Voron22 results
have not been re-established under the final stricter acceptance rules.

Test logs, benchmark JSON, failure ledgers, and the final source hashes are
copied to the ignored local `benchmarks/robustness_20260905/` directory. Full CAD
artifacts remain on the laptop under `results/*-artifacts/`.

## Remaining failures and next work

- Six selected Voron parts fit surfaces but fail to produce a valid analytic
  solid, even with zero reported free edges. Their preserved analytic STEP and
  report artifacts are the starting point for investigating wire orientation,
  parameter-space trimming, and shared boundaries on freeform faces. Fitting
  more surface types has not resolved this failure category.
- Two selected dense Fusion cases time out in both versions under the total
  240-second budget. Dense topology recovery and verification need profiling
  before changing budgets or mesh resolution.
- Verification checks component counts, not complete component correspondence.
  Local deviation is sampled with bounded vertex/triangle coverage, not a
  certified maximum distance. Narrow features on very dense meshes and
  nested/intersecting bodies remain useful adversarial cases.
- Mesh cleanup is deliberately limited. It does not repair self-intersections,
  close openings, or infer missing geometry.
- Process supervision covers API conversion and edits. Cancellation, durable
  job state, and transactional publication of all edit artifacts remain open.
  A process killed while publishing an edit can still leave mixed revisions.
- No broad clean-analytic success-rate improvement is claimed. The measured
  improvements are trustworthy rejection of the three false successes,
  recovery of very small separate bodies as analytic solids, canonical periodic
  boundary reuse, and bounded native execution.

The remote test environment fixes `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and
`MKL_NUM_THREADS` to `1`. Corpus cases run sequentially with a 240-second outer
deadline. Short diagnostic tests overlapped part of the initial seven-case
baseline, so those timings are not evidence of a performance improvement.
