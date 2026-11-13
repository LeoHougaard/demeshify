# Diagnose delivered topology before grading tessellation

A saved V41 q02 output passed the existing strict reconstruction gates but its
additional topology diagnostic was indeterminate. Its shell remained closed,
manifold and coherently oriented with cellular Euler characteristic zero; native
validity alone had changed during evaluation.

Reopening the actual retained STEP established a valid solid, shell, every face
and every edge. Its supported Euler characteristic was zero, genus one, matching
the original. Applying the scorer's fine `_step_mesh` to that in-memory object
introduced `BRepCheck_InvalidPolygonOnTriangulation` on two edges and made three
faces invalid. Removing generated mesh metadata on a diagnostic copy restored
native validity. The STEP bytes never changed. This identifies scorer-generated
triangulation metadata, not a delivered native topology defect. The saved probe
and complete native statuses remain local under
`benchmarks/multires-1000/experiments/topology-fresh-probe-v41-q02/`.

`benchmark_case` now obtains topology diagnostics from independent fresh imports
of the actual reference and reconstructed STEP files before running grading
metrics. Those diagnostic objects are never meshed. It does not clean, repair or
mutate the scorer's shapes, change reconstruction geometry or substitute another
acceptance gate. Results record `measurement_stage=fresh_step_import_before_meshing`,
independent-import provenance and both STEP SHA-256 values. Historical frozen
grader results remain immutable; the new source must be frozen and qualified
separately before claiming updated workflow evidence.

Dictionary-only callers can still inspect supplied shapes, explicitly labelled
`supplied_metric_shapes`. Missing paths, failed imports and diagnostic errors stay
unavailable/indeterminate with `topology_match=null`; they never fall back to
possibly contaminated supplied shapes or become a match. Unsupported delivered
topology remains indeterminate, and a genuinely lost through-hole remains a
mismatch. A matching topology signature does not establish matching dimensions,
surface geometry or feature history.

Twenty-five focused topology/benchmark checks pass, including tiny actual STEP
files whose supplied metrics shapes contain unowned geometry. Fresh-file
diagnostics match while both files and those supplied shapes stay unchanged.
The production benchmark path, genuine lost-hole mismatch, invalid/missing STEP
and nonfatal comparator errors are covered. These primitives test the evaluator;
they are not reconstruction corpus models or yield evidence. Ruff passes.
Syntax-tree comparison against `635569f9aa19b9ab6ce3ec792bbd59869454fc4b`
confirms analytic-quality functions, acceptance assignments and conversion/
verification deadline checks are unchanged.

```powershell
python -m pytest tests/test_benchmark_topology_diagnostics.py tests/test_brep_topology.py -q
python -m ruff check tools/benchmark_surface_corpus.py tests/test_benchmark_topology_diagnostics.py
```
