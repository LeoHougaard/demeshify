# Surface reconstruction failure ledger

This file records failure mechanisms, not just failing filenames. Update it
after every benchmark cycle with `tools/analyze_surface_failures.py` and retain
the raw JSON report beside the benchmark.

## Current representative corpus

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

## Failure mechanisms observed during development

| Mechanism | Evidence | Common fix | Status |
| --- | --- | --- | --- |
| Open fitted boundaries | Positive sewing free-edge count | Construct one canonical 3D edge and shared endpoint vertices for both faces; fill only genuinely closed residual loops | Implemented |
| Edge-closed but invalid shell | Zero free edges with unorientable or self-intersecting wires | Check oriented-shell diagnostics and repair face/wire orientation before `MakeSolid` | Export correctness implemented; editable analytic torus repair remains |
| STEP-only invalidity | Kernel-valid solid fails export/import | Treat STEP round-trip as an acceptance gate; do not trust in-memory validity alone | Implemented |
| Wrong periodic complement | Large torus/plane face area after trimming | Validate trim area against source patch area and reject arbitrary seam changes | Implemented; robust torus p-curve rebuild remains |
| Over-aggressive surface absorption | Expected transition faces disappear into neighboring primitives | Preserve residual ownership unless every node satisfies the analytic equation; avoid topology-blind label smoothing | Implemented |
| Over-aggressive global snapping | Local point fit passes but distant intersections move materially | Use relation-specific, one-tenth-tolerance gates and disable plane-only perfecting on dense tangent networks until surfaces and edges can be solved jointly | Implemented |
| Decimal parameter rounding | Values such as one third drift or unrelated dimensions become equal | Use bounded rational relation hypotheses and accept only if all supporting nodes remain inside tolerance | Implemented for plane offsets |
| Native OCCT failure | Plate fitting or periodic face division terminates in native code | Bound solver inputs, prefer analytic/UV construction, isolate corpus cases in subprocesses with timeouts | Implemented |
| Residual B-spline cracks | Independently fitted freeform supports disagree at their shared boundary | Fit boundary-constrained surfaces using the same 3D edge and separate surface p-curves | Partial |
| Straight facet against curved analytic trim | Triangle chord does not lie on the neighboring cylinder/cone/sphere/torus | Split the analytic trim at source nodes, project boundary vertices, and rebuild touching triangles as C0 curved fillings | Implemented and STEP round-trip tested |
| Sweep regression from over-broad conforming split | Extrusions/revolutions were mistaken for residuals, producing 772 free edges in `24168_a0ede173_146` | Apply border deformation only to actual freeform/fallback ownership; preserve recognized sweep trims | Fixed in the following benchmark cycle |
| Native process termination | OCCT exits with access violation before Python can catch an exception | Run analytic reconstruction in a bounded child process; recover in the parent from the untouched STL | Implemented and exercised by six previous crash cases |
| Full analytic result fails validation | Open shell, invalid solid, excessive surface error, or invalid STEP round-trip | Build a shell directly from one vertex and one edge per source mesh entity; do not infer connectivity by sewing independent triangle faces | Implemented; 22/60 holdout cases recovered |
| Dense fallback exceeds case budget | Retessellation and duplicate STEP imports consume most of the exchange time | Reuse the source STL for scoring and perform one STEP import in the scorer | Implemented; 63.5k-triangle outlier completes in 168 seconds |
| Partial cone split into local cylinders/extrusions | `27029_3fd2328f_8` produced 17 patches instead of the source's 12 | Recombine smooth oblique sweep fragments and accept the cone only after a stricter union-node fit | Recognition fixed: 8 planes, 3 cylinders, 1 cone |
| Tiny planar chord inside a cylinder | A two-triangle strip introduced an artificial face and trim crack | Absorb only adjacent tangent chords whose nodes and normals satisfy the cylinder at one-tenth tolerance | Implemented |
| Closed intersection crosses its parameter seam | Correct cone/cylinder intersection selected the long complementary arc | Split the exact intersection at the periodic seam and share the new seam vertex | Implemented |
| Tangent circular hole has the wrong edge seam | Touching planar wires were reported as intersecting because the circle used an arbitrary seam vertex | Move the periodic edge seam to the canonical multi-surface junction vertex | Implemented |
| UV-bounded cone becomes unorientable after sewing | `133589_40359674_2` had zero free edges, but one generated cone p-curve became a self-intersecting wire | Build full-angle cones on their mathematical support from the canonical shared intersection circles instead of substituting edges into a generated UV band | Fixed: 15-face analytic solid, valid STEP round-trip, 0.00000000000005 mm P95 |
| Circular runs hidden in a general extrusion | `71503_271b24c0_0` was 10 planes, 2 cylinders, 2 inaccurate B-spline extrusions | Find stable circumcenter/radius runs in the node profile, then validate the implied cylinders against every 3-D node and normal | Fixed: exact 14-plane/6-cylinder graph, watertight analytic STEP, 0.000005 mm P95 |
| Dense failed extrusion enters GeomPlate | A 1,538-face patch spent more than a minute in a native plate solve | Apply the same vertex, face, and boundary-complexity limits used for other residual types | Implemented |
| Smooth non-planar model over-segmented by local consensus | `78685_2fe9291a_2` grew from 10 useful swept patches to 43 fragments and reached an unsafe plate fit | Reserve aggressive local peeling for models with planar mechanical datums or very dense meshes | Fixed; watertight analytic STEP, 0.0125 mm P95 |
| Open/inconsistent source cannot use closed faceted carrier | Parent recovery raises when an analytic worker fails and the STL itself is not manifold | Repair or reconstruct source topology before carrier construction; do not label an open quilt watertight | Open limitation in the uncurated stress set |

## Next common fix

Correctness coverage on the curated corpus remains 60/60 and editable analytic
coverage is now 44/60 without weakening the acceptance gate. The first priority
is the remaining partially clipped periodic-cylinder/torus class: construct the
complete UV boundary as one coordinated wire, share its physical trim edges,
and validate both the in-memory solid and STEP round-trip. The separate scaling
priority is a topology-repair carrier for very dense open or inconsistent STL
inputs. Until those succeed, the exact faceted carrier is retained whenever the
source mesh can validly supply one.
