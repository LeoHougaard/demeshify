# Local open-profile interpolation

Later reporting correction: the 13/20 and ten-of-ten counts below are historical
reported-gate results. The coarse q01 output retained 132 planar source facets
without reporting them. With truthful counters, V15 passes 12/20 and nine of
that source's ten exports. See
[complete planar recovery](canonical-planar-recovery-2026-11-12.md) for the actual
repair, corrected matched comparison and remaining fidelity limitations.

## Failure and experiment

The frozen coarse development export `5b7f2dd6f8dee0946745-q01` was rejected
after a fitted extrusion overshot its source profile by 1.341 mm. Inspection of
the actual exported STEP identified the first open extrusion's basis curve,
rather than its three small residual hole patches. Its 23 stored profile nodes
contained an exact repeated consecutive node. The global degree-3-to-8
approximation reached Z=10.367 mm while the profile ended at Z=9.861 mm.

V14 removed only consecutive numerical duplicates, retaining all other nodes.
Six generic repeated-node invariance tests fail on V13 and pass on that change.
The whole conversion then produced valid fitted CAD, but still failed the
unchanged reference dimension gate: 0.20680 mm against 0.12 mm. Its native edge
tolerances reached 0.10313 mm. V14 was not promoted for strict recovery. Its
source, patch, results and actual STEP remain local. Switching to nominal mesh
bounds to bypass the tolerance-padded B-rep dimension check was rejected.

V15 uses local interpolation for explicitly open extrusion profiles after the
necessary numerical duplicate sanitization. It keeps the same source-node
budget and chord anchors. The already periodic closed-profile path retains the
same nodes, fit tolerance and periodic interpolation. Ambiguous near-closed
profiles retain their previous approximation path. No reference data enters
fitting, and no acceptance threshold or deadline changes.

Candidate surface source SHA-256:
`cbb3e356777fca7577fef335ffc5aac2cce098a956a8b72e67cf00ab1eb0cd8d`.
The preceding verified converter is `6ead82d`; evaluator/controller `09489ca`
has the same grading-function syntax tree as that revision. Per-case contracts
retain actual application, evaluator, runtime and input hashes.

## Matched result and limitations

The fast target/coarse/fine cohort improves from two to three strict passes.
The expanded frozen development set improves **11/20 to 13/20 strict**, with
20/20 geometry passes and no lost prior pass. These are 20 tessellation cases
of five source models. All ten variants of the affected source now pass the
existing strict gates, compared with eight previously. Only this source has all
ten variants in the inner set; this is not a full-corpus source-model rate.
The complete cohort wall time changes from 545.40 to 548.04 seconds, so no
overall speed gain is claimed.

The coarse recovered output has reference maximum/local deviations
0.045913/0.053239 mm, volume error 0.206384%, and tolerance-padded dimension
error 0.073259 mm. Its independently reopened STEP is valid, with one solid,
144 faces and zero reported fallback facets. Its 138 planar faces include
fragmented caps, and the small coarse hole remains visibly polygonal. Passing
these existing gates does **not** establish complete recovery of the original
smooth features or feature history. The remaining topology/surface-quality
limitations are explicit work for subsequent iterations.

The second recovered export, q06, previously failed STEP validity after a C0
residual fill and returned a 1,656-facet carrier. It now produces a valid
22-face solid without that fill, using the existing finer-trimming retry.
Reference maximum/local deviations are 0.007751/0.007837 mm, volume error
0.007007%, and dimension error 0.009176 mm. Independent reopening retains the
reference's single-solid tunnel signature `[[0]]`. Full-profile, upper-curve
and through-hole sections were inspected for both recovered outputs. The finer
hole follows the reference; the coarse one retains the limitation noted above.

Retained STEP SHA-256 values:

- q01: `af11807c52a246f7abae92ee5ca47241c6889981cd4616c9eacaf41a9421fe63`
- q06: `12a7c827cfe866b247915945d2015e7533323ba17c2513fefbe27eb2163b349a`

All nine separate frozen validation cases retain strict passes. Three private
controls retain three geometry passes and the same two smooth outputs; the
harder coarse anchor still uses 1,220 facets. Twenty-four targeted tests and
Ruff pass. The full suite passes **287 tests**, three warnings, in 295.59 seconds.
V15 is promoted locally for the two recovery gains with the limitations above.
Repository CI must still qualify the committed head and actual merge.

Local evidence is under `benchmarks/multires-1000/experiments/open-local-interpolation-v15*`,
`inspection-open-local-interpolation-v15/`, and
`inspection-open-local-interpolation-v15-q06/`. The raw inputs and failed V14
evidence remain intact. These results do not replace the unfinished frozen
1,000-source / 10,000-input baseline; the composed corpus currently contains
304 qualified sources / 3,040 inputs.

```powershell
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/open-profile-v14-three.json --output benchmarks/multires-1000/experiments/open-local-interpolation-v15-three.json --artifacts benchmarks/multires-1000/experiments/open-local-interpolation-v15-artifacts --fresh
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/v15-remaining17.json --output benchmarks/multires-1000/experiments/open-local-interpolation-v15-remaining17.json --artifacts benchmarks/multires-1000/experiments/open-local-interpolation-v15-artifacts --fresh
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/validation-nine-frozen.json --output benchmarks/multires-1000/experiments/open-local-interpolation-v15-validation9.json --fresh
```
