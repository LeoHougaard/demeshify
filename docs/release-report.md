# Release evaluation, September 17, 2026

A subsequent [additional-design evaluation](design-evaluation-2026-09-18.md)
tests 56 further files, including real STL exports and multiple tessellations of
reference STEP models, and reruns the original 500. That regression retains
497 geometry passes and improves strict CAD recovery to 383, with no lost passes.
The additional designs' substantially lower CAD recovery rate limits how far the
original development-corpus result below can be generalized.

The completed 500-file evaluation reaches 99.4% geometric acceptance and 76.4%
strict CAD recovery. Near-100% CAD recovery remains unmet. The application checks
below support a documented local public beta; they do not establish universal
STL conversion or production manufacturing suitability. No release has been
published.

## Results

| Measure | Corrected baseline | Final candidate |
| --- | ---: | ---: |
| Geometry accepted | 461/500, 92.2% | 497/500, 99.4% |
| Strict CAD accepted | 335/500, 67.0% | 382/500, 76.4% |
| Conversion timeouts | 36 | 0 |

There are 47 additional strict CAD passes and 36 additional geometry passes.
No baseline geometry or strict CAD pass is lost. No previously verified
non-faceted result is replaced by facets. The final full run took 3,499 seconds,
about 58 minutes. [Per-file results](release-results-2026-09-17.csv) include all
500 input and reference hashes, baseline outcomes, final outcomes, and measured
errors.

| Input family | Files | Geometry accepted | Strict CAD accepted |
| --- | ---: | ---: | ---: |
| Single extrusion | 193 | 192 | 185 |
| Multiple extrusions | 126 | 125 | 117 |
| Fillets | 123 | 122 | 55 |
| Chamfers | 26 | 26 | 23 |
| Fillets and chamfers | 10 | 10 | 1 |
| Voron printed parts | 22 | 22 | 1 |

Of the 497 geometry passes, 382 meet strict CAD acceptance, 17 have non-faceted
CAD surfaces but miss representation-quality checks, and 98 use verified faceted
fallbacks. This distinction is essential: geometric acceptance alone does not
establish successful recovery of curved CAD surfaces.

Three inputs remain unverified:

| Input | Remaining source defect |
| --- | --- |
| `146853_40cadbdd_27` | 32 open boundary edges after degenerate-triangle cleanup |
| `60977_4bb093ec_0` | Four non-manifold edges |
| `41117_1b3271f2_96` | Four non-manifold edges |

These inputs do not provide a comparable closed volume. They remain failures
under the unchanged geometry gate and are included in the 500-file denominator.
The converter retains best-effort output without claiming it is verified.

The 98 verified fallbacks include 44 cases that reached the isolated analytic
worker's deadline and 54 whose fitted result failed topology or geometry checks.
All completed the overall conversion deadline through fallback. Among the 17
verified non-faceted models below the CAD gate, ten miss curved-surface type or
area checks, five introduce unexpected generic surface types, and four report C0
boundary fills. These categories overlap.

Further CAD work should focus on fillet/torus recognition and joining fitted
faces, followed by the expensive analytic cases. The failed experiments and
preserved diagnostic artifacts are recorded in the work log. Increasing the
reported rate by relabelling facets or weakening acceptance would conceal these
remaining limits.

## What counts as a pass

Geometry acceptance checks the delivered STEP against the full-resolution input
STL. It requires a valid solid, matching component count, comparable closed
volume with at most 2% error, and sampled P95, maximum, and local maximum surface
deviation within `max(0.12 mm, 0.003 * bounding-box diagonal)`. Simplified fallbacks
must pass these same checks against the original input. These are sampled
geometric checks, not a proof of an exact Hausdorff-distance bound.

Strict CAD acceptance also requires no triangle fallback, no source-mesh carrier,
no missing reference analytic surface types, and at least 95% area recall and
precision for each expected curved analytic type. Unexpected generic surface
types and C0 reconstruction warnings fail this gate. Reference STEP geometry is
used only by the independent evaluator after conversion. The reconstructor
receives the STL alone.

Passing the CAD gate does not mean the original feature history was recovered.
The test evaluates surface reconstruction, the application's default engine.

## Corpus and baseline

The fixed corpus contains 500 distinct STL hashes: 478 seeded Fusion 360 Gallery
components and 22 Voron controls. The Fusion selection uses the latest available
revision of each component in the local official-test corpus, with seed
`20260917`. Selection does not filter by conversion outcome, model complexity,
or triangle count within that pool. The pre-existing pool has 5,037 revisions of
3,158 components. Its materialization excludes 842 revolve-family revisions and
575 cases outside its single-solid extrusion, profile, or source-validity scope.
The release selection does not undo those earlier scope restrictions.

The corpus includes 193 single-extrude, 126 multi-extrude, 123 fillet, 26 chamfer,
10 combined fillet/chamfer, and 22 Voron files. Complexity labels divide it into
292 simple, 154 medium, and 54 complex cases. Ten raw inputs are not watertight
and one reference B-rep is invalid. All remain in the denominator.

This corpus mainly represents designed mechanical CAD. It is not a representative
sample of noisy scans, arbitrary decorative meshes, or every public STL source.
The fixed corpus was used during development; it is not an unseen holdout set.

The baseline application is commit `e68b340b5d33fe558bce82615830e17ea1d3f606`.
An evaluator defect originally counted a slow independent reference audit against
the conversion deadline. The corrected evaluator gives conversion 240 seconds
and independent reference auditing another bounded 120 seconds. It does not
change geometry or CAD acceptance thresholds, and production retains its
240-second supervised job deadline.

The corrected baseline reuses 456 originally completed cases, which already met
the tighter combined deadline, and reruns all 44 original timeout cases with
unchanged baseline application files. Eight rechecks add geometry passes and none
adds a strict CAD pass. The original results and each row's provenance are retained
locally. Timings overlap some diagnostic work and do not establish a controlled
speedup.

The final candidate runs with four workers and one numerical-library thread per
worker on Windows, an AMD Ryzen 7 5700G, and 64 GB RAM. Input and reference hashes
are checked against the frozen selection. The local manifest SHA-256 is
`ba8c0f3469d93df7c20d27678c849889a0bf64b67954a8ea2033872c51a0f27b`.
Its absolute paths are machine-specific; per-file hashes identify the corpus on
other machines.

## Reproduction

Obtain the source data under its terms and prepare the local manifests using
`tools/materialize_fusion_official_test.py` and
`tools/materialize_assembly_corpus.py`. See [benchmark data](../datasets/README.md)
for the pinned Voron sources and data restrictions. Model geometry and generated
STEP files are not included in this repository.

```powershell
python tools/select_release_corpus.py datasets/fusion_official_test/manifest.json --control datasets/voron_stealthburner_cw2/manifest.json --control datasets/voron_tap_r8/manifest.json --output benchmarks/release/manifest.json
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
python tools/benchmark_surface_corpus.py benchmarks/release/manifest.json --output benchmarks/release/results.json --case-timeout 240 --workers 4 --no-resume --artifacts-root benchmarks/release/artifacts
```

## Application verification

- All 180 Python tests pass on the final candidate. Ruff passes.
- A clean source copy installs the locked dependencies with Python 3.12.13.
  Its frontend production build, supervised sample conversion, saved report,
  and STEP download pass.
- Audits of the locked Python runtime and browser dependencies report no known
  vulnerabilities. These checks are not a complete security audit.
- Browser checks cover sample conversion, saved-run loading, STEP download, and
  clear faceted-fallback labels at desktop and 390-pixel mobile widths. The latest
  checks inspect the DOM; screenshot capture was unavailable.
- Both largest inputs also pass through the actual HTTP upload, supervised
  conversion, saved-report, and download endpoints. The 3.3-million-triangle
  input completes the request in 182.54 seconds with 48,000 fallback faces; the
  696,008-triangle input completes in 155.18 seconds with 60,000 fallback faces.
  These checks use the normal launch environment, including inherited
  `OMP_NUM_THREADS=16`, and the unchanged production 240-second watchdog.
  Download SHA-256 hashes exactly match the verified files on disk.
- The largest saved result passes mobile DOM checks for the verified-fallback
  label, actual 48,000-face count, and no horizontal overflow. The input preview
  is disabled and its oversized body is not downloaded. A later desktop resize
  could not be checked because the browser automation host became unavailable;
  the earlier desktop checks above remain the available evidence.
- Host and browser-origin checks, bounded job admission, download validation,
  and progress completion have regression coverage.

The build reports an advisory for a 795 kB JavaScript chunk. Python tests retain
one Starlette test-client deprecation warning. Neither prevents these checks
from passing. CI still needs to run on the eventual release commit.

The supported deployment remains a local, single-user application or private
Codespace. See [the security policy](../SECURITY.md) for that boundary.
The [work log](release-work.md) records the measured changes and rejected trials.
