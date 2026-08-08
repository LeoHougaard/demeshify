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
- Representation: 38 models passed as analytic/B-spline reconstructions; 22
  required the exact source-topology faceted B-rep fallback.

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

## Next common fix

Correctness coverage is now 60/60, so the next target is raising editable
analytic coverage above 38/60 without weakening the acceptance gate. The first
priority remains a coordinated periodic-face rebuild: choose a torus parameter
seam, rebuild every affected trim loop and p-curve together, and validate both
the in-memory solid and STEP round-trip before accepting it. Until that succeeds,
the exact faceted carrier is retained instead of exporting an invalid analytic
shell.
