# Qualify reference meshes independently of solve outcomes

`tools.reference_mesh.validated_reference_mesh(shape, deflection_mm, ...)`
returns a `ReferenceMesh` containing the fine mesh and readiness diagnostics, or
raises `ReferenceFixtureError` with a distinct reason and partial diagnostics.
Call it on the original STEP in the grader. The converter must receive only STL.

The helper requires valid, finite, closed native solids with no unowned geometry.
Optional expected solid/shell counts check fixture metadata. It calls the existing
`app.scoring._step_mesh` with exactly the supplied deflection; the existing
0.04-radian angular setting is unchanged. No looser tessellation retry is used.

Every native face must have triangles, with one exception: the existing exact
triangular planar fast path emits a triangle directly and needs no attached OCCT
triangulation. Its native face count must still match the emitted triangle count.
All other meshes require positive per-face native triangulation counts and equal
native/emitted triangle totals. The aggregate mesh must be finite, nonempty,
watertight, consistently wound and have the expected number of face-connected
surface components. Closed native shells supply that count; shared topological
edges between shells are refused as unsupported ambiguity.

Diagnostics report native and mesh measurements. They contain no invented
deviation, dimension or volume acceptance threshold and no reconstruction pass or
fallback count. A ready reference is not a successful Demeshify reconstruction.
Missing solver/fallback measurements must remain missing in downstream reports.

These checks do not certify every trimmed region. A face can have some native
triangles while omitting another region; counts, closedness and component checks
cannot prove geometric coverage or reproduce feature history. The existing
independent geometry/quality gates remain necessary. No topological equivalence
or full geometric certification is claimed by this helper.

A reference readiness failure belongs to fixture acquisition/qualification or
the grader. Retain its evidence, repair/exclude it and acquire a replacement as
needed to reach 1,000 usable sources. Do not put it into a Demeshify solve-failure
denominator, count it as an accepted conversion, or reuse an incomplete reference
mesh as ground truth. Controller/grader integration is separate from this helper.
