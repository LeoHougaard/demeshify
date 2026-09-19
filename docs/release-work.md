# Release verification and conversion improvement

## Goal

Prepare the local application for public release. Measure conversion on a fixed
500-file corpus, improve recovered CAD surfaces toward 100%, and retain verified
faceted STEP as a separately reported fallback. Do not weaken validity, error,
volume, component-count, or analytic-representation checks to increase the rate.

## Plan

1. Verify installation, dependencies, API boundaries, and the browser workflow.
2. Freeze 500 distinct STL inputs and measure commit `e68b340` as the baseline.
3. Group failures by cause. Test one general geometry change at a time against
   affected cases and existing successes.
4. Re-run the fixed corpus on the final candidate and report both CAD recovery
   and geometric acceptance, including unresolved failures.

## Current result

The final frozen v9 run completed all 500 files: 497 geometry passes (99.4%),
382 strict CAD passes (76.4%), and no conversion timeouts. The corrected baseline
is 461 geometry and 335 strict CAD passes. There are no geometry, strict CAD, or
verified non-faceted regressions. All application and evaluator files still match
the frozen v9 hashes. The full Python suite passes all 180 tests.

The near-100% CAD goal remains unmet. Both largest files pass the actual upload,
supervised conversion, saved-report, and download API in 182.54 and 155.18 seconds.
Downloaded STEP hashes match the verified files. The largest result also passes
mobile DOM checks, including the skipped oversized input preview. Final Ruff,
whitespace, source-hash, and public results-table checks pass.

The [release report](release-report.md) records the completed corpus and public
beta limits. Local checks support that documented beta scope; CI still needs to
run on the eventual release commit. No commit, tag, push, or release was made.
Entries below retain the history of individual experiments; intermediate counts
and running states are historical.

## Evaluation contract

The corpus is under the ignored `benchmarks/release_20260917/` directory. Its
manifest records input and reference hashes. It includes all 22 existing Voron
controls and 478 seeded random Fusion component revisions. Only the latest
available timeline revision of each component is eligible. Duplicate STL hashes
are removed. Selection does not depend on conversion outcomes, triangle count,
or model complexity. Seed: `20260917`.

There are 193 single-extrude, 126 multi-extrude, 123 fillet, 26 chamfer,
10 combined fillet/chamfer, and 22 Voron cases. The manifest identifies 10 open
inputs and one invalid reference B-rep. These remain in the evaluation and must
be reported explicitly, rather than silently removed from the denominator.

The baseline source is an unchanged Git archive of `e68b340`. Runs use four
worker processes, one numerical-library thread per process, and a 240-second
conversion deadline. Independent reference auditing has its own bounded 120-second
allowance. Candidate runs must use the same conditions and manifest.
The reconstructor receives only the STL. Reference STEP files are read afterward
by the evaluator to assess the recovered representation.

Reproduce the exact selection and hashes with:

```powershell
python tools/select_release_corpus.py datasets/fusion_official_test/manifest.json --control datasets/voron_stealthburner_cw2/manifest.json --control datasets/voron_tap_r8/manifest.json --output benchmarks/release_20260917/reproduced-manifest.json
```

```powershell
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
.\.venv\Scripts\python.exe -u benchmarks/release_20260917/baseline-source/tools/benchmark_surface_corpus.py benchmarks/release_20260917/manifest.json --output benchmarks/release_20260917/baseline.json --case-timeout 240 --workers 4 --no-resume --artifacts-root benchmarks/release_20260917/baseline-artifacts
```

## Release checks so far

- Baseline: 153 Python tests passed in 213 seconds; Ruff passed.
- Locked Python runtime dependencies: pip-audit found no known vulnerabilities.
- Locked browser dependencies: npm audit found no known vulnerabilities;
  production build passed.
- GitHub private vulnerability reporting was disabled despite the security
  policy directing reporters there. Enabled it and verified the setting.
- Corrected README repository links to `LeoHougaard/demeshify`.
- Added host and browser-origin checks and shared admission limits for synchronous
  conversions, background conversions, and feature edits. Regression checks are
  in progress.

The sample converted and its STEP download returned valid STEP content. Desktop
and mobile views were inspected. Browser testing uncovered premature completion
from an inner worker. Progress now remains running until the supervising job
finishes; reloading an active run resumes polling. The focused API and progress
suite passed after this fix.

## Geometry experiments

- Cylinder interior validation recovered `25395_5e6c2bd3_0`. Its long flat walls
  had been fitted as shallow cylinders using end vertices. The final version
  permits normal chord error while rejecting bows beyond the geometry budget.
  The thin-plate regression and existing coarse-cylinder tests pass.
- Changing preview tessellation to absolute deflection did not recover the two
  targeted timeouts. Rejected that change.
- Constant-section construction recovered two targeted prismatic cases. The
  first candidate lost short circular arcs on `35740_5322d674_12`; it was stopped
  after finding this regression. Recovery now checks that clearly recognized
  curved surfaces are retained. A dedicated regression test passes.
- Simplifying dense meshes before analytic recognition produced fragmented
  surfaces and a native crash on `139597_7005536c_10`. Rejected that approach.
- Simplifying only the faceted fallback recovered that dense case in a 37-second
  direct trial with 8,000 faces, 0.026 mm maximum sampled local deviation, and
  1.42% volume error. Verification used the original 134,622-triangle input.
  The candidate uses 12,000 faces and reports simplification explicitly.

Baseline and candidate source snapshots remain immutable under the ignored
benchmark directory. The first candidate batch was stopped and is partial.
Candidate v5 completed all 500 cases; v9 has completed the final full-corpus rerun.
Diagnostic and baseline runs overlap parts of these batches, so timings are not
evidence of a controlled speedup.
Any timeout-sensitive final comparison needs a matched rerun without diagnostics.

Readiness is assessed in the current result and release report above.

### Further measured trials

- The existing feature-tree engine recovered none of eight selected surface
  failures. Five timed out, one crashed, and two failed geometric verification.
  Rejected as an additional release recovery path.
- Cylinder normal validation now accounts for the angular span of each source
  triangle. Rounded ends can no longer make a neighbouring flat wall look like
  a shallow cylinder merely because its vertices fit within the distance budget.
  The ten-case targeted run recovered seven strict CAD results, with all ten
  passing geometry. It retained the previously passing short-arc case. The
  graph, periodic-boundary, joining, and prismatic tests passed, including coarse
  cylinders. Evidence: `chord-validation.json`.
- Two dense fallbacks missed the 0.12 mm limit at 12,000 faces. Mesh trials at
  24,000 faces reduced local deviations from 0.195 and 0.170 mm to 0.109 and
  0.107 mm respectively. An adaptive budget can address these without changing
  the geometry gate; final STEP verification is still required.
- Both adaptive fallbacks passed final STEP verification at 24,000 faces in
  `adaptive-fallback.json`. Regression checks prove scoring still receives the
  original dense input and that inch units are preserved.
- Layered construction recovered `49145_4a5b250e_14` with strict analytic and
  geometry checks passing. The four-case trial retained both existing CAD
  successes and the remaining verified fallback. A synthetic circular boss on
  a rectangular plate also passes.
- Candidate v2 was stopped after 153 completed cases to replace it with the
  improved candidate v3. Its partial results and immutable source are preserved.
  Candidate v3 evaluates all 500 cases from a separate immutable source snapshot.
- The full Python suite passed after these geometry changes. The additional
  layered-boss regression passed separately. Ruff and the browser build passed.
  Browser DOM checks confirmed the verified-fallback label, 24,000 displayed
  facets, and final filename. A late provisional-preview race was corrected.
  Screenshot capture failed during the final revisit, so that revisit used DOM
  checks; the earlier desktop and mobile layouts were visually inspected.
- Broadening periodic-face splitting did not recover any of four trial cases;
  reverted it. Tightening the area agreement for a full-angle cylindrical face
  from 20% to 2% recovered `21893_0500d043_50`. The old shortcut filled the side
  openings where a bridge met two rings and produced three disconnected solids.
  A synthetic two-ring bridge reproduces the original failure, with 1 mm local
  error and three shells. The fix produces one solid, no residual fitted faces,
  and 0.00034 mm local error. The surface/periodic/joining tests pass.
- The original baseline completed all 500 files: 335 strict CAD passes, 453
  geometry passes including fallback, 44 timeouts, and three other geometry
  failures. Total wall time was 5,501 seconds with overlapping diagnostic work.
- A tenfold smaller default trimming tolerance recovered `134478_5bf44488_0`
  and `22666_bdaa1303_8`, but lost previously accepted `137098_0a45d1c8_6`.
  Rejected as a global default. The next trial keeps the normal path and only
  retries a failed, small analytic construction with finer trimming. Retry
  artifacts replace the original only after unchanged STEP/geometry checks pass.
  Tests reject an otherwise-valid but undersized retry without overwriting the
  original artifact.
- Candidate v3 was stopped after more than 168 cases to reduce CPU contention.
  Candidate v4 continues its full 500-file run. Timeout-sensitive losses require
  a controlled rerun after the diagnostic workloads finish.
- The conditional fine-trimming retry passed all three matched cases: both new
  recoveries and `137098_0a45d1c8_6`. Candidate v4's loss on that last part was a
  timeout during concurrent diagnostic load; the controlled retry completed with
  its original accepted geometry. Candidate v4 was then stopped as a partial run.
- Candidate v5 freezes the promoted geometry changes for the full 500-file test.
  No other corpus or diagnostic batch runs alongside it. The Python suite is
  rerun once on this version; any timeout-sensitive cases still need checking
  after that suite finishes. Earlier candidate files remain partial evidence.
- All 173 tests passed in 239 seconds on candidate v5. The browser build,
  launcher syntax checks, Ruff, and whitespace checks pass.
- Further cone research is separate from the frozen v5 batch. A small diagnostic
  batch runs with one worker while v5 runs with four; this means final timings
  still need a check without diagnostics. The first cone-peeling proposal found
  four missing Voron cones but recovered no strict CAD passes in four files.
  Finer trimming of its remaining freeform faces did not help and was reverted.
- A revised cone proposal checks source nodes rather than chord-interior points,
  grows validated cone regions, and uses the existing datum-axis regularizer.
  On a synthetic chamfered slot it recovers all six planes, two cylinders, and
  two cones, with one valid solid, no residual fitted faces or facets, and
  0.0062 mm maximum local deviation. Real-file evaluation is still pending;
  this proposal is not yet promoted over the immutable v5 snapshot.

- The revised cone trial still recovered no strict CAD passes on the four real
  files, though the chamfered-slot regression and all 46 surface tests pass.
- Candidate v5 exposed a CAD regression on `43248_dce7bc76_1`: tiny transition
  strips rejected a supported cylinder, leaving a general extrusion. Simple
  area weighting recovered it but regressed two flat-wall controls. Rejected.
  The revised exception requires 99% area support and measurable variation in
  the supporting normals. All four matched cases pass strict CAD and geometry,
  including the short-arc control; all 14 graph tests pass. Evidence:
  `weighted-chords-v3.json`. The frozen v5 run remains unchanged.
- Raising spline cost globally recovered a thin constant-section part but
  regressed the genuine changing-curvature spline test. Rejected. A narrower
  exact-circle preference is being evaluated without changing production code.
- Strongly circular profile spans now receive preference over a single general
  spline spanning multiple radii. The genuine-spline control still passes. The
  eight-case trial retains all controls and recovers `51860_32540fd4_30`, with
  essentially full cylindrical area recall. All 52 existing profile/prismatic
  tests pass; the new two-radius fixture passes separately. Evidence:
  `exact-profile-arcs.json`. Raising the extrusion-operation limit recovered
  none of five additional files, so that proposal was not adopted.
- The largest input (`86194_c1abdf4a_0`) cleans to one watertight body. Its first
  reduction passes distance checks but fails the unchanged 2% volume gate. A
  staged reduction avoids repeating the expensive collapse from 3.3 million
  triangles, but the full 48,000-face STEP path still exceeds 240 seconds.
  This remains unresolved; no timeout or geometry gate was relaxed.
- A second thin-part regression (`115430_9b0c814b_0`) showed that 99% area
  support was too strict for genuine cylindrical transitions. Using the same
  95% support fraction as the count-based check, plus the independent curvature
  requirement, passes all six matched CAD/geometry cases. The flat-wall and
  sliver graph tests pass. Evidence: `weighted-chords-v4.json`.
- The native faceted STEP mode terminated its isolated diagnostic process and
  was rejected. Disabling redundant parametric curves only for global planar
  triangle carriers keeps all nine fallback tests passing and reduces a
  48,000-face export from 120.8 MB to 52.4 MB, but the largest complete case
  still times out. Verification profiling continues.
- Batched verification distance queries retain exactly the same probes and
  statistics while building each spatial index once. Three matched real-file
  score reports are exactly identical to the old implementation.
- Frozen candidate v5 completed all 500 cases: 376 strict CAD passes (75.2%)
  and 495 geometric passes (99.0%), versus baseline 335 and 453. There are
  46 new strict CAD passes and five CAD regressions. The five geometric failures
  are the three previously invalid inputs and timeouts on the two largest files.
  Wall time was 4,199 seconds, with overlapping diagnostic work.
- The two periodic trimming regressions are recovered by recognizing complete
  end rims despite tiny tangent-filleting strips. Both matched periodic cases
  pass, including the interrupted-cylinder control.
- The last timeout regression spent its budget fitting profiles for sections
  that were ineligible for prismatic recovery. Raw section measurements now
  screen persistence, layered volume, and operation count before curve fitting;
  only the middle persistent section needs fitting. The final two CAD regression
  cases pass in `final-regressions.json`; broader profile validation is pending.
- Direct triangle extraction through CadQuery wrappers was slower and was
  replaced with raw OpenCascade topology traversal. On a 12,000-face delivered
  STEP this extracts the exact verification mesh in 0.87 seconds versus 33
  seconds for the wrapper trial. Existing validity and geometry gates remain.

- Raw-section screening passes all seven matched CAD and geometry cases,
  including the profile timeout regression. All 53 profile/prismatic tests pass.
  The full Python suite of 177 tests passed before the evaluator timing correction; all
  15 evaluator tests and Ruff pass after it.
- Corrected an evaluator timing bug. The largest input completed conversion,
  including delivered-STEP geometric verification, in 226 seconds. The former
  240-second whole-case watchdog killed the subsequent independent reference
  audit and reported a conversion timeout. Conversion still has a 240-second
  acceptance limit and production watchdog; reference auditing now has its own
  120-second allowance. Tests reject a conversion taking 241 seconds and accept
  a 239-second conversion followed by a 10-second reference audit. All geometry
  and CAD representation gates remain unchanged.
- Re-evaluating the original baseline's 44 timeout cases with unchanged baseline
  application files and the corrected evaluator. Its 456 completed cases already
  passed the tighter combined deadline. The original baseline is retained;
  corrected comparison results must identify the reused and rerun cases.
- Frozen candidate v7 contains 84 source files with SHA-256 hashes. Its full
  500-file run uses four workers and the corrected evaluator. Baseline auditing
  overlaps this run; borderline timeout cases require a quiet matched check.
- A narrower exact-arc priority experiment recovered none of five matched cases
  and was reverted. Evidence: `exact-arc-priority.json`.
- Browser checks confirm a successful sample conversion, a downloaded STEP with
  an ISO-10303-21 header, saved-result loading, and correct 24,000-triangle fallback
  labelling at desktop and 390-pixel mobile widths. No horizontal overflow was
  observed. These latest checks inspect the DOM; screenshot capture is unavailable.

- A finer profile-tolerance trial examined all 71 previous CAD failures with at
  most 20,000 triangles. It recovered one additional strict CAD case, retained
  the previous exact-arc gain, and did not resolve the remaining failures.
  Several passing controls could not use the finer profile candidate. This
  broader policy was not adopted; a safe conditional retry needs more evidence.
  Evidence: `fine-profile-failures.json` and `fine-profile-trial.json`.
- The corrected two-largest trial passes the 3.3-million-triangle input, including
  its independent reference audit. The 696,008-triangle thin part still times
  out because its 48,000-face reduction has 2.052% volume error, so the current
  fallback tries the full original mesh. A 60,000-face diagnostic is watertight,
  retains the component count, has 1.456% volume error and under 0.003 mm sampled
  local deviation. Full STEP verification of that additional budget is pending.

- The 60,000-face fallback passes the full independent audit in
  `finer-dense-recovery.json`. Conversion, including delivered-STEP checks, took
  222.82 seconds; the separate reference audit also passed. The new regression
  confirms the finer attempt uses the original mesh, not an already simplified
  intermediate. All eight focused fallback tests and Ruff pass.
- Candidate v7 was stopped and retained as a partial run to include that fix.
  Frozen candidate v8 is now evaluating all 500 files from scratch. Its source
  hashes are in `candidate-v8-source/snapshot-hashes.json`.
- The three referenced CI action tags resolve upstream. A scan of the releasable
  source files found no recognized private-key or API-token patterns.

- Final candidate Python suite: 180 passed in 299.66 seconds. One dependency
  deprecation warning remains in Starlette's test client; no test failed.
- Reproducing the corpus matches all 500 ordered input and reference hashes.
  Manifest SHA-256: `ba8c0f3469d93df7c20d27678c849889a0bf64b67954a8ea2033872c51a0f27b`.
- A fresh source copy installed all locked dependencies using Python 3.12.13.
  Its clean npm install reports no vulnerabilities, and its production frontend
  build passes. The build retains Vite's advisory about a 795 kB JavaScript chunk.

- The clean installation served the built frontend, converted the sample through
  the supervised API job, verified the delivered solid, and returned a valid
  15,499-byte STEP download. Evidence: `clean-install-result.json`. First imports
  emitted dependency docstring syntax warnings; conversion and verification passed.

- The broader CAD check found a partial-recovery regression on
  `101635_11b839a3_15`: v5 returned valid planes, cylinders and an extrusion,
  while v8 needed facets. Both miss strict CAD acceptance, so checking only
  pass/fail rates would have hidden this loss.
- Restricting area-weighted normal consensus to circular-profile candidates
  with an independently fitted normal-derived axis restores that CAD model.
  Profile-only seeded fits and broad local fits retain triangle-count consensus.
  The five-case matched trial passes geometry on all five, preserves four strict
  CAD successes, and restores the partial CAD result. All 14 graph tests and
  Ruff pass. Evidence: `profile-normals-chords.json`.
- Candidate v8 is retained as a partial run. It had no strict CAD losses but the
  partial-CAD regression above, three malformed-input failures, and a large-file
  timeout under concurrent load. Candidate v9 is frozen and now reruns all 500
  files with the narrower cylinder policy. Final comparison also checks loss of
  previously verified non-faceted results.

- Final v9 Python suite: 180 passed in 288.74 seconds, with the same single
  Starlette test-client deprecation warning.
- Baseline deadline recheck is complete. Eight of the 44 original timeouts now
  pass geometry; none adds a strict CAD pass. `baseline-corrected.json` combines
  these 44 rechecks with the 456 originally completed cases and records each
  row's provenance. Corrected baseline: 461/500 geometry (92.2%), 335/500 strict
  CAD (67.0%), and 36 conversion timeouts. Application files are unchanged.

## September 18: additional design tests

- Added an evaluation of 21 recent original STL exports, two export resolutions
  of 16 distinct STEP models, and three resolutions of another matching STEP.
  These are 56 unique STL files. Private names, geometry, and per-file artifacts
  remain outside the public repository.
- All 21 original STLs are watertight. Correct metre interpretation restores
  their intended physical size; the original bytes are unchanged. Three apparent
  filename-matched STEP references were rejected as different geometry.
- Added visible import units and dimensions, an explicit metre suggestion, and
  a recoverable upload error explaining the difference between browser preview
  and server conversion. Local Chromium tests cover the real input, desktop and
  mobile layouts, failed upload, retry, conversion, and STEP download.
- Smaller local fitting samples recover adjacent small cylinders. Cone fitting
  now includes its initial apex in the optimizer bounds even when the apex lies
  far outside the mesh. These fixes have synthetic regression coverage.
- Thin layered recovery can restore full concave conical bore bands. A real
  plate now retains 60 conical faces without facets and passes the unchanged
  delivered-STEP geometry gate. Tests cover both ends of countersunk bores and
  exclude convex conical bosses from the cutting operation.
- Ordinary sphere trimming failed on both resolutions of a reference-backed
  part. A circular-rim recovery aligns the sphere pole with the measured rim.
  Both exports now pass strict reference checks, preserving all 25 CAD faces.
  Rotated small and large spherical-cap fixtures also pass.
- The large rotated-cap fixture fails against the preserved pre-fix source and
  passes with the change. The small-cap fixture remains a passing control.
- Finer general trimming, loose layered profiles without complete conical cuts,
  lofted layers, and broad cone reclassification were rejected when they lost
  curved detail, made invalid solids, or failed geometry checks. A periodic
  cylinder-boundary experiment remains unpromoted.
- Final candidate Python verification: 187 tests pass in 292.32 seconds, with
  one Starlette test-client deprecation warning. Ruff and frontend build pass.
  The frozen candidate completed all 500 original release cases and all 56
  additional cases, including explicit checks for lost non-faceted output.
- Final original-corpus result: 497/500 geometry passes, 383/500 strict CAD
  passes, and 402/500 verified non-faceted results. The previous candidate had
  497, 382, and 399 respectively. No earlier pass is lost. The three malformed
  inputs remain unverified, and no overall conversion times out. The run took
  4,507.62 seconds with three workers.
- Final additional-corpus result: 54/56 geometry passes and 19/56 verified
  non-faceted results, up from 13. Strict CAD improves from 5 to 7 of 36
  reference-backed cases. No prior pass is lost. The two failed exports are
  from one already-invalid assembly. The run took 2,226.78 seconds with two
  workers; overlapping workloads prevent a controlled timing comparison.
- The final spherical example also passes actual browser conversion, saved-run
  loading, STEP download, visual inspection, and a reference audit of the
  downloaded CAD. The preview server remains running. No release was published.
