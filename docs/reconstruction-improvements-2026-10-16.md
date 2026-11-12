# Reconstruction improvements, October 16, 2026

Two geometry changes at `8edb8c4` replace verified triangular fallbacks with
recovered CAD on two previously failing inputs. Compared with `6039744`, the
same selected ten models retain 8 geometry passes, improve strict CAD passes
from 5 to 6, and improve verified non-faceted results from 5 to 7. No previous
geometry or strict CAD pass is lost. These are selected-case results, not a new
500-case corpus rate.

## Preserve shared topology on planar models

Thin frame `146853_40cadbdd_27` previously produced invalid fitted boundaries
and fell back to 124 triangular faces. The recovery path now constructs shared
source edges and vertices, then merges coplanar faces into connected plane faces.
It runs only after ordinary fitting fails, for closed, consistently oriented,
single-body planar inputs with at most 2,000 cleaned faces.

The delivered STEP has 48 plane faces, one valid solid and no triangular faces.
Independent checks found the same surface area, volume and topology, with all
361 occupancy probes matching the source. A separate plate with two holes retains both holes. Curved, open,
non-manifold, disconnected and cavity-shell controls stay outside this recovery
path. A candidate must still pass the existing delivered-STEP geometry checks
before replacing the previous output.

## Constrain sparse extrusion profiles

Fillet model `30675_8c9704a7_0` contains an extrusion profile with clustered nodes
beside a long sparsely sampled span. The previous B-spline fit matched the nodes
but bowed between them. Its analytic candidate failed local deviation and volume
checks, so the converter delivered 13,642 verified triangular faces.

The fit now adds chord anchors to unusually long profile spans within the
existing point budget. It retains the original profile nodes and fitting
tolerance. Balanced curved profiles keep their existing fit. A synthetic
regression reproduces the bowing without copying dataset geometry into tests.

The new delivered STEP has 315 non-faceted faces, including cylinders, cones,
extrusions and a B-spline residual. It passes the geometry gate. Independent
measurement confirms the delivered solid and the remaining strict CAD failure.
For this profile, an exact full-domain check also bounds the production curve
and delivered STEP curve within 0.006118 mm of the source polyline in both
directions.

| Measurement | Previous analytic candidate | New delivered CAD |
| --- | ---: | ---: |
| Local deviation | 0.149432 mm | 0.011951 mm |
| Volume error | 2.26466% | 0.410618% |
| Maximum deviation | 0.130144 mm | 0.010313 mm |

The previous final faceted fallback was already geometrically verified. This
change improves its CAD representation within the existing error limits; it does
not claim lower surface error than the original exact facets.

Cylinder-area precision is still 93.0347%, below the unchanged 95% requirement.
Strict CAD acceptance therefore remains false for this model.

## Matched results and validation

| Case | Before | After | Geometry | Strict CAD |
| --- | --- | --- | --- | --- |
| `146853_40cadbdd_27` | 124 triangular faces | 48 plane faces | Pass | Pass, newly recovered |
| `30675_8c9704a7_0` | 13,642 triangular faces | 315 non-faceted faces | Pass | Below cylinder-area precision gate |
| `54506_cb98479f_0` | Verified fallback | Verified 12,000-face fallback | Pass | No |
| `60977_4bb093ec_0` | Unverified | Unverified | Reject | No |
| `41117_1b3271f2_96` | Unverified | Unverified | Reject | No |
| `136486_48c9afc2_1` | Recovered CAD | Recovered CAD | Pass | Pass |
| `101744_092d5940_0` | Recovered CAD | Recovered CAD | Pass | Pass |
| `127372_1e3b5df8_2` | Recovered CAD | Recovered CAD | Pass | Pass |
| `51692_3f21a1b1_1` | Recovered CAD | Recovered CAD | Pass | Pass |
| `28888_da6e6e94_0` | Recovered CAD | Recovered CAD | Pass | Pass |

All 237 collected tests passed exactly once across six supervised groups, with
zero failures or skips. These include ten new regressions for planar topology,
hole preservation, rejection of a wrong-size candidate, exclusion of curved
models, whole-span spline fidelity and point retention. Python lint passed.

```text
python -m pytest -o addopts= -q -p no:cacheprovider tests
python -m ruff check app tests tools
```

The matched conversion used `tools/benchmark_surface_corpus.py`, the frozen local
release manifest and the ten `--id` selections above, with `--workers 1
--case-timeout 240 --no-resume`. Native numerical libraries used one thread.
Reference auditing retained its separate 120-second allowance. Each test group
had a 180-second process-tree deadline. Commands, commit identities, source/test
hashes, test-node coverage, logs and per-case results were saved locally.
Input/reference SHA-256 identities matched the earlier baseline and release CSV.
No dataset geometry or generated STEP artifacts were added to Git.

Validity, ownership, shell/solid counts, deviation, volume and strict CAD gates
are unchanged. The 60-second analytic worker budget and 240-second job deadline
are unchanged. The frontend code is unchanged from the earlier
[reliability validation](reliability-validation-2026-10-16.md).

## Remaining failures

The dense fillet model still reaches the analytic detection limit and uses a
verified faceted fallback. Its final geometry remains accepted. A bounded trace
localized repeated cone and torus fitting work, without establishing a safe
optimization to promote in this change.

Both rejected inputs retain four non-manifold edges each. Their rejection is
expected for the malformed source meshes. No opening was filled and no source
vertices were moved to force acceptance.

The complete 500-case corpus was not rerun. Historical corpus totals remain
unchanged. Validation used the existing installed runtime without dependency
installation, a new dependency audit or browser automation.
