# Supported residual reconstruction, V41 candidate

V41 recovers a difficult middle-resolution relay export as one valid solid with
55 analytic faces and zero fallback facets. The matched original converter
returns a 2,072-facet carrier, which preserves the input geometry but fails the
strict smooth-CAD gate. On the three-design development inner cohort, strict
passes increase from two to three, with both passing controls retained.

**Promotion remains inconclusive.** The completed older development cohort has
13 strict passes out of 20, but its fresh matched original-converter run is
still pending. The historical best also scored 13/20; that historical result
does not establish matched regression equivalence. V21 remains the strongest
verified core until the required comparisons qualify V41.

The evaluated converter snapshot is
`032fae4fdff92fc82a4d88b13e1df90f2824e274`, frozen manifest SHA-256
`8e61535f08f9f86877f5f8a29aea514ac4870fa247f8ff934a45ab7135c4565f`.
Its Graph source, `app/surface_graph.py`, has SHA-256
`5abe5c93fcd6eb2ddf78d3980c787d5021a02d64bb38f5d0b0005a2286611d52`.
The original converter is `c84f7ac864a1a251bc944f1333c7f81105db96dc`,
manifest `abdbaa623da4241bbcb09bf09f5162df2ef8a630c10492771d0ace13bb0fbad9`.
Both use frozen grader `e9e2497f272e97bd8d1b413a42b87bd33bcba799`, manifest
`9c136ef66d53fea52f1c0c3198f89426050872b2e234d9b0dafc8dcb680d928f`.
These identities distinguish the actual evaluated artifacts from later working
experiments.

## Shared cause and bounded correction

Independently recognized regions of the same cylinder left artificial trim
borders. Tiny circular chord strips could remain classified as planes,
including strips touching perpendicular end fillets. Separately, an earlier
small-residual ownership rule could flatten a curved strip into a neighboring
plane while retaining stale fit diagnostics. These segmentation errors can
make individually plausible surface fits impossible to join reliably.

The candidate changes Graph recognition and ownership:

- Recover residual cylinder regions using an independently validated existing
  support, preserving the original owner's geometry and faces. A small region
  must touch its seed and pass the existing validator over their complete union.
- Recognize a residual plane only when its nodes and normals establish a plane,
  its valid trimmed polygon matches the source triangle area, and two adjacent
  independent cylinder axes provide consistent tangent supports. A tessellation
  cell alone is insufficient evidence.
- Merge connected cylinder owners only when their infinite supports coincide
  at floating-point precision, ownership is current and nonoverlapping, and the
  unchanged seeded-cylinder validator accepts every face in the union.
- Test each neighboring cylinder against the original chord size, node, normal
  and tangent checks before selecting support owners. Perpendicular end fillets
  that do not explain the chord keep their original ownership.
- When separate fits differ because of stored-coordinate rounding, fit one
  cylinder to all support and chord nodes. Individual and joint residuals must
  remain within measured float32 input precision, accounting for input-unit
  scaling; other coordinates use a double-roundoff floor. The complete union
  must still pass the original node, normal and triangle-chord validator before
  any owner changes. This does not widen the exact-support comparison.
- Apply the original plane-fit node and normal limits before absorbing a tiny
  residual into a plane, then refresh the resulting region's actual diagnostics.

Merges refresh bounds, area, trim loops and fit statistics. Removed owners are
resolved for later chord operations, and every source face retains one owner.
The Graph cache version changes so older segmentation results cannot be reused.
The optional coordinate-scale argument defaults to one for existing callers.

An independent source review against main `9f281bfa8fa0091e4e30ac75cc622ec0ac8b0690`
found no blocking defect in these invariants. Review is separate from measured
reconstruction qualification. V42–V44 BRep experiments, closed-shell normalization
experiments and preview tessellation work are excluded from this candidate.

## Matched evidence and limits

The three inner cases were fresh for both versions, using identical inputs,
settings, isolated conversion and the same frozen grader. Reference STEP remains
host-only; grading consumes the retained converter output without reconstructing
again. The original 120-second analytic, 240-second conversion and 360-second
whole-case limits remain in force, alongside the original validity, solid and
component, no-facet, analytic-quality, deviation, volume, dimension and STEP
round-trip gates. No reference model enters the reconstruction algorithm.

| Inner development case | Original strict | V41 strict | Original conversion | V41 conversion |
| --- | --- | --- | ---: | ---: |
| Difficult middle export | Fail, 2,072 facets | Pass, 55 analytic faces | 127.77 s | 17.25 s |
| Smooth-surface control | Pass | Pass | 11.51 s | 14.11 s |
| Existing fine-export control | Pass | Pass | 29.32 s | 34.46 s |

The recovered target has 38 planes, 15 cylinders and two tori. Its reference
maximum deviation is 0.069515 mm, local maximum 0.084552 mm, dimension error
0.097552 mm against the original 0.12 mm limit, and volume error 0.005589 percent.
Both controls retain their geometry measurements, but their observed conversion
times are slower. This small comparison establishes neither a broad speed gain
nor a population reconstruction rate. Equivalent recovered geometry also does
not imply recovery of the original feature history.

The older development cohort is complete at 13/20 strict across five source
designs, with 16 fresh cases and four verified reused cases. These are 20 case
results, not 20 independent designs or 20 fresh conversions. Seven strict
failures remain despite zero reported fallback facets in that cohort. Fresh
matched original results, separate validation cases and private regression
anchors still need to qualify promotion.

The affected source's ten-export expansion has evaluated seven variants: q00–q04
still fail strict reconstruction, while q05 and q06 pass. That partial run has
three fresh cases and four verified reused cases. q07–q09 remain pending; no
ten-of-ten source-model success is established.

The frozen acquisition corpus contains 1,000 distinct STEP models and 10,000 STL
exports, grouped by source/geometry family across development, validation and
holdout. Its full quantitative baseline remains unfinished. The inner and older
development results must not be presented as a 10,000-case result, and holdout
failures must not become iterative tuning cases.

## Checks and retained evidence

Thirty-five focused Graph checks pass across supported residual recognition,
tiny-plane ownership, coincident cylinder ownership, chord ownership and joint
cylinder support. They include physical radius/axis/offset negatives, a real
planar wall and end cap, rejected full-union validation, disconnected supports,
sequential chord ownership and millimetre/inch coordinate scaling.

```powershell
python -m pytest tests/test_supported_residual_primitives.py tests/test_tiny_residual_plane_ownership.py tests/test_coincident_cylinder_ownership.py tests/test_coincident_chord_ownership.py tests/test_joint_chord_cylinder_support.py -q
```

Local evidence under `benchmarks/multires-1000/experiments/controller-v2/`
includes `c84-inner.json`, `v41-inner.json`, `matched-inner-decision.json`,
`v41-legacy20.json`, `v41-target-ten.json`, frozen run contracts, per-case
attestations, retained STEP outputs and failure artifacts. Original failed
experiments and private anchors remain preserved locally. Generated geometry,
private manifests and experimental BRep changes are excluded from delivery.

Before promotion, finish the fresh matched regression comparisons and the
affected ten-export cohort, inspect representative outputs, and record any lost
passes or performance changes. Those targeted checks do not satisfy the full
corpus requirement: freeze the original converter's 10,000-case baseline and
complete the justified matched candidate evaluation on those same inputs before
claiming a corpus-qualified reconstruction improvement. Until then, retain V21
as the verified default and label V41 provisional. Reviewed intended source must
pass exact-head CI, merge to the intended target and pass CI at the actual merged
SHA; CI alone does not qualify reconstruction yield.
