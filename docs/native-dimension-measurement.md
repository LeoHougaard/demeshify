# Diagnose dimensions without waiving the original gate

The grader retains its original conservative dimension measurement: source
`BoundingBox()` before meshing, output `BoundingBox()` after meshing, and their
maximum dimension difference in `reference_dimension_max_error_mm`. The source
diagonal, acceptance Boolean, `max(0.12, diagonal * 0.003)` limit and every other
geometry/quality gate remain unchanged from the existing qualified grader.

Cached triangulation deflection can enlarge these boxes. A proposed change to
replace that conservative measurement with native geometry bounds was rejected
for production acceptance. The observed fitted output also has real curve-end
to vertex gaps of approximately 0.228 mm and a line gap of approximately 0.203 mm.
Its existing dimension failure cannot be dismissed as harmless metadata padding.
It remains unqualified until those boundary errors are genuinely resolved under
the original strict gates.

Separate diagnostics now use
`BRepBndLib.AddOptimal_s(shape, box, False, False)` for both shapes. This measures
trimmed native geometry without mesh-deflection or shape-tolerance padding, with
OCCT's numerical precision still applying. It does not use a preview or vertex-only
bound. The results are reported as `reference_native_dimension_max_error_mm`,
`reference_source_native_dimensions_mm` and `reference_output_native_dimensions_mm`,
with their measurement method and availability. Conservative source/output
dimensions are also retained, so the two measurements can be compared directly.
Unavailable native diagnostics remain unavailable and cannot alter acceptance.

The helper never changes geometry or clears, rebuilds or reduces triangulation
or tolerance metadata. Maximum native edge and vertex tolerances are reported
separately. A small native dimension difference does not repair shared boundaries,
remove uncertainty or establish geometric equivalence. Native diagnostics are
never substituted into the original dimension gate.

Tiny actual CAD tests reproduce the inflated mesh-box difference while confirming
that the conservative gate still rejects it. Ordinary 0.119 mm physical-size
errors still pass the existing threshold; 0.121 mm and 0.3 mm errors fail, and
translated shapes still fail deviation checks despite equal dimensions. Spheres
and known transformed primitives exercise true geometry bounds beyond topological
vertex extrema. These are evaluator tests, not converter recovery evidence.

The rejected production-measurement diff and its source/test/document snapshots
are preserved locally under `experiments/agent-corpus-review/` with the rejection
reason and hashes. Existing scores and frozen evidence remain historical. The
new diagnostic module is included in source fingerprints; it does not authorize
relabeling old cache records or claiming a promoted reconstruction candidate.

Validation passes 32 selected checks and Ruff. Literal syntax-tree comparison
against `9c039a7` confirms the source/output box, diagonal, dimension error,
volume/solid/shell checks, deflection, limit, original dimension result field
and reference acceptance Boolean remain unchanged. Native values do not enter
that Boolean.
