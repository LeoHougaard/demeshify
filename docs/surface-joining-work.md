# Surface joining work

## Outcome and acceptance

Improve the fitted surface models that currently fall back to a complete
triangle-based STEP. Preserve the existing geometry gates, source components,
and the distinction between clean analytic and mixed fitted/faceted output.
Promote a change only when a previously failing real model retains fitted
surfaces in a valid, accurate STEP and representative working cases remain valid.

All CAD work and tests run on the laptop over SSH. The prior implementation is
preserved in `C:\Users\Leo\stl-joins-20260905\baseline`; experiments use the
adjacent `candidate` directory. The earlier workspaces remain untouched.

## Plan

1. Classify native validity failures in the saved diagnostic models.
2. Test a local repair against a demonstrated cause.
3. Measure actual exported STEP geometry, including local deviation and volume.
4. Integrate successful changes, run a matched real-model comparison and the
   relevant tests, and record any remaining failures.

## Evidence and attempts

- Re-sewing saved latch, cable door, and chain anchor fitted faces produces
  self-intersecting wires and unorientable faces. Zero free edges does not imply
  a valid face arrangement. Cable door also has a curve-on-surface mismatch.
- Guidler and LED diffuser contain invalid face orientations after STEP import.
  The diffuser has one faulty thin B-spline triangle among 529 faces. Its signed
  area is negative. Its vertex tolerance is about 0.026 mm, comparable to half
  the triangle's shortest edge length of 0.052 mm.
- Standard shape healing, recomputing tolerances, and volume construction without
  intersections did not fix the diffuser. Reversing the wire and face orientation
  produced a valid in-memory solid but failed another STEP roundtrip. Omitting
  exported parameter-space curves did not resolve it either.
- Refitting the defective face against its existing boundary at tighter precision
  produced a 529-face solid that survives STEP export/import and passes the
  unchanged geometry gates. The production fix instead uses each transition
  triangle's altitude to set its filling precision. A fresh conversion now keeps
  50 recognized planar patches and the local residual faces in one valid solid.
  Local maximum deviation is 0.00333 mm against a 0.12 mm limit; volume error is
  0.231%. It remains explicitly mixed fitted/faceted output.
- Three controlled thin-transition regressions fail against the baseline and
  pass with local filling precision. The baseline drops the 0.01 and 0.02 mm
  wide faces, and misses the curved boundary of the 0.05 mm face by 0.000295 mm.
  The candidate preserves all three; boundary deviations are at most 0.00000559 mm.
- The clipped cone in Fusion case `27029_3fd2328f_8` exposes a second cause.
  A wire built from 3D intersection branches encloses the wrong part of an
  otherwise accurately fitted cone. Prefer the mesh boundary projected into
  cone parameter space for partial cone patches. Repair those already exact
  surface segments at kernel precision, not whole-part fitting tolerance.
  Tightening UV repair alone did not help because the earlier intersection-based
  candidate was still selected. Choosing the intended UV region is necessary.
- The combined changes pass the normal Fusion benchmark on that case and the
  two selected working controls. Clean analytic results increase from 2/3 to
  3/3. The recovered case retains 8 planes, 3 cylinders, and 1 cone, with no
  residual faces or gap caps. Local maximum deviation is 0.00205 mm and volume
  error is 0.00161%. The reference surface and circular-edge checks also pass.
- All 27 focused joining/B-rep controls pass, and Ruff passes. The broader
  selected Voron evaluation is complete. All ten selected real models pass
  geometric verification. The full suite passes 153 tests in 276.62 seconds,
  with one existing Starlette test-client deprecation warning. Final Ruff
  checks pass. All 44 Python files match the laptop's tested source by SHA-256.

The two changes meet the promotion gate and are retained in the working tree.
No acceptance limits were loosened, and no reference STEP geometry is used by
the reconstruction algorithms.

## Real-model results

| Selected models | Previous output | New output |
| --- | --- | --- |
| Three Fusion controls | 2 clean, 1 full faceted fallback | 3 clean |
| Seven Voron controls | 1 clean, 6 full faceted fallbacks | 1 clean, 1 mixed fitted model, 5 full faceted fallbacks |

All previously working results retain their geometric and clean-analytic
acceptance. The three Fusion controls are `27029_3fd2328f_8`,
`133589_40359674_2`, and `71503_271b24c0_0`. The seven Voron controls are the
same guidler, latch, spacer, cable door, chain anchor, LED diffuser, and Tap
center-left cases from the preceding robustness review.

The diffuser received a fresh baseline/candidate comparison in this run. The
other previous outcomes are recorded in the preceding review's selected-case
reports. The frozen `baseline` directory contains the working tree after those
robustness changes. Timings are not used to claim a speed improvement; short
diagnostics and rendering overlapped some evaluation work. The two dense Fusion
timeouts and the full larger corpora were not rerun in this iteration.

The recovered Fusion STEP has 12 faces and is about 967 KB, versus 2,162 faces
and 5 MB in the previous faceted output. Its minimum analytic-area recall is
98.53% and circular-edge-length recall is 92.96%, both above the unchanged
benchmark requirements. The faces retain analytic supports, but some trim
boundaries still follow the mesh samples. This does not recreate a feature tree.

The actual previous output, new output, and reference CAD were rendered on the
laptop from the same direction and visually inspected. The overall shape and
cutout placement agree. The numerical checks provide the accuracy evidence.

## Remaining failures

Guidler, latch, cable door, chain anchor, and Tap center-left still need full
faceted fallback. Their remaining defects include self-intersecting wires and
inconsistent shared boundaries after sewing. The two fixes here resolve thin
transition fitting and a wrong clipped-cone region; they do not resolve every
freeform boundary arrangement.

## Reproduction and artifacts

The laptop workspace is `C:\Users\Leo\stl-joins-20260905`. `combined` contains
both changes. Use the existing locked Python environment and single-thread
numeric settings:

```powershell
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$python = 'C:\Users\Leo\stl-robustness-20260905\candidate\.venv\Scripts\python.exe'
Set-Location C:\Users\Leo\stl-joins-20260905\combined
& $python -m pytest --tb=short
& $python -m ruff check .
& $python tools/benchmark_surface_corpus.py `
  C:\Users\Leo\stl-robustness-20260905\corpus\fusion-controls\manifest.json `
  --id 27029_3fd2328f_8 --id 133589_40359674_2 --id 71503_271b24c0_0 `
  --output ..\results\fusion-combined-v1.json --case-timeout 240 --workers 1 --no-resume
```

Remote `results` contains the comparisons, diagnoses, and test logs. Successful
cone output is retained under `results/examples/27029_3fd2328f_8/`. Local copies
of the summary evidence, reviewed comparison image, and example STEP are in
the ignored `benchmarks/joining_20260905/` directory.

Kernel references used for the experiments:

- [Validity checks](https://occt3d.com/dev/doc/refman/html/class_b_rep_check___analyzer.html)
- [Volume construction](https://dev.opencascade.org/doc/occt-7.5.0/refman/html/class_b_o_p_algo___maker_volume.html)
- [Shape tolerances](https://dev.opencascade.org/doc/refman/html/class_shape_fix___shape_tolerance.html)
- [STEP export options](https://github.com/CadQuery/cadquery/blob/master/doc/importexport.rst)
