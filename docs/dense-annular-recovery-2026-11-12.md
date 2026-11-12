# Dense interior annular recovery

V10 extends the registered-boundary recovery from V9, committed at `7162562`
and merged with the batch controller in PR #2. V10 source SHA256:
`59c14cba4f403dc78a033c044e4910c67c30e8016ff09bdca04b06d5debef16a`
(`app/surface_brep.py`). The full local suite passes all 262 tests with zero
failures, errors or skips; Python lint also passes. This qualifies V10 for
promotion within the tested cohorts, not as completion of the full hill climb.

The next failed development export has only 24 points on each closed rim but
646 support nodes. The earlier 500-node bound excluded it solely because of
interior mesh density. V10 separates the existing boundary interpolation
budget (502 stored loop points including closing duplicates) from support
projection work. The latter uses the same 1,500-node bound as the existing
graph fitter. Every support node is still checked. No boundary points are
discarded, and fit tolerances, topology/geometry gates and timeouts are unchanged.

## Matched evidence

| Cohort | Original baseline | V9 | V10 |
| --- | ---: | ---: | ---: |
| Development, strict cases | 8/20 | 9/20 | 10/20 |
| Development, geometry cases | 20/20 | 20/20 | 20/20 |
| Affected source, strict exports | 5/10 | 6/10 | 7/10 |
| Affected source, all-ten success | 0/1 | 0/1 | 0/1 |
| Separate validation, strict cases | 9/9 | Unmeasured | 9/9 |
| Private anchors, geometry | 3/3 | 3/3 | 3/3 |

There are no lost prior strict passes. Development comprises five source
models; validation comprises three different source models with coarse,
middle and fine exports, selected by metadata and mesh cost before inspecting
solve outcomes. These correlated, selected cases are not a full-corpus rate.
Validation failures are not used for tuning. Holdout remains untouched.
Two unreferenced personal anchors retain smooth geometry successes; the
referenced difficult coarse anchor still has 1,220 fallback faces and fails
strict recovery. Missing reference CAD is not counted as a strict success.

The newly recovered export changes from 1,246 fallback faces to one smooth
residual, delivering a valid single solid with 17 faces. Reference local
deviation remains 0.0145252 mm; reference maximum changes from 0.0144907 to
0.0145059 mm and volume error improves from 0.112109% to 0.095099%. Reference
dimension error remains 0.0322841 mm. Input maximum deviation increases from
0.0124108 to 0.0135136 mm; input local maximum remains 0.0140604 mm. All pass
the same existing gates. Conversion time changes from 18.44 to 21.60 seconds,
sampled working set from 1.74 to 1.88 GB. This is a fidelity/representation
gain, not a demonstrated speedup. Across the 20 cases, single-run median
conversion wall time changes from 15.33 to 15.08 seconds without a controlled
performance claim.

Independent reimport and five section plots show the small through-hole
remains open and tracks the reference. Inspection STEP SHA256:
`d083d1e2a41288affc46b5ae419f3482765fbbd264b32c1ff817eb00ce9bdde4`.
Eight annular tests pass, including a dense interior and rejection of a single
outlying interior node. The prior complete browser export/reopen workflow is
documented in the V9 report; it was not repeated as a fresh V10 browser run.

## Reproduction

Use the locked Python 3.12 environment and one native numerical thread.
Frozen inputs, complete per-case results, source contracts and failed
experiments remain local under `benchmarks/multires-1000/`:

```powershell
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/dense-annular-three.json `
  --output benchmarks/multires-1000/experiments/dense-annular-v10-three.json
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/v10-remaining17.json `
  --output benchmarks/multires-1000/experiments/dense-annular-v10-remaining17.json
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/validation-nine-frozen.json `
  --output benchmarks/multires-1000/experiments/validation9-dense-annular-v10.json
```

The development union contains 20 fresh V10 cases. Its V9 and original
baselines reuse earlier immutable records with explicit provenance; no run is
relabelled a fresh 10,000-case evaluation. Validation baseline was freshly
evaluated with an explicitly logged `85beea1` source overlay, restored to the
candidate afterward. Private manifests and inspection plots stay out of Git.

Corpus acquisition is at 74 distinct sources / 740 exports after a clean
five-model batch. The 1,000/10,000 target and complete baseline remain required.
Next examine the remaining invalid trim shells, including actual per-face
and shared-edge diagnostics, while alternating bounded acquisition batches.
