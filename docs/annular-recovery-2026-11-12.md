# Registered annular residual recovery

Baseline: `85beea11fb35bcd37d514c0471bba71fda69467d` (converter sources
also match the retained `266bc3e` inner baseline). Candidate source SHA256,
`app/surface_brep.py`:
`7b509e95e6f19a18ddcb9c9cbfd2e712241ef2a44518c4a6d4ee48a47c32987c`.

A coarse public export already passed, while the same model's middle export
fell back to 446 triangle faces around a small closed profile. The old graph
fit could not parameterize that annular residual. Independently fitting its
two rims also left incompatible cap trims and mismatched periodic seams.

The candidate registers loop winding and cyclic start, interpolates both
closed rims on one chord-parameter grid, and lofts one periodic smooth face.
Both planar neighbors use the same complete boundary edges. Recovery is
limited to small, two-loop residuals that the old graph fit cannot represent.
Every source support node must pass the existing fit tolerance; both cap
boundaries must pass tighter local curve/plane checks together. The normal
validity, geometry, topology and strict no-facet gates remain unchanged.

## Matched evidence

| Check | Baseline | Candidate |
| --- | ---: | ---: |
| Selected inner cases, strict | 4/13 | 5/13 |
| Seven remaining exports of affected source, strict | 4/7 | 4/7 |
| Union, strict (20 cases, five source models) | 8/20 | 9/20 |
| Union, geometry | 20/20 | 20/20 |
| Affected source, strict exports | 5/10 | 6/10 |
| Affected source, all-ten success | 0/1 | 0/1 |
| Private development anchors, geometry | 3/3 | 3/3 |

No prior strict success was lost. Two private non-faceted controls remain
non-faceted; the difficult private coarse anchor still falls back. Personal
cases without reference CAD are not counted as strict reference successes.
All cases above are development data. These are selected, correlated exports,
not independent designs or an unbiased estimate of the requested 10,000 cases.

The full local Python suite passes: 257 tests, zero failures or skips, in
310.8 seconds. Python lint and the production frontend build also pass.
These local checks are separate from hosted CI for the eventual commit.

For the newly recovered middle export, fallback faces fall from 446 to zero.
The delivered valid, single solid has 19 faces: eight planes, six cylinders,
four extrusion surfaces and one B-spline. Reference maximum/local deviations
remain 0.0158473/0.0159168 mm; reference volume error changes from 0.304924%
to 0.298808%. Maximum input deviation remains 0.0114306 mm. Conversion time
changes from 15.07 to 15.55 seconds, sampled process-tree working set from
1.72 to 1.84 GB. This is a recovery improvement, not a demonstrated speedup.

Input/reference/recovered cross-sections were visually compared at five
depths through the small hole. The hole remains open. The browser success
workflow took 22.6 seconds and exported a valid 19-face single solid. The
downloaded STEP matched the server file:
`8abf738e7d656d78880c8e3d3fafaedccf03643ae5f1b9cfa67c518723b68471`.
Saved-run reopen restored both input and STEP previews. Projection and wheel
zoom were driven on both; native right/middle drag remains unverified.

## Reproduction and retained local evidence

Use Python 3.12 with the locked dependencies, serially, with OMP, OpenBLAS,
MKL and NumExpr thread counts set to one. Frozen manifests and geometry remain
local because of upstream licensing and personal data. Typical candidate
commands, from the repository root:

```powershell
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/inner-matched13.json `
  --output benchmarks/multires-1000/experiments/registered-annular-v9-matched13.json
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/affected-remaining-seven.json `
  --output benchmarks/multires-1000/experiments/source-remaining7-registered-v9.json
python -m pytest tests/test_annular_residual_recovery.py tests/test_surface_brep.py
```

The retained JSON contracts bind exact source, input/settings hashes and
runtime. Baseline files use `inner-baseline-266bc3e` and
`source-remaining7-baseline-85beea1`. The source-ten comparison explicitly
aggregates seven new cases with three earlier retained records; it is not a
new fresh full-ten run. Private anchor records, inspection STEP/plots, browser
screenshots, and rejected V1 through V8 patches/probes remain in the ignored
benchmark tree. Acquisition/evaluation tooling is already merged in PR #1.

## Continuing work

The 1,000-source / 10,000-input corpus and its complete frozen baseline remain
unfinished (69/690 fixtures at this checkpoint). Resume acquisition in bounded
batches between experiments. Four exports of the improved source still fail;
inspect their residual loops and segmentation before extending this recovery.
Unequal sampling must preserve recognized breakpoints and must pass the same
source-node, shared-boundary and delivered-shape checks. Do not tune on holdout
failures or count a faceted result as smooth recovery.
