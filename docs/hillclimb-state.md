# Demeshify hill climb

## Latest qualification, November 12

November 13 continuation: reference-only failure retention is merged in
[PR #11](https://github.com/LeoHougaard/demeshify/pull/11), exact head
`c84f7ac864a1a251bc944f1333c7f81105db96dc`, main
`fdb8fda9f6d22d8a24d49f37e8d147492b2f3895`. Both passed CI
([head](https://github.com/LeoHougaard/demeshify/actions/runs/43341322727),
[merge](https://github.com/LeoHougaard/demeshify/actions/runs/43343429599)).
The latest composed snapshot is 819 sources / 8,190 inputs, explicitly partial.
Acquisition proceeds in bounded batches between matched reconstruction tests.
Frozen converter bytecode passed an
actual ten-export AppContainer/reference-grading workflow for one source; see
[the artifact tool and limits](frozen-converter-artifacts-2026-11-13.md).
Production sandbox integration, a frozen grader and the full corpus/baseline
remain unfinished. V22 through V27 are not promoted; verified reconstruction stays
V21. V24 sped up failures but replaced a useful coarse hybrid with all facets.
Its source, tests, patch, matched results and diagnostic STEP remain local.
V25/V26 recover supported analytic regions but do not make a valid solid.
V27 normalizes the converter's own STEP into a valid solid, but the unchanged
deviation gate rejects it (1.259 mm maximum, 0.954 mm local maximum).
All retain the two passing controls in the matched three-case cohort. Actual
cylindrical trims that fail tessellation are the next diagnostic category;
neither valid topology alone nor zero facets establishes reconstruction success.

V21 is merged in [PR #9](https://github.com/LeoHougaard/demeshify/pull/9).
Exact head `04ae353d7d8b51ee0889f36fc89298b0f11919bd` and main merge
`50abe20631110980f0113260d0caac140777eab8` passed repository CI:
[head run](https://github.com/LeoHougaard/demeshify/actions/runs/43332981593),
[merged run](https://github.com/LeoHougaard/demeshify/actions/runs/43334033398).
The earlier snapshot at that milestone was 469 sources / 4,690 inputs, still partial.
Bounded case batches now support progressive evaluation; see
[the controller evidence](progressive-evaluation-cache-2026-11-12.md).
They are merged in [PR #10](https://github.com/LeoHougaard/demeshify/pull/10),
head `b064e669ceeddb7a855df3e7d528b8b9d0c1ad30`, main
`59d8774d04aeb232d5e906a70fe0b246ec4a42a9`; both passed CI
([merged run](https://github.com/LeoHougaard/demeshify/actions/runs/43338306959)).
Four additional development middle exports give three strict passes and one
analytic-worker timeout/faceted failure. Reference-only failure retention is
fixed and tested locally; see [the evidence](reference-failure-artifacts-2026-11-13.md).

V21 is locally qualified: truthful planar fallback reporting, complete planar
patch recovery and canonical open-extrusion junctions. Source SHA-256 is
`299ef8c7f98f5e8e8ee7f1b600513f7cb7c67a94ecb8eb247a40fdb229d8499e`.
The corrected V15 baseline is 12/20 strict, rather than its historically reported
13/20: one coarse output had 132 unreported source facets. V21 genuinely recovers
that case as 19 CAD faces and zero retained facets, giving 13/20 with no lost pass
and 20/20 geometry across five designs. Nine separate validation cases and three
private geometry controls retain their outcomes; all 303 tests and Ruff pass.
Both inspected target STEP files retain the reference's through-hole topology.
The tiny coarse hole remains polygonal. See
[the matched evidence and commands](canonical-planar-recovery-2026-11-12.md).

The full 1,000-source / 10,000-input
baseline, production OS isolation and additional cross-model hill climbing
remain unfinished. Earlier sections below preserve the experiment history;
their old strict counts and smaller fixture totals are superseded by this note.

## Contract and current best

Started November 12, 2026 from reliability branch
`codex/stl-reliability-20261011`, `fb6c6f23ff3e53d7f0b86450b64299e4a17400a4`.
The registered annular-boundary candidate V9 is merged in PR #2. Its exact
head `1f4d35d` and main merge `724953f` both passed repository CI. The denser
interior extension V10 is merged in PR #3; both `a510c2c` and main `0bd4001`
passed CI. Periodic closed extrusion fitting V11 and serial preview export V12
are merged in PR #4; exact head `3d0edf6` and main merge `ac5e266` passed CI.
See `docs/periodic-extrusion-recovery-2026-11-12.md`.
V13 shared periodic rims are merged in PR #6, head `6ead82d`, main `06232b9`;
both passed CI. V15 local open-profile interpolation is the strongest locally
verified reconstruction candidate, improving the original development strict
count from 8 to 13 of 20 cases without losing a prior pass. Nine separate
validation cases and three private geometry controls retain their outcomes;
all 287 local tests pass. See `docs/open-profile-interpolation-2026-11-12.md`
for exact source, unchanged gates and remaining surface-quality limitations.
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

V10 separates boundary interpolation complexity from interior projection work,
retaining every support-node check within the existing graph fitter's support
bound. It recovers another development export previously represented by 1,246
facets. The affected source reaches 7/10 strict exports and still fails all-ten
source-model acceptance. The small through-hole was visually checked at five
depths. See the V10 report for unchanged gates, measured error/time/memory and
exact source hash. Acquisition's clean five-model batch reached 74/740 fixtures;
continue toward 1,000/10,000 in bounded batches between experiments. The next
observed category is zero-free-edge invalid trim shells, not another blanket
increase in fitting bounds. Inspect actual faces and shared curves first.

V11's actual shell diagnostic found 34 bad edge orientations around a small
closed extrusion. Retaining its profile nodes in a periodic fit recovered the
fine export and reduced conversion time 109.46 to 40.93 seconds, with greater
sampled memory (2.01 to 2.81 GB). The affected source reaches 8/10, still failing
all-ten acceptance. Its hole's Euler signature and five visual sections match
the reference. Fragmented trim chains remain: the delivered STEP has 1,011
edges. Share complete periodic rims next. The separate V12 resource correction
makes STL preview export explicitly serial while preserving the recovered
case's exact STEP data and 119,174-triangle preview geometry, all three matched
strict controls and both disconnected-body CAD export modes. See
`docs/serial-preview-export-2026-11-12.md`. Acquisition reached 99/990 usable inputs;
20 additional licensed KiCad STEP downloads are not counted until qualified.

The next bounded primary batch has reached 199 sources / 1,990 inputs. Five
licensed public KiCad sources add 50 qualified exports, including a two-solid
model. The composed, globally deduplicated partial corpus is **204 sources /
2,040 inputs**, explicitly incomplete. The predeclared final quotas are 980
Fusion sources plus 20 KiCad sources. The acquisition and composition tools
are merged in PR #5; head `ee9838f` and main merge `b83d60f` passed CI. Seven
further licensed public replacements have been acquired but do not count
until native qualification. Family groups were declared before conversion.

V13 reuses complete periodic extrusion rims on both planar caps and checks
every boundary node before replacing a trim chain. Its matched development
20 retain 11 strict / 20 geometry passes, validation nine retain nine strict
passes and private three retain three geometry passes / two smooth outputs.
The fine target drops from 1,011 to 497 delivered STEP edges with unchanged
maximum deviations; its volume error increases slightly within the same gate.
All 276 tests and Ruff pass. Independent STEP reopening retains the reference's
single-solid tunnel topology. V13 is locally promoted for trim complexity and
targeted resource gains; exact head `6ead82d` and merge `06232b9` passed CI. See
`docs/shared-extrusion-rims-2026-11-12.md`. The two unresolved exports retain
their original failures: the coarser one has a 1.34 mm fitted-geometry error;
the middle-fine one fails STEP round-trip validity after a residual C0 fill.
Inspect their actual trimmed geometry next, independently of the source CAD.

V14's repeated-node removal alone repaired the coarse spline fit, but failed
the unchanged 0.12 mm reference dimension gate and was not promoted for strict
recovery. V15's local open interpolation also recovers the remaining invalid
middle-fine export. The matched 20 improve 11 to 13 strict, geometry remains
20/20, and the affected source improves eight to ten accepted exports. This
is one fully evaluated source among five development designs, not a full-corpus
model rate. Coarse planar caps remain fragmented and the small coarse hole is
visibly polygonal; full original smooth-feature semantics are not established.
Independent STEP reopening and sections preserve the tunnel for both recoveries.

The current composed fixture snapshot has **304 distinct sources / 3,040 STL
inputs**, explicitly partial, after a clean 100-new-source primary batch.
Progressive evaluation now reuses identical cases with frozen code/runtime/input
identity and keeps raw attempts separate from certified cache records. The
controller milestone is merged in PR #7; head `09489ca` and main `271cdc6`
passed CI. See `docs/progressive-evaluation-cache-2026-11-12.md`.

A task-specific per-user/per-app Windows AppContainer profile and native
file-access probe have been established. Access grants were limited to its
new job folder, with no changes to existing app permissions or global settings.
The probe can write its job and cannot read the grader sentinel. Actual Python
conversion and native STEP-denial proof remain unverified; do not claim the full
reference-isolation requirement is complete yet.

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
