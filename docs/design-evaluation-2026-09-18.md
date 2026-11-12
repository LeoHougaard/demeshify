# Additional design evaluation, September 18, 2026

CAD recovery improves on the additional designs, but remains well below the
near-100% target. The completed 56-file evaluation preserves every previous
geometry, strict CAD, and verified non-faceted pass.

| Measure | Baseline | Candidate |
| --- | ---: | ---: |
| Delivered STEP passes geometry checks | 54/56, 96.4% | 54/56, 96.4% |
| Verified output without triangle fallback | 13/56, 23.2% | 19/56, 33.9% |
| Strict CAD recovery where reference STEP is provided | 5/36, 13.9% | 7/36, 19.4% |

Twenty inputs lack matching reference CAD and are unscored for strict recovery.
Two reference-backed inputs fail before reference comparison; they remain in the
36-case denominator. The benchmark's raw aggregate divides strict successes by
all 56 cases, so the table explicitly uses the reference-backed denominator.

## Test selection

The additional corpus contains 56 distinct STL byte sequences:

- 21 recent original exports, including 17 related camera-mount components.
- Coarse and fine exports of 16 distinct recent STEP models, giving 32 cases.
- Three new export resolutions of another verified matching STEP.

These are 56 files, not 56 independent product designs. The original Downloads
files are unchanged. Private geometry, names, screenshots, and per-file results
remain outside the public repository. The converter receives only the STL;
reference STEP geometry is used after conversion to check its output.

| Input group | Files | Geometry accepted, before / after | Without facets, before / after | Strict CAD, before / after |
| --- | ---: | ---: | ---: | ---: |
| Original STL exports | 21 | 21 / 21 | 8 / 10 | 0 / 0 on the one matched reference |
| Two export resolutions of 16 STEP models | 32 | 30 / 30 | 5 / 9 | 5 / 7 |
| Three resolutions of the matched difficult design | 3 | 3 / 3 | 0 / 0 | 0 / 0 |

The two geometry failures are exports of the same already-invalid STEP assembly
containing 660 solids. They are included in every aggregate denominator.

## Full release regression

The same frozen candidate also completes the original 500-file corpus. Across
both corpora, 556 distinct STL hashes were tested, with no overlap.

| Measure | September 17 candidate | September 18 candidate |
| --- | ---: | ---: |
| Geometry accepted | 497/500, 99.4% | 497/500, 99.4% |
| Strict CAD accepted | 382/500, 76.4% | 383/500, 76.6% |
| Verified output without facets | 399/500, 79.8% | 402/500, 80.4% |
| Overall conversion timeouts | 0 | 0 |

No earlier geometry, strict CAD, or verified non-faceted pass is lost. The three
unverified inputs are the same malformed meshes identified in the original
release report. Of the 497 geometry passes, 383 meet strict CAD checks, 19 have
verified non-faceted output below that threshold, and 95 use verified facets.

The additional strict CAD pass is a sensor mount. Two chain-anchor models also
gain verified non-faceted output but remain below strict reference checks.
[All 500 per-file results](release-results-2026-09-18.csv) include the input and
reference hashes, prior and current pass flags, and measured errors. This fresh
run took 4,507.62 seconds, about 75 minutes, with three workers.

## Units and reference identity

All 21 original exports are watertight. Twenty contain one body and one contains
four. Their coordinates are below one unit; confirmed camera-part dimensions
establish a metre interpretation, about 180 by 250 mm. Baseline and candidate
comparisons use the same input bytes and units. An earlier millimetre run is
retained locally but does not represent the intended physical size.

Filename matching alone was insufficient to establish reference identity.
Geometric checks rejected three of four apparent STL/STEP counterparts as
different parts. Only the matching counterpart is retained as a reference. The
other 20 original exports can be checked for delivered geometry and fallback,
but their exact CAD-surface recovery rate remains unknown.

## Verified changes

- Import units and output dimensions are visible before conversion. Very small
  imports receive an explicit metre suggestion; scale does not change silently.
- Upload connection errors explain that the browser can preview a local file
  while conversion requires the server. A failed upload can be retried.
- Smaller fitting samples recover neighboring small cylinders. Cone fitting
  includes its initial apex in the optimizer bounds even outside the mesh box.
- Thin layered recovery restores full conical bore bands, including countersinks
  at both ends. One real plate now retains 60 conical faces without facets.
- When ordinary sphere trimming fails, a circular-rim recovery aligns the pole
  with the measured rim. Both resolutions of one part now preserve all 25 CAD
  faces, including its sphere, cylinder, and four cones. Minimum curved-area
  recall exceeds 99.9% against the original STEP.

The geometry gate, curved-area guard, strict reference checks, and worker
deadlines are unchanged. A result without facets can still miss strict reference
checks; two new results do, and are not counted as strict CAD gains.

## Remaining failures

| Reason for remaining incomplete recovery | Files |
| --- | ---: |
| Native worker deadline | 18 |
| Fitted solid rejected by existing checks | 10 |
| Native crash during fitting | 5 |
| Partial surface recovery with facets | 2 |
| Invalid source assembly exports | 2 |

The matching difficult design still uses facets at all three fresh export
qualities. Increasing triangle density alone does not solve its reconstruction.
The intact original STL topology and repeated failures from valid STEP sources
show that mesh repair would not address the main recovery failures in this set.

Saved worker progress and geometry identify problems in grouping curved blends,
trimming boundaries, joining faces, and native freeform fitting. A last progress
stage identifies where execution stopped, not the precise cause of a native
crash. Finer general trimming, incomplete conical cuts, lofted layers, and broad
cone reclassification were rejected when they lost curved detail, produced
invalid solids, or failed geometry checks.

## Verification and limits

All 187 Python tests pass. Ruff and the production frontend build pass. Tests
include countersinks at both bore ends, retaining convex conical bosses, and
rotated small and large spherical caps. One Starlette test-client deprecation
warning and the existing frontend bundle-size advisory remain.

Local headless Chromium checks cover a real camera STL at desktop and mobile
widths, metre selection, output dimensions, deliberate upload failure, retry,
supervised conversion, and STEP download. The spherical result also passes the
browser workflow and a reference audit of its downloaded STEP. Saved-result
loading and displayed CAD geometry were inspected. Shared-browser automation
was unavailable, so these were local browser checks; the preview server remains
running for manual use.

The additional corpus ran from frozen application source with two workers and
one thread per numerical library. It took 2,226.78 seconds. The 500-file release
regression used three workers. Overlapping work means these timings do not
establish a controlled speed comparison. The conversion deadline remains 240
seconds, with a separate bounded independent reference audit.

The original 500-case results and acceptance definitions are in the
[September 17 release report](release-report.md). The additional designs are
harder for this converter; its original corpus rate should not be presented as
the expected CAD recovery rate for arbitrary user files. No release has been
published by this evaluation.
