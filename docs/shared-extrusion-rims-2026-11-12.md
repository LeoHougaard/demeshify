# Shared complete rims on closed extrusion bands

## Hypothesis and change

V11 made the fine development target `5b7f2dd6f8dee0946745-q09` valid by
interpolating its explicitly closed profile periodically. Its delivered STEP
still contained 1,011 edges, including 523 B-splines: the planar caps retained
hundreds of separate trim segments around the small closed hole.

V13 extends the existing annular shared-boundary mechanism to recognized closed
linear extrusions. It constructs the bounded periodic band, then supplies its
two complete rim edges to both adjacent planar faces. Both rims must be accepted
together. Each extrusion support node must lie within half the existing fit
tolerance of an interpolated profile node translated inside the finite extrusion
interval. The existing local curve/plane boundary guard remains, with an added
projection check of every boundary node rather than only the 32 sampled nodes.
Unmatched or unsupported bands retain the previous construction path.

This changes joining, not segmentation or the grading thresholds. No reference
STEP is supplied to reconstruction. The new joining tests use real generated STL
support nodes and isolate joining by supplying a recognized surface graph. They
verify valid STEP round trips, one solid, three faces, two complete shared rims,
and one periodic seam at two scales. A third test perturbs a boundary node missed
by the previous sampling guard. All three fail on the preceding source and pass
on V13; the prior eight tests in those files remain passing.

## Progressive matched evaluation

The frozen first cohort is `periodic-extrusion-three.json`: the fine target,
recovered middle control and original coarse success. All three retain strict
passes. On the fine target, canonical edges fall from 1,010 to 496 and delivered
STEP edges from 1,011 to 497 (20 circles, 468 lines, nine B-splines). Conversion
time in this single matched run falls from 40.107 to 29.396 seconds; whole-case
time from 71.954 to 53.018 seconds; sampled process-tree RSS from 2.810 to
2.168 GB. These are individual local observations, not confidence intervals.

Fine-target reference maximum and local deviations remain 0.00378070 and
0.00553871 mm, with dimension error 0.01258219 mm. Reference volume error changes
from 0.04859535% to 0.05399685%; this is a small deterioration, not improved
geometric accuracy. Both remain inside the unchanged strict gates. Nineteen
faces, one valid solid and zero fallback facets are retained. The middle control
retains identical geometric metrics and topology.

All 20 matched development cases preserve their outcomes: 11 strict passes,
20 geometry passes, no lost prior pass. The affected source remains 8/10,
failing all-ten source acceptance. The other changed export, q08, drops from
744 delivered edges to 456. Its local reference deviation changes from
0.00700068 to 0.00754397 mm, volume error from 0.00617048% to 0.00700637%,
both within the original gates. The other 18 cases retain identical recorded
geometric and topology metrics. The complete cohort's measured wall time is
550.45 versus 545.40 seconds; the targeted speed observation should not be
generalized to the whole corpus.

Nine separate frozen validation cases retain nine strict passes. Three private
controls retain three geometry passes and the same two non-faceted outputs;
the harder coarse anchor still uses 1,220 facets and is not a strict success.
The fine target was independently exported again and its approximately
0.057 mm through-hole visually inspected at five depths. The hole remains open
with contours following the STL and reference; the reference is opened only
after conversion. The retained STEP SHA-256 is
`d227ad10dab21e70762affeb133e25c024546a8cfd7c3240057a4fd1e648ec35`.

The full suite passes: **276 tests**, three warnings, 292.78 seconds. Ruff passes.
Independent STEP reopening confirms 497 actual edges, 19 faces, one valid solid
and shell Euler signature `[[0]]`, matching the reference's tunnel topology.
V13 is promoted locally for trim complexity and targeted resource gains, with
strict yield unchanged. Repository CI must qualify the committed head and the
actual merge. Source snapshot, patch, baseline test
failures and complete per-case evidence are retained locally under
`benchmarks/multires-1000/experiments/shared-extrusion-rims-v13*` and
`v13-baseline-test-*`. Candidate surface source SHA-256:
`be41b1556ee4d3f1fa10d03b58feae561e6189ac237c57c8f3299b03e94cf64e`.

```powershell
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/periodic-extrusion-three.json --output benchmarks/multires-1000/experiments/shared-extrusion-rims-v13-three.json --artifacts benchmarks/multires-1000/experiments/shared-extrusion-rims-v13-artifacts
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/v11-remaining17.json --output benchmarks/multires-1000/experiments/shared-extrusion-rims-v13-remaining17.json --artifacts benchmarks/multires-1000/experiments/shared-extrusion-rims-v13-artifacts
```
