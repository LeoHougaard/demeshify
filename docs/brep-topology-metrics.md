# Evaluation-only BRep topology

`tools/brep_topology.py` exposes `topology_metrics(shape)` and
`compare_topology(reference, output)`. It does not change any acceptance gate,
converter input, cache identity or benchmark controller. The caller must keep
reference geometry outside the converter's access boundary.

For supported finite solids, the signature groups shell Euler characteristics
by owning solid and by native outer/cavity shell role. The signature ignores
solid order and incidental face subdivision. Each shell reports its native
unique vertices, nondegenerate edges, face wire counts, closedness, edge
incidence and orientation coherence. Genus is `(2 - chi) / 2`, where
`chi = V - E + sum(2 - face wire count)` for a closed orientable cellular shell.
Seam edges have two occurrences but one identity. Degenerate pole edges do not
count as one-dimensional cells; native topology maps are essential because
CadQuery's `Edges()` omits those edges. Inner face wires reduce the face's cell
contribution. No triangulation or approximation tolerance is used.

The comparison has three outcomes: `match`, `mismatch`, and `indeterminate`.
Invalid/open shells, non-manifold edge incidence, incoherent orientations,
inconsistent Euler parity, faces without boundary cells, extra geometry outside
solids, and reversed/infinite material orientation prevent a supported result.
Doubly closed face domains require explicit seam cuts in two independent UV
directions. Otherwise their intrinsic handle topology is ambiguous for this
formula and the result is indeterminate. Some valid periodic trims can require
a richer domain algorithm. Unsupported does not mean the geometry is wrong.

These are limited topological invariants, not proof of geometric equivalence,
exact feature semantics or original feature history. A box and a cylinder can
have the same signature. A blind hole leaves genus unchanged, while a tiny
through-hole changes it. Dimensions, relative component placement, surface
quality, original topology requirements and geometric deviation checks remain
necessary. The diagnostic does not prove that a kernel-valid shell is embedded
without geometric self-intersection or that every possible periodic-domain
encoding is cellular; it reports only its explicitly supported construction.

Fourteen focused checks cover a box, cylindrical seam, full sphere and its
degenerate pole edges, torus seams, tiny/multiple through-holes, a blind hole,
cavity shells, multiple solids and ordering, unowned edges, open shells,
reversed material orientation, unsupported doubly closed domains, and actual
STEP export/reimport. Run with the existing project environment:

```powershell
python -m pytest tests/test_brep_topology.py -q
python -m ruff check tools/brep_topology.py tests/test_brep_topology.py
```

The synthetic checks establish the supported subset. No development,
validation or holdout corpus was evaluated while adding this diagnostic.
