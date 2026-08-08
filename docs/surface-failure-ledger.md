# Surface reconstruction failure ledger

This file records failure mechanisms, not just failing filenames. Update it
after every benchmark cycle with `tools/analyze_surface_failures.py` and retain
the raw JSON report beside the benchmark.

## Current representative corpus

Five of six representative models export as valid watertight STEP solids. The
remaining case is `137997_9a5922be_0`:

- Recognition: 41 analytic supports (14 planes, 15 cylinders, 12 tori), with
  every input triangle assigned and P95 geometric error 0.0302 mm.
- Trimming: one cylinder and two planes require node-fitted B-spline fallbacks;
  one closed residual loop requires a C0 fill face.
- Topology: sewing reports zero free edges, but one small plane/torus boundary
  is used with the same orientation by both incident faces.
- In-memory repair: reversing that one occurrence produces a kernel-valid
  solid, but changes the periodic torus p-curve semantics.
- STEP exchange: export/import selects the complementary torus domain, splits
  two torus faces, and returns an unorientable invalid solid.
- Current behavior: correctly return `best_effort`; never claim watertightness.

## Failure mechanisms observed during development

| Mechanism | Evidence | Common fix | Status |
| --- | --- | --- | --- |
| Open fitted boundaries | Positive sewing free-edge count | Construct one canonical 3D edge and shared endpoint vertices for both faces; fill only genuinely closed residual loops | Implemented |
| Edge-closed but invalid shell | Zero free edges with unorientable or self-intersecting wires | Check oriented-shell diagnostics and repair face/wire orientation before `MakeSolid` | Implemented, periodic torus case remains |
| STEP-only invalidity | Kernel-valid solid fails export/import | Treat STEP round-trip as an acceptance gate; do not trust in-memory validity alone | Implemented |
| Wrong periodic complement | Large torus/plane face area after trimming | Validate trim area against source patch area and reject arbitrary seam changes | Implemented; robust torus p-curve rebuild remains |
| Over-aggressive surface absorption | Expected transition faces disappear into neighboring primitives | Preserve residual ownership unless every node satisfies the analytic equation; avoid topology-blind label smoothing | Implemented |
| Over-aggressive global snapping | Local point fit passes but distant intersections move materially | Use relation-specific, one-tenth-tolerance gates and disable plane-only perfecting on dense tangent networks until surfaces and edges can be solved jointly | Implemented |
| Decimal parameter rounding | Values such as one third drift or unrelated dimensions become equal | Use bounded rational relation hypotheses and accept only if all supporting nodes remain inside tolerance | Implemented for plane offsets |
| Native OCCT failure | Plate fitting or periodic face division terminates in native code | Bound solver inputs, prefer analytic/UV construction, isolate corpus cases in subprocesses with timeouts | Implemented |
| Residual B-spline cracks | Independently fitted freeform supports disagree at their shared boundary | Fit boundary-constrained surfaces using the same 3D edge and separate surface p-curves | Partial |
| Straight facet against curved analytic trim | Triangle chord does not lie on the neighboring cylinder/cone/sphere/torus | Split the analytic trim at source nodes, project boundary vertices, and rebuild touching triangles as C0 curved fillings | Implemented and STEP round-trip tested |
| Sweep regression from over-broad conforming split | Extrusions/revolutions were mistaken for residuals, producing 772 free edges in `24168_a0ede173_146` | Apply border deformation only to actual freeform/fallback ownership; preserve recognized sweep trims | Fixed in the following benchmark cycle |

## Next common fix

The next priority is a coordinated periodic-face rebuild. It must choose a
torus parameter seam, rebuild every affected trim loop and p-curve together,
then validate both the OpenCascade solid and a STEP export/import before the
change is accepted. Changing a surface seam or one edge orientation in
isolation is explicitly rejected because benchmark experiments showed that it
can select the complementary torus domain.

Hybrid residual facets now retain source mesh topology and reuse exact
analytic/faceted interface edges. The six-model mechanical regression corpus
remains at five accepted watertight STEP solids; the remaining periodic torus
exchange failure is unchanged. A dedicated mixed cylinder/organic-cap test
also verifies that conforming facets form a watertight solid and survive STEP
export/import without degrading the editable cylinder.
