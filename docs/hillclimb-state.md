# Demeshify hill climb

## Contract and current best

Started November 12, 2026 from reliability branch
`codex/stl-reliability-20261011`, `fb6c6f23ff3e53d7f0b86450b64299e4a17400a4`.
The registered annular-boundary candidate (V9) is locally qualified for a
source milestone; see `docs/annular-recovery-2026-11-12.md`. It improves one
strict reconstruction without losing a prior pass in 20 matched public cases.
The October
498/500 geometry, 384/500 strict CAD and 406/500 non-faceted results are
historical, not a fresh baseline. Existing source and private geometry remain
local. Research is not paused. No duration or spending budget was supplied.

The active requirement is **1,000 distinct usable STEP sources, ten genuinely
different STL tessellations each**, plus private personal regression anchors.
The smoke corpus is a workflow check, never the requested full baseline.
Reference geometry is for grading only. Equivalent final shape and surfaces
do not imply recovery of the original feature history.

## Fixture construction

`tools/build_multires_corpus.py` materializes an installed licensed collection
serially, resumes from per-source records, supervises native exports, records
excluded fixtures, and stops before consuming the last 15 GiB. Numerical thread
pools are limited to one and Windows processes run below normal priority.

The installed Fusion 360 Gallery s2.0.1 extended collection has 42,912 STEP
files. Its local LICENSE.md was read: use is restricted to non-commercial
research. Neither source geometry nor derived geometry is to be published.
Source: https://github.com/AutodeskAILab/Fusion360GalleryDataset.

Ten absolute linear/angular OCCT presets range from 0.20 mm / 0.65 rad to
0.004 mm / 0.045 rad. Cached triangulations are cleared between exports.
Headers, ordering and winding do not make meshes distinct. All ten must have
different triangle geometries and valid watertight consistently wound meshes.
Purely planar models often cannot meet this condition and are excluded.
This intentionally biases the new corpus toward curved geometry; existing
planar successes remain regression anchors. Ten inputs are ten resolutions
of one design, not ten independent designs.

Identical source bytes and collisions of volume/area/principal-inertia
signatures are conservatively excluded. This screen can discard different
shapes with the same signature; it is not a proof of shape equivalence.
Dataset design families stay together in deterministic development (60%),
validation (20%) and holdout (20%) splits. Historical development membership
must be excluded from holdout before freezing the full cohort.

## Evaluation and gates

`tools/benchmark_multires_corpus.py` uses a separate converter process receiving
only a neutral STL path/name, engine and units. It then grades the result in
the parent against the reference STEP. No reference path, object, labels or
manifest is passed to conversion. This is process/data-flow separation, not
an OS filesystem access-control boundary; an OS-enforced reference sandbox
remains unverified.

The original strict no-facet and geometry gates remain. Additional reference
checks compare symmetric sampled and local deviations, exact B-rep volume,
dimensions, and solid/shell counts. Reference surface meshes use absolute
deflection at most 0.002 mm; sampled deviations are measurements, not a
certified Hausdorff bound. Shape history is not scored. Per-quality and
all-ten-per-source outcomes are reported separately. Partial runs explicitly
remain partial. Cache identities include geometry, settings, source and
runtime; original full-run evidence must never be overwritten by a candidate.

## Experiments and retained evidence

- `benchmarks/hillclimb-20261112/`: frozen private personal manifests (10
  development, 46 holdout entries) and 73 file hashes. The initial baseline
  was interrupted to prioritize the explicit 10,000-input requirement after
  two completed non-faceted geometry passes. This is not a ten-case result.
- `benchmarks/multires-smoke/`: one genuine public source and ten distinct
  exports; first full-pipeline smoke evaluation completed with 10/10 strict
  and reference geometry passes. One source model is not a corpus rate.
- `benchmarks/multires-1000/`: full 1,000-source acquisition/export is resumable
  serially. Source byte hashes, signature exclusions, preset meshes and split
  membership are retained locally. Neither 1,000 sources nor the 10,000-case
  baseline is complete yet. The measured reconstruction gain below is limited
  to selected development cases, not a corpus-wide estimate.
- Eight new tests cover mesh identity, real ten-way curved exports, rejection
  of unchanged planar exports, partial-count reporting, identical reference
  acceptance and rejection of displaced/extra geometry. Related benchmark
  tests also pass; repository lint passes.

## Next work

Leo explicitly steered this on November 12: balance acquisition, speed and
reconstruction improvements progressively; do not wait for all inputs or run
all 10,000 before starting improvements. Acquisition was stopped with completed
records preserved so it no longer occupies the sole local evaluation slot.

`tools/select_multires_cohort.py` freezes a development-only inner cohort from
complete source bundles. It balances solid-count/surface-type strata and picks
lower triangle cost within each stratum, without looking at solve outcomes.
`benchmarks/multires-1000/inner-six.json` contains six source models and three
presets (coarse, middle, fine), 18 cases. This is selected development evidence,
not an unbiased corpus estimate. Personal difficult anchors remain additional
regressions. Other resolutions of the same sources are not independent holdout.

V9 aligns closed residual cross-sections and their periodic parameters, then
shares each complete fitted boundary with its planar neighbor. The matched
13-case inner cohort improves from 4 to 5 strict passes. Seven additional
exports of the affected source retain 4 strict passes; together these are
20 distinct cases from five source models, improving 8 to 9 strict passes.
The affected source improves 5/10 to 6/10 exports; its all-ten source-model
success remains false. Three private development anchors retain their prior
geometry outcomes, including two non-faceted successes. No holdout tuning or
gate/budget changes occurred. Failed V1 through V8 evidence remains local.

Expand checks after a
candidate earns them: targeted failure plus passing controls, all ten variants
of affected development sources, then broader validation and justified full
corpus evaluation. Resume fixture acquisition in separate serial batches
between experiments, preserving the 1,000-source / 10,000-case objective.
Never call partial or cached results a new complete baseline.
Keep known personal-model segmentation, periodic trimming, fragmented fillets,
and dense profile breakpoint retention as diagnostic categories. Change one
shared cause at a time, evaluate matched development cases and existing
successes, and promote/reject/inconclusive without changing the gates or
budgets. Validation informs promotion; do not tune against holdout failures.
Keep rejected experiment diffs and results locally. PRs contain only reviewed
source, tests and public documentation, never private manifests. Leo authorized
delivery through qualified CI, ready-for-review, merge and merged-SHA CI;
initial draft status is not a final delivery gate.

## Product rubric

1. Import an original STL, choose correct units, see dimensions before convert.
2. Convert with truthful progress, bounded completion and actionable failure.
3. Inspect input and delivered STEP with identical rotate/pan/zoom/fit controls;
   check holes, thin walls, blends and disconnected bodies visually.
4. Quality display distinguishes geometry verification, strict surface recovery
   and every full/partial faceted fallback. No invented quality score.
5. Export STEP, independently reopen every transfer root, verify topology and
   geometry, then reopen the saved run in the product.

The V9 success flow was exercised in the native browser: original STL import,
millimetre dimensions, live progress, input/STEP inspection, projection and
wheel zoom, export, independent STEP reimport, and saved-run reopen. Both
saved input and STEP were visible after reopening. Screenshots and downloaded
STEP hash are retained locally in `benchmarks/multires-1000/product-v9-runs/`.
The export caption now avoids calling fitted B-splines analytic. Right-drag
orbit and middle-drag pan were not driven by the available native drag API;
fallback and malformed-input browser flows remain unverified in this run.
Record those limitations rather than awarding a product quality score.
