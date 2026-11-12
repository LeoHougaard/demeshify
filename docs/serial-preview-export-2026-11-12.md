# Serial native preview export

CadQuery 2.8's generic STL exporter calls `Shape.exportStl` with parallel
meshing enabled by default. Both product preview paths now call that same
writer explicitly with `parallel=False`. Existing relative deflection,
linear/angular tolerances, binary format, full Workplane contents and STEP
export are preserved. A failed STL write raises a clear error.

This is V12, a separate resource correction after the V11 periodic profile
improvement. Candidate source SHA256 values:

- `app/surface_brep.py`: `f895121b067f7ed6784b3f8d4963933296c9dbf8dcbac78447dca81761f964ea`
- `app/cad.py`: `35d44e5bf8a8d7e793dca6b0e89e93477523ffb5d37148234aac641e87325a44`

Matched coarse/middle/fine controls retain all three strict passes and exactly
the same geometry/error metrics as V11. Only the two export functions change;
the rest of both modules' syntax trees is identical. Twenty-eight existing
CAD/API/extrusion workflow tests pass in 27.2 seconds. The earlier full 263-test
run applies to V11; final-head repository CI must qualify this resource change.

Both high- and low-quality CAD export controls preserve two disconnected
solids through STEP reimport and two watertight STL components. Their canonical
triangle geometries match the original parallel exporter exactly.

The real recovered fine case independently preserves a valid 19-face single
solid. Its STEP `DATA` section is identical to V11, excluding time-dependent
header metadata, SHA256
`06447f942625e159066e29b0bd1759cc90d7e1fbd20dde081964eec52f76c964`.
Its 119,174-triangle preview also has identical canonical geometry, SHA256
`0051518f1319870c02afde7ffc84f3176c00517cce6e3964acab2f608a1d1501`.
Watertightness, winding and positive volume are preserved.

One matched fine-case conversion changes 40.93 to 40.11 seconds, whole-case
grading 72.33 to 71.95 seconds, sampled working set 2.805 to 2.810 GB. No speed
or memory improvement is claimed from these single runs. The correction makes
native meshing's serial execution explicit without changing output fidelity.

Reproduction uses `periodic-extrusion-three.json` with
`tools/benchmark_multires_corpus.py`, output `experiments/serial-preview-v12-three.json`.
Local checks and exact preview/STEP comparisons are retained under
`benchmarks/multires-1000/experiments/serial-preview-v12-*` and
`inspection-serial-preview-v12/`. No additional full-corpus evaluation is
claimed. Continue source acquisition and whole-rim sharing against this best.
