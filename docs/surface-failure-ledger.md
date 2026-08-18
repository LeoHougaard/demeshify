# Surface reconstruction failure ledger

This file records failure mechanisms, not just failing filenames. Update it
after every benchmark cycle with `tools/analyze_surface_failures.py` and retain
the raw JSON report beside the benchmark.

## Prior verified baseline

The strict Fusion holdout gate now accepts 60 of 60 models:

- Families: chamfer, fillet, fillet/chamfer, single- and multi-extrude,
  revolve, and mixed revolve/extrude.
- Complexity: 21 simple, 23 medium, and 16 complex models.
- Results: 60 complete, 60 valid B-reps, 60 watertight solids, 60 valid STEP
  export/import round-trips, zero free edges, and 100% geometric acceptance.
- Representation: 44 models passed as analytic/B-spline reconstructions; 16
  required the exact source-topology faceted B-rep fallback. This count uses
  the clean 60-case run plus sequential reruns of cases that were outside that
  manifest slice or contended for OpenCascade resources.

An additional uncurated first-60 manifest stress run accepted 50/60. Six cases
exceeded a four-minute outer limit and four could not use the closed faceted
carrier because their processed STL topology was open or inconsistent; one of
those inputs contains 2.66 million triangles. These are recorded as scaling and
source-repair limitations, not counted as successful conversions.

The former periodic-torus failure `137997_9a5922be_0` also passes separately as
a watertight faceted STEP solid with zero free edges and P95 error below
0.000000000001 mm. Its analytic periodic p-curve reconstruction remains a
quality/editability improvement target, but it is no longer an export failure.

## 2026-08-18 exact-manifest verification

The current benchmark has separate strict geometric and clean-analytic gates.
A result passes the geometric gate only when it is a closed, valid solid with
zero free edges, acceptable error, and a valid STEP re-import. A faceted carrier
or hybrid can pass that correctness gate, but never the clean-analytic gate.

- Curated `--max-triangles 50000`: `improvement_curated60_uv_v2.json` is
  60/60 geometric, 60 valid STEP re-imports, zero free edges, zero timeouts,
  and zero crashes. It contains 43 `complete` and 17 `best_effort` results:
  41/60 pass the current clean-analytic representation gate, 15 use a full
  source-topology carrier, and two are explicitly rejected hybrid results with
  residual facets. The 41/60 value is intentionally stricter than the older
  44/60 analytic/B-spline ledger count, which combined a clean run with
  sequential reruns and predated the current representation-quality fields.
- Uncurated `--limit 60`: `improvement_stress60_uv_v2.json` improves strict
  correctness from 50/60 to 53/60. Timeouts fall from six to four and crashes
  from four to three. All 53 successes have valid STEP re-imports and zero free
  edges; 35 pass the clean-analytic gate, 16 use a full carrier, and one
  (`78685_2fe9291a_2`) is an explicitly rejected hybrid with residual facets.
- Case-P95 error percentiles in millimetres are shown below. The slight stress
  P95 increase is caused by admitting three additional valid cases, not by a
  relaxed tolerance; the per-case acceptance limit remains unchanged.

| Corpus | Run | P50 | P90 | P95 | P99 | Maximum |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Curated | before (`surface_holdout60_v52.json`) | 0.000000480 | 0.005760 | 0.014229 | 0.039498 | 0.045105 |
| Curated | after | 0.000057921 | 0.000891 | 0.001254 | 0.025601 | 0.045346 |
| Stress | before (`surface_holdout60_v71.json`, 50 valid) | 0.000051604 | 0.001144 | 0.002184 | 0.024883 | 0.045346 |
| Stress | after (53 valid) | 0.000031940 | 0.000902 | 0.002322 | 0.027944 | 0.045346 |

The sequential one-worker after runs took 1,950.3 seconds curated and 3,423.138
seconds stress. The older JSON schema did not record wall or per-case runtime,
so an exact before/after runtime ratio cannot be recovered without rerunning the
old commit; filesystem timestamps are not treated as benchmark evidence.

Focused evidence is retained in `improvement_periodic_regression4.json` (4/4
known periodic cases remain clean analytic),
`improvement_recovery_regression3.json` (3/3 strict-valid recovery cases), and
the exact-run artifact directories under `benchmarks/artifacts/`. In the real
viewer, the recovered `78685` STEP result matched the source silhouette and
through-hole without a visible open seam, while residual triangle regions and
the `REJECTED: FALLBACK GEOMETRY` label remained clearly exposed.

## Failure mechanisms observed during development

| Mechanism | Evidence | Common fix | Status |
| --- | --- | --- | --- |
| Open fitted boundaries | Positive sewing free-edge count | Construct one canonical 3D edge and shared endpoint vertices for both faces; fill only genuinely closed residual loops | Implemented |
| Edge-closed but invalid shell | Zero free edges with unorientable or self-intersecting wires | Check oriented-shell diagnostics and repair face/wire orientation before `MakeSolid` | Export correctness implemented; editable analytic torus repair remains |
| STEP-only invalidity | Kernel-valid solid fails export/import | Treat STEP round-trip as an acceptance gate; do not trust in-memory validity alone | Implemented |
| Wrong periodic complement | Large torus/plane face area after trimming | Validate trim area against source patch area and reject arbitrary seam changes | Implemented; robust torus p-curve rebuild remains |
| Clipped periodic p-curve self-intersects | A single high-degree closed UV interpolation is invalid although every source boundary interval is valid | Build one coordinated UV wire from exact per-interval surface segments, then apply the existing area and validity gates | Implemented and regression-tested for a dense clipped torus |
| Periodic faces remain mutually cracked | Individually valid clipped torus/cylinder faces leave 15 free edges in full `147141_495ac591_0` reconstruction | Reuse one canonical 3-D trim edge with separate p-curves on both adjacent supports | Open; full result correctly falls back |
| Over-aggressive surface absorption | Expected transition faces disappear into neighboring primitives | Preserve residual ownership unless every node satisfies the analytic equation; avoid topology-blind label smoothing | Implemented |
| Over-aggressive global snapping | Local point fit passes but distant intersections move materially | Use relation-specific, one-tenth-tolerance gates and disable plane-only perfecting on dense tangent networks until surfaces and edges can be solved jointly | Implemented |
| Decimal parameter rounding | Values such as one third drift or unrelated dimensions become equal | Use bounded rational relation hypotheses and accept only if all supporting nodes remain inside tolerance | Implemented for plane offsets |
| Native OCCT failure | Plate fitting or periodic face division terminates in native code | Bound solver inputs, prefer analytic/UV construction, isolate corpus cases in subprocesses with timeouts | Implemented |
| Residual B-spline cracks | Independently fitted freeform supports disagree at their shared boundary | Fit boundary-constrained surfaces using the same 3D edge and separate surface p-curves | Partial |
| Straight facet against curved analytic trim | Triangle chord does not lie on the neighboring cylinder/cone/sphere/torus | Split the analytic trim at source nodes, project boundary vertices, and rebuild touching triangles as C0 curved fillings | Implemented and STEP round-trip tested |
| Sweep regression from over-broad conforming split | Extrusions/revolutions were mistaken for residuals, producing 772 free edges in `24168_a0ede173_146` | Apply border deformation only to actual freeform/fallback ownership; preserve recognized sweep trims | Fixed in the following benchmark cycle |
| Native process termination | OCCT exits with access violation before Python can catch an exception | Run analytic reconstruction in a bounded child process; recover in the parent from the untouched STL | Implemented and exercised by six previous crash cases |
| Full analytic result fails validation | Open shell, invalid solid, excessive surface error, or invalid STEP round-trip | Preserve the failed analytic artifacts for diagnosis, then build a shell directly from one vertex and one edge per source mesh entity | Implemented; invalid child reports can no longer replace a verified carrier |
| Dense fallback exceeds case budget | Retessellation and duplicate STEP imports consume most of the exchange time | Reuse the source STL for scoring and perform one STEP import in the scorer | Implemented; 63.5k-triangle outlier completes in 168 seconds |
| Partial cone split into local cylinders/extrusions | `27029_3fd2328f_8` produced 17 patches instead of the source's 12 | Recombine smooth oblique sweep fragments and accept the cone only after a stricter union-node fit | Recognition fixed: 8 planes, 3 cylinders, 1 cone |
| Tiny planar chord inside a cylinder | A two-triangle strip introduced an artificial face and trim crack | Absorb only adjacent tangent chords whose nodes and normals satisfy the cylinder at one-tenth tolerance | Implemented |
| Closed intersection crosses its parameter seam | Correct cone/cylinder intersection selected the long complementary arc | Split the exact intersection at the periodic seam and share the new seam vertex | Implemented |
| Tangent circular hole has the wrong edge seam | Touching planar wires were reported as intersecting because the circle used an arbitrary seam vertex | Move the periodic edge seam to the canonical multi-surface junction vertex | Implemented |
| UV-bounded cone becomes unorientable after sewing | `133589_40359674_2` had zero free edges, but one generated cone p-curve became a self-intersecting wire | Build full-angle cones on their mathematical support from the canonical shared intersection circles instead of substituting edges into a generated UV band | Fixed: 15-face analytic solid, valid STEP round-trip, 0.00000000000005 mm P95 |
| Circular runs hidden in a general extrusion | `71503_271b24c0_0` was 10 planes, 2 cylinders, 2 inaccurate B-spline extrusions | Find stable circumcenter/radius runs in the node profile, then validate the implied cylinders against every 3-D node and normal | Fixed: exact 14-plane/6-cylinder graph, watertight analytic STEP, 0.000005 mm P95 |
| Dense failed extrusion enters GeomPlate | A 1,538-face patch spent more than a minute in a native plate solve | Apply the same vertex, face, and boundary-complexity limits used for other residual types | Implemented |
| Smooth non-planar model over-segmented by local consensus | `78685_2fe9291a_2` grew from 10 useful swept patches to 43 fragments and reached an unsafe plate fit | Reserve aggressive local peeling for models with planar mechanical datums or very dense meshes | Improved to a watertight hybrid STEP with 0.01188 mm P95; two residual faceted regions remain and are not analytic success |
| Open/inconsistent source cannot use closed faceted carrier | Parent recovery raises when an analytic worker fails and the STL itself is not manifold | Repair or reconstruct source topology before carrier construction; do not label an open quilt watertight | Improved for `78685`; still open for `104995`, `66762`, and `51913` |

## Next common fix

Correctness coverage on the curated corpus remains 60/60 without weakening the
acceptance gate, and the exact stress run improves from 50/60 to 53/60. The
coordinated periodic UV wire is implemented; the next periodic step is sharing
its physical 3-D trim edges between adjacent analytic supports so `147141` does
not retain 15 free edges before recovery. The separate scaling priority is a
topology-repair carrier for the three remaining open/inconsistent stress inputs,
plus the four dense cases that still exceed 240 seconds. Until those mechanisms
succeed, invalid fitted shells remain diagnostic-only and a faceted carrier is
retained only when the source mesh can validly supply one.
