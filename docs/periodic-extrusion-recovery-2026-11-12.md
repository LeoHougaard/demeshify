# Periodic closed extrusion profiles

Baseline V10: `a510c2c8659e785d11bace81289e3a30bb75ffcb`, merged in PR #3 as
`0bd400191685758870464c93a63ff04b5ae7724d`. Both the head and merge passed CI.
V11 source SHA256, `app/surface_brep.py`:
`7a36e9b904fca7bd75d05cf4c988fafbfe584feab297f874c42848aabf98dcc0`.

The fine export's first sewn shell had zero free edges and individually valid
faces, but 34 inconsistent edge orientations, all between one closed extrusion
and its two planar caps. Its 259-point profile was downsampled to 128 points and
approximated as an open B-spline with coincident endpoints. Repeated sewing at
higher tolerances did not repair it. The unmodified diagnostic builder spent
76.6 seconds reaching the same invalid closed shell.

V11 fits explicitly closed profiles of at most 500 stored points with periodic
interpolation, retaining their original profile points. Open profiles retain
their existing path. This fixes closed-curve derivatives and seam behavior;
recognition, delivered-shape gates, validity checks and timeouts are unchanged.
No reference CAD enters conversion. The periodic-seam distinction is also a
useful invariant in the pinned [SolidCore seam implementation](https://github.com/tangency-labs/solidcore/blob/bfc2b6c7660a8609b172c1a0ba459ab0b3de3a8b/crates/solidcore-step/src/topology/seams.rs);
this change uses existing OCCT APIs and copies no SolidCore implementation.

## Matched results

| Cohort | V10 | V11 |
| --- | ---: | ---: |
| Development strict cases, five sources | 10/20 | 11/20 |
| Development geometry cases | 20/20 | 20/20 |
| Affected source's strict exports | 7/10 | 8/10 |
| Affected source, all-ten success | 0/1 | 0/1 |
| Separate validation strict cases, three sources | 9/9 | 9/9 |
| Private development anchors, geometry | 3/3 | 3/3 |

No previous strict pass was lost. Both private smooth controls remain smooth;
the difficult coarse private anchor still falls back. Unreferenced personal
cases are not strict reference successes. Holdout is untouched. The original
development baseline was 8/20; these are selected, correlated tessellations,
not full-corpus or independent-design rates.

The fine export changes from a 2,956-face faceted carrier to a valid single
solid with 19 fitted faces: four planes, ten cylinders and five extrusions.
Conversion time changes 109.46 to 40.93 seconds, complete graded case time
118.19 to 72.33 seconds, sampled process-tree working set 2.01 to 2.81 GB.
This is one matched run, not a controlled performance distribution.

The fitted result's input P95 is 0.0002574 mm, maximum 0.0046421 mm, local
maximum 0.0055368 mm and volume error 0.054168%. The carrier previously matched
the input mesh essentially exactly. Reference maximum changes 0.0010501 to
0.0037807 mm, reference local maximum 0.0010502 to 0.0055387 mm, reference
volume error 0.005570% to 0.048595%, and dimension error 0.0000002 to
0.0125822 mm. All retain the original acceptance limits. Smoothing is not
reported as improved geometric deviation.

Independent reimport retained one valid solid, one shell, and the source's
Euler shell signature `[0]` (one tunnel). Five section plots through the small
hole show its shape remains open and follows the reference. Inspection STEP
SHA256: `753d3a40def184387e548c15bf17d77117367b3c22a43c47ddd3ce66a49c1a3a`.
The delivered STEP still has 1,011 edges, including fragmented trim chains;
reducing that fragmentation and export cost is continuing work.

The full local suite passes 263 tests, zero failures/errors/skips, in 280.6
seconds. Lint passes. The new regression checks periodicity, STEP reimport and
meshing for a small, densely sampled closed extrusion. The browser workflow
was verified for V9; this is not a new V11 browser-flow claim.

## Reproduction and continuation

Use Python 3.12 with locked dependencies and native numerical pools set to one:

```powershell
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/periodic-extrusion-three.json `
  --output benchmarks/multires-1000/experiments/periodic-extrusion-v11-three.json
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/v11-remaining17.json `
  --output benchmarks/multires-1000/experiments/periodic-extrusion-v11-remaining17.json
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/validation-nine-frozen.json `
  --output benchmarks/multires-1000/experiments/validation9-periodic-extrusion-v11.json
```

All 20 V11 development cases and nine validation cases are fresh. V10/original
comparisons reuse explicitly identified immutable records. Raw shell BREP,
edge ownership diagnostics, per-case contracts/results, private anchors and
inspection STEP/plots remain in the ignored benchmark tree.

Corpus acquisition has 99 usable STEP sources / 990 distinct exports. A pinned
20-model KiCad download is additional raw input, pending native fixture checks;
it is not counted as usable. The 1,000/10,000 target and full baseline remain
unfinished. Continue bounded acquisition and small matched experiments. The
next trim hypothesis is sharing each complete closed extrusion rim with its
caps, using the existing annular-boundary machinery. Native STL preview export
also needs explicit serial meshing; preserve current tessellation tolerances
when making that separate resource change.
