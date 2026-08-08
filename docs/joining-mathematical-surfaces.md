# Joining fitted mathematical surfaces

## Decision

Use a surface-first, topology-aware, globally constrained B-rep reconstruction.
Do not round independently fitted coefficients. Decimal rounding is coordinate
dependent, can worsen the point fit, and does not guarantee common intersections
or tangency.

The reconstruction order is:

1. Segment mesh nodes and fit candidate support surfaces.
2. Infer only high-confidence design relations: coplanar, parallel,
   perpendicular, coaxial, concentric, equal radius, and tangent.
3. Jointly perfect surface parameters by minimizing node error, constraint
   error, and movement from the independent fits.
4. Solve canonical vertices from all incident support surfaces.
5. Intersect each adjacent pair of perfected surfaces once to create one
   canonical 3D edge, trimmed between canonical vertices.
6. Build each face on its support surface from those shared edges. The same
   OpenCascade edge and vertex topology must be reused by both incident faces.
7. For a freeform face, construct its support surface from canonical boundary
   curves plus interior point constraints. Store a separate p-curve for the
   shared 3D edge on each incident support surface and enforce SameParameter.
8. Sew only for final healing and verification, then validate manifold edge
   incidence and STEP export/import.

The joint perfecting objective should have the form

`node_fit_error + relation_weight * constraint_error + movement_weight * parameter_change`.

Relations must be confidence-gated. A nearly parallel pair should not be forced
parallel unless the change remains inside the reconstruction tolerance and is
supported by enough nodes.

## Why this approach

- Siemens D-Cubed 3D DCM is a geometric constraint solver. Its relevant model is
  constraint solving over geometry, not coefficient rounding:
  <https://www.siemens.com/en-gb/products/plm-components/d-cubed/3d-dcm/>
- Point2CAD fits surfaces from segmented points, obtains edges from pairwise
  surface intersections, and obtains corners from edge intersections:
  <https://openaccess.thecvf.com/content/CVPR2024/papers/Liu_Point2CAD_Reverse_Engineering_CAD_Models_from_3D_Point_Clouds_CVPR_2024_paper.pdf>
- Kovacs, Varady, and Salvi describe constrained perfecting after primary
  surface fitting and before final B-rep topology/stitching:
  <https://3dgeo.iit.bme.hu/papers/reverse/constraints.pdf>
- ComplexGen represents vertices, edges, faces, and their incidences together,
  then applies global structural and geometric optimization:
  <https://haopan.github.io/papers/ComplexGen.pdf>
- OpenCascade sewing identifies contiguous boundaries; it is not a replacement
  for constructing common edges:
  <https://dev.opencascade.org/doc/refman/html/class_b_rep_builder_a_p_i___sewing.html>
- OpenCascade GeomPlate supports point/curve constrained surfaces and G0/G1/G2
  boundary continuity for residual freeform and blend patches:
  <https://dev.opencascade.org/doc/refman/html/class_geom_plate___build_plate_surface.html>

## Current implementation

`app/surface_brep.py` now clusters boundary-chain endpoints, solves one
least-movement corner against all incident analytic equations, constructs one
OpenCascade vertex per corner, and reuses one surface-intersection edge for both
adjacent faces. The report exposes canonical vertex and edge counts.

The remaining open boundaries on the Dragon Mount test are predominantly on
node-fitted B-spline faces. Correctly eliminating those gaps requires building
the two surface-specific p-curves for each shared 3D edge as one coordinated
operation; repeatedly repairing one shared edge face-by-face invalidates the
previous face and is deliberately not used.
