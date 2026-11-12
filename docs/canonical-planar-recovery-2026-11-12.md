# Complete planar caps and canonical extrusion junctions

## Reporting correction

The previous V15 result of 13/20 strict passes included a reporting error.
The coarse development export `5b7f2dd6f8dee0946745-q01` retained 132 source
triangles across seven recognized planar patches, while reporting zero fallback
facets. An instrumented reconstruction and its actual 144-face STEP confirmed
the path. Those faces cannot count as strict no-facet recovery.

V18 applies truthful facet counts, patch IDs and the inspection overlay to
this path without changing any grading threshold. Its fresh corrected baseline
is **12/20 strict, 20/20 geometry**, across five development source models.
The affected source passes nine of its ten exports. Baseline surface source
SHA-256: `4ba28b1ae7593cefe2b00615a1ac653011e15595341e5b7b96baef893592f825`.
Its exact source and all results remain in the local experiment archive.

## Reconstruction change

V21 recovers one complete planar face from the connected coplanar triangles
when ordinary trim construction fails. It copies the complete face complex
once, preserving shared topology, then uses OCCT domain unification. Adoption
requires one valid PLANE, area agreement within relative 1e-9, and every original
support node within 1e-7 mm of that plane. Native merging uses 1e-7 mm linear
and 1e-8 rad angular tolerances; the optional operation is bounded to 1,500
triangles. A failed merge retains and reports the facets.

Open extrusion bands can also share their four complete finite edges with
neighboring planar faces. All boundary nodes, finite endpoints and neighboring
planes must validate, and all four borders must match together. Their junctions
reuse the global canonical vertices, preserving each replacement's orientation.
Coincident coordinates alone had left different vertex identities at three-face
junctions: the preceding edge-only V16 candidate lost a previously passing
export and was rejected. Closed periodic rims retain their existing behavior.

No reference STEP, source model name or grading manifest enters reconstruction.
No fit budget, conversion deadline or acceptance threshold is relaxed. The
preceding profile fitting, including its node budgets and local interpolation,
is unchanged.

## Matched evidence

The frozen development cohort contains 20 STL cases from five designs, including
all ten exports of the affected source. It is not an overall product success rate.

| Measurement | Corrected baseline | V21 |
| --- | ---: | ---: |
| Strict no-facet CAD passes | 12/20 | 13/20 |
| Geometry passes | 20/20 | 20/20 |
| Previously passing cases lost | — | 0 |
| Affected source's accepted exports | 9/10 | 10/10 |
| Coarse target's STEP faces | 144 | 19 |
| Coarse target's retained facets | 132 | 0 |
| Coarse reference volume error | 0.206384% | 0.075540% |
| Coarse reference dimension error | 0.073259 mm | 0.004048 mm |
| Second target's reference volume error | 0.146960% | 0.055540% |
| Second target's reference dimension error | 0.062572 mm | 0.007974 mm |

The coarse target's maximum reference deviation remains 0.045913 mm; local
maximum improves 0.053239 to 0.045921 mm. The second target retains its maximum
and local deviations and produces 14 faces instead of 15. Total measured
case wall time changes 525.49 to 470.08 seconds in this single matched run.
This is not a general speed claim. Per-case latency and sampled process-tree
memory remain in the retained results.

Nine separate validation cases retain nine strict passes with truthful counts.
Three private anchors retain three geometry passes and the same two smooth
outputs; the difficult coarse anchor still uses 1,220 facets. Some private
anchors lack reference CAD and are not strict reference passes.

Freshly captured target STEP files independently reopen as valid single solids
with 19 and 14 faces, respectively. Their shell Euler signatures match the
reference's through-hole topology, `[[0]]`. The exact inspected files also pass
independent reference grading; those artifact-only grades are explicitly marked
as reused conversions and are not additional corpus cases.

- Coarse STEP SHA-256: `35a1ba97c49f4155088fa8cebb998b06a365ef4815fda3abcd7994600ef05449`
- Second STEP SHA-256: `d758a7eb5361e3634a31e819eda5f372705f3d6a712341e04e3fce62fbc6e3dd`

Whole-profile, upper-curve and through-hole sections were visually inspected.
The tiny coarse hole remains visibly polygonal. These results establish the
stated gates and topology, not complete original smooth-feature semantics or
feature history. The earlier fine q06 recovery remains a valid 22-face solid.

All **303 tests pass**, three warnings, in 254.01 seconds. Ruff passes. New tests
cover whole-plane recovery and STEP reopening at two scales, a preserved
through-hole, an unsupported source corner, truthful fallback counts/overlay
when merging fails, and shared finite edges/vertex identity with failure controls.

## Reproduction and continuation

Candidate `app/surface_brep.py` SHA-256:
`299ef8c7f98f5e8e8ee7f1b600513f7cb7c67a94ecb8eb247a40fdb229d8499e`.
Evaluation launched from `bf3970973ab534078c7113aa59e3525a4666f516` with the
candidate working source; contracts record the actual application/evaluator,
input and runtime hashes. The interpreter was Python 3.12.13 with CadQuery 2.8.0,
OCP 7.9.3.1.1, NumPy 2.4.6 and Trimesh 4.12.2. Numerical pools were one.
Geometry and evidence remain local; licensed and private fixtures are not published.

```powershell
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/open-rims-v17-three.json --output benchmarks/multires-1000/experiments/canonical-planar-recovery-v21-three.json --artifacts benchmarks/multires-1000/experiments/canonical-planar-recovery-v21-artifacts --fresh
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/open-rims-v17-remaining17.json --output benchmarks/multires-1000/experiments/canonical-planar-recovery-v21-remaining17.json --artifacts benchmarks/multires-1000/experiments/canonical-planar-recovery-v21-artifacts --fresh
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/validation-nine-frozen.json --output benchmarks/multires-1000/experiments/canonical-planar-recovery-v21-validation9.json --artifacts benchmarks/multires-1000/experiments/canonical-planar-recovery-v21-validation-artifacts --fresh
```

Private controls, retained-output grading, images and the full test log are under
`benchmarks/multires-1000/experiments/` and `inspection-canonical-planar-v21-*`.
The rejected/provisional V16–V20 sources, patches and failures remain intact.

The latest composed snapshot has **359 distinct STEP models / 3,590 STL inputs**,
334 family groups, explicitly incomplete. Another bounded acquisition batch is
running. The requested 1,000-source / 10,000-input frozen baseline is unfinished;
no full-corpus recovery rate is claimed. Production OS reference isolation and
stronger topology/fixture grading remain work before that baseline. The next
development cohort contains four additional preselected, untested families;
holdout outcomes have not been used for tuning.
