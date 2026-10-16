# Dense recognition and 500-case validation, October 16, 2026

Code commit `50980b5e8284dd4f6d36dd167490490cf9070dbd` passed all 240 tests
and a fresh run of the same 500 input/reference pairs used in the September 18
release evaluation. Geometry acceptance is 498/500,
strict CAD acceptance is 384/500, and verified
non-faceted output is 406/500. No historical pass
in any of those three categories was lost.

The only promoted source change in this checkpoint filters irrelevant face
adjacency rows before two Python loops. It reduces dense-model recognition
time in the profiling comparison. Dense CAD construction and cylinder-area
precision still need further work. Their existing verified outputs are retained.

## Exact adjacency filtering

Two loops previously visited the full mesh adjacency array for each local
residual or freeform chart. NumPy membership masks now select the rows those
loops can use. The loops retain the same endpoint orientation, duplicate rows,
row order, patch ownership and subsequent update order. Fitting equations,
thresholds, output checks and worker budgets are unchanged.

On dense fillet `54506_cb98479f_0`, one matched recognition profile changed from
105.2733 seconds at `a4d7c83` to 52.8906 seconds with these filters. Both runs
assigned all 147,990 cleaned triangles to 340 patches with 831 adjacencies.
Patch counts were 87 plane, 25 cylinder, one torus, 32 extrusion and 195 freeform.
The local equivalence checks covered 528 row selections, 1,056 complete chart
function comparisons and six sequential patch-update cases. Three new
parameterized regressions check selected-face coverage, crease partitions
and separation across unselected faces.

These measurements support the local filtering optimization. The earlier
profile did not save a complete fitted-parameter digest, so identical counts
are not a claim of byte-identical surface graphs.

The complete conversion still exceeds the unchanged 60-second analytic worker
budget for this dense input. A fresh pipeline trace reached completed recognition
and then timed out in B-rep construction. Its delivered STEP remains the same
verified 12,000-face fallback, with maximum deviation 0.0120492 mm, local
deviation 0.0170747 mm and volume error 0.0892198%. No final CAD recovery or
end-to-end speed improvement is claimed for this model.

A separate two-filter prototype for other active neighbor loops passed its
local equivalence checks but timed out in its one bounded build probe. It was
archived without promotion. That probe ran alongside the corpus and does not
provide a controlled timing comparison.

## Full corpus results

The run used all 500 distinct IDs in the frozen release manifest. All 1,000
input and reference SHA-256 values matched before evaluation and at final
validation. The manifest SHA-256 is
`eb091f9624846e88a1bec7705a36fd03f01882786f4b9e6c745da1ce7525c4b8`.
An immutable snapshot supplied all app and benchmark code. Its hashes stayed
unchanged throughout the run, and each final case record matches its saved
worker record.

| Gate | September 18 | This run |
| --- | ---: | ---: |
| Delivered-STEP geometry | 497/500 | 498/500 |
| Strict CAD | 383/500 | 384/500 |
| Verified non-faceted output | 402/500 | 406/500 |

The [per-file results](release-results-2026-10-16.csv) include baseline gates,
current gates, source identities, quality measurements and conversion timings.
The changed passes are:

| Case | Newly passing gate |
| --- | --- |
| `146853_40cadbdd_27` | Delivered-STEP geometry |
| `146853_40cadbdd_27` | Strict CAD |
| `146853_40cadbdd_27` | Verified non-faceted output |
| `voron-stealthburner-cw2-c-stealthburner-led-diffuser-c306642fbb` | Verified non-faceted output |
| `34914_8911676d_26` | Verified non-faceted output |
| `30675_8c9704a7_0` | Verified non-faceted output |

These cumulative changes include the previously documented
[planar and sparse-profile recovery](reconstruction-improvements-2026-10-16.md).
The new adjacency filters alone do not account for the recovered cases.
The ten previously selected models retain 8 geometry,
6 strict CAD and 7 verified
non-faceted passes, matching `a4d7c83`. Their statuses, face-type counts and
fallback representations are unchanged in the corpus run.

Case statuses are 406 `complete`, 94 `best_effort`. The geometry rejections are
`60977_4bb093ec_0`, `41117_1b3271f2_96`.
Both retain four non-manifold input edges and remain unverified.
The saved failure ledger records 116 strict CAD failures. Categories overlap:

| Failure category | Cases |
| --- | ---: |
| unclean_analytic_representation | 115 |
| analytic_area_recall | 99 |
| faceted_surface_fallback | 92 |
| missing_analytic_surface_types | 89 |
| global_source_mesh_fallback | 86 |
| residual_bspline | 11 |
| analytic_area_precision | 4 |
| local_surface_deviation | 2 |
| analytic_trim_failure | 2 |
| edge_closed_invalid_shell | 1 |
| step_roundtrip_invalid | 1 |
| invalid_brep | 1 |
| invalid_solid | 1 |
| surface_deviation | 1 |

## Runtime and resources

The benchmark completed in 66.61 minutes.
The outer manager completed in 66.78 minutes.
It used four worker slots at below-normal process priority with numerical
library thread limits set to one. OCCT meshing can still use additional native
threads. The lowest sampled available memory was
32.90 GiB, lowest free disk space was
51.05 GiB, and peak sampled whole-system CPU
use was 58.11%.

The manager enforced a 90-minute outer limit, a 6 GiB available-memory floor,
an 8 GiB free-disk floor and a sustained CPU-contention guard. The benchmark
supervises each conversion and reference audit together for at most 360 seconds.
Its 240-second conversion allowance is checked after conversion returns, and
the reference audit has a separate 120-second allowance. Product API job
supervision and its existing density-dependent analytic worker budgets were
unchanged. No job exceeded the manager's resource limits.

The largest measured conversion took
228.494 seconds; the longest complete
case including its audit took 276.583 seconds.
Historical timings used earlier code and a different execution context, so
this run does not establish a corpus-wide speedup or slowdown.

The installed runtime was Python 3.12.13, CadQuery 2.8.0, OCP 7.9.3.1.1,
NumPy 2.4.6, SciPy 1.18.0 and trimesh 4.12.2. The existing environment and local
corpus supplied everything.
There were no downloads, installations, external services, credentials, uploads
or paid calls. Per-case JSON and logs are retained locally; temporary STEP files
from the 500-case run were cleaned by the existing benchmark. Focused experiment
artifacts are retained separately. No dataset geometry was added to Git.

## Cylinder experiment and remaining design limit

Model `30675_8c9704a7_0` retains its verified 315-face non-faceted STEP.
Its cylinder-area precision is 93.0347%, below the unchanged 95% strict gate.
An experimental triangle-normal check removed two false cylinder patches and
reached 100% precision, but the result used 841 triangular faces. That experiment
was rejected and the production classifier restored before this checkpoint.

Independent tracing localized the new residual to one extrusion patch with
351 profile nodes and 288 canonical boundary edges. Uniform downsampling to the
existing 128-point budget leaves an original profile vertex 0.744523 mm from
the downsampled polyline. The resulting fitted support overshoots and its
canonical trim is invalid. Existing
area and trim checks correctly reject it. One fixed geometry-aware simplification
removed the extreme excursion but still failed fidelity and trim validity.

Further recovery needs profile breakpoint retention within the fitting budget,
validation between profile nodes and valid canonical trimming. Raising timeouts,
relaxing precision, accepting invalid trims or replacing the current non-faceted
baseline would not close that gap.

## Reproduction and test coverage

All 240 collected test nodes passed exactly once across six supervised groups,
with zero failures or skips. Python lint passed. Test and source hashes match
the evaluated code. The frontend is unchanged from its earlier successful build.

```text
python -m pytest -o addopts= -q -p no:cacheprovider tests
python -m ruff check app tests tools
python -u tools/benchmark_surface_corpus.py release-500-manifest.json --output results.json --workers 4 --case-timeout 240 --no-resume
```

The manifest must resolve the existing local input/reference pairs; dataset
paths are machine-specific. Exact commands, all six test-group logs and XML,
runtime versions, source hashes, immutable snapshots, profiling comparisons,
rejected experiments and the full failure ledger are retained in the local
validation workspace. Gates for validity, topology ownership, component counts,
deviation, volume and CAD representation remain unchanged.
