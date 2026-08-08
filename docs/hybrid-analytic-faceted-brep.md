# Hybrid analytic/faceted reconstruction design

## Guarantee boundary

For a closed, consistently oriented, two-manifold STL, the converter can retain
the mesh as a master topology and guarantee that every source region has an
output representation. It cannot guarantee a watertight solid for an arbitrary
open or non-manifold STL without first repairing or changing that input
topology. That distinction must be reported rather than hidden.

## Representation hierarchy

Each connected surface region receives the highest representation that passes
both geometric and exchange validation:

1. Elementary analytic face: plane, cylinder, cone, sphere, or torus.
2. Analytic sweep: linear extrusion or surface of revolution.
3. Boundary-constrained B-spline/plate face.
4. Faceted residual carrier derived from the original mesh topology.

The fourth level is coverage, not the first reconstruction strategy. An
organic region can remain faceted while a neighboring hole remains an editable
analytic cylinder.

## Topology-first construction

1. Repair and orient the input mesh; reject solid guarantees for open or
   non-manifold inputs.
2. Segment mesh faces while preserving a one-owner-per-triangle invariant.
3. Fit and confidence-gate analytic/freeform hypotheses from unique nodes.
4. Infer global relations and jointly refit them. Do not round coefficients.
5. Freeze the region adjacency graph and its ordered boundary chains.
6. Create one canonical vertex and one canonical 3D edge per graph entity.
7. Build analytic faces and their p-curves on those shared edges.
8. Build residual boundary-constrained faces. If that fails, construct a
   faceted residual cell whose interface is the same canonical boundary—not an
   independent copy of the source polyline.
9. Verify edge incidence: every solid edge must have exactly two oppositely
   oriented incident faces.
10. Validate the in-memory solid and a STEP export/import before acceptance.

## Faceted interface requirement

A source triangle adjoining a cylinder uses a straight chord, while the
cylinder boundary is curved. They cannot simply share that chord as a valid
B-rep edge because the chord does not lie on the cylinder. The transition must
therefore use one of these constructions:

- project the interface to the analytic intersection and refit the residual
  boundary strip to that curve;
- use a narrow ruled/B-spline transition face between the source chord chain
  and canonical analytic curve; or
- keep the complete connected component faceted if the transition cannot be
  created inside tolerance.

This is why “make every unrecognized triangle a STEP face” alone does not
guarantee a watertight mixed solid.

## Implemented conforming-facet repair

The fallback now follows the local-repair pattern used by STL repair tools:

1. Retain the source residual region's triangle connectivity.
2. Split the analytic trim at every source boundary node, matching MeshLab's
   rule of splitting the opposite edge before welding a mismatched vertex.
3. Project those nodes into the analytic surface's parameter space and create
   exact on-surface B-rep edges. A cylinder boundary segment is therefore a
   curve on the cylinder, not its straight chord.
4. Move the residual boundary vertices to those canonical edge endpoints.
5. Keep interior triangles planar, but rebuild each interface triangle as a C0
   B-spline filling constrained by its curved analytic edge and its two mesh
   edges.
6. Accept the region only when every source triangle produced a valid face,
   the reconstructed area remains within tolerance, sewing has no free edges,
   and STEP export/import remains valid.

This is intentionally atomic. Partially repaired regions would introduce the
same T-junctions and unmatched boundaries that the fallback is meant to remove.

## STEP output

OpenCascade can translate manifold B-reps, faceted B-reps, shell-based surface
models, and AP242 tessellated entities. The preferred editable output remains a
manifold B-rep. Tessellated entities may be included as an auxiliary visual
carrier, but they do not make an invalid mixed B-rep watertight.

## Evidence used

- GlobFit fits local primitives and then enforces only globally consistent
  relations through constrained optimization:
  <https://geometry.stanford.edu/lgl_2024/papers/lwcscm-gfcfpdgr-11/lwcscm-gfcfpdgr-11.pdf>
- Point2CAD segments points, fits analytic or freeform supports, intersects
  adjacent surfaces to obtain topology, and then clips the supports:
  <https://openaccess.thecvf.com/content/CVPR2024/papers/Liu_Point2CAD_Reverse_Engineering_CAD_Models_from_3D_Point_Clouds_CVPR_2024_paper.pdf>
- CGAL documents least-squares region growing for planes, spheres, and
  cylinders and RANSAC support for plane, sphere, cylinder, cone, and torus:
  <https://doc.cgal.org/latest/Shape_detection/index.html>
- OpenCascade sewing assembles contiguous boundaries but does not replace
  correct shared topology construction:
  <https://dev.opencascade.org/doc/refman/html/class_b_rep_builder_a_p_i___sewing.html>
- OpenCascade GeomPlate supports point and G0/G1/G2 curve constraints for
  boundary-controlled residual surfaces:
  <https://dev.opencascade.org/doc/refman/html/class_geom_plate___build_plate_surface.html>
- OpenCascade STEP supports manifold, faceted, shell-based, and tessellated
  representations and documents STEP p-curve translation:
  <https://dev.opencascade.org/doc/overview/html/occt_user_guides__step.html>
- MeshLab's `Snap Mismatched Borders` projects each border vertex onto the
  nearest opposing boundary edge, splits the receiving face, and then welds
  the vertices:
  <https://pymeshlab.readthedocs.io/en/latest/filter_list.html#meshing-snap-mismatched-borders>
- CGAL's repair pipeline first refines triangles at intersections, optionally
  uses iterative snap rounding, and stitches geometrically identical border
  halfedges only after connectivity conforms:
  <https://doc.cgal.org/latest/PMP_Mesh_repair/index.html>
- Netfabb distinguishes tolerance-based border stitching from hole filling and
  warns that naive triangle filling does not preserve curved shape:
  <https://help.autodesk.com/cloudhelp/2025/ENU/NETF-LuaApiRef/files/lua-api-ref/objects/mesh/NETF-LUA-MESH-REPAIR.html>
- OpenCascade's N-side filling constrains a surface to pass through supplied
  3-D boundary edges at C0, G1, or G2 continuity:
  <https://dev.opencascade.org/doc/refman/html/class_b_rep_fill___filling.html>
