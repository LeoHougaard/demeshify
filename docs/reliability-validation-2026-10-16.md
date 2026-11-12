# Reliability validation, October 16, 2026

The reliability branch's implementation at
[`894179c`](https://github.com/LeoHougaard/demeshify/commit/894179cc6c0e67ef6b2eaa4d714dee08287f13c8)
passed the advanced tests and six-case comparison. A fresh full suite,
independent geometry audit and four additional release cases also completed.
No implementation changes were needed during this validation. The code and test
hashes stayed unchanged throughout; this report adds documentation only.

## Test and application checks

- **227 passed, zero failed or skipped.** All collected test nodes ran exactly once
  across six supervised groups. This includes all 50 advanced reconstruction tests.
- The 43 mesh/cavity/STEP integrity regressions cover floating cancellation,
  enclosed cavities, stray geometry and multiple STEP transfer roots.
- API tests cover conversion, background polling, saved results, STEP downloads,
  feature edits, failure reporting and job deadlines.
- Python lint passed. The existing frontend dependencies produced a Vite build
  in 2.03 seconds. Vite reported its existing large-chunk advisory.

The installed runtime was Python 3.12.13, CadQuery 2.8.0, OCP 7.9.3.1.1,
trimesh 4.12.2, numpy 2.4.6, pytest 8.4.2, Node.js 24.15.0 and Vite 7.3.6.
No dependencies were installed or updated. Equivalent complete-suite checks are:

```text
python -m pytest -o addopts= -q -p no:cacheprovider tests
python -m ruff check app tests tools
npm --prefix web run build
```

## Matched conversions and additional cases

The six frozen cases were rerun with one worker, one numerical thread, the same
240-second conversion budget and separate 120-second reference-audit allowance.
Against `210caea`, geometry acceptance stayed **4/6**, strict CAD acceptance stayed
**3/6**, and every recorded quality metric was identical. No accepted gate was lost.
Input, reference and saved baseline artifact hashes also matched.

Four previously untested release cases were selected before execution: two fillet
inputs with zero-area cleanup exposure and unrelated single-/multi-extrude
controls. The dense fillet input has 147,994 original triangles. These four are
screened against the historical release CSV, without claiming a fresh paired
baseline comparison. All four retained their historical geometry/CAD acceptance.

| Case | Loaded triangles | Geometry | Strict CAD | Delivered result |
| --- | ---: | --- | --- | --- |
| `146853_40cadbdd_27` | 124 | Pass | No | Faceted fallback |
| `60977_4bb093ec_0` | 9,288 | Reject | No | Unverified diagnostic |
| `136486_48c9afc2_1` | 46,200 | Pass | Pass | Recovered CAD |
| `41117_1b3271f2_96` | 1,066 | Reject | No | Unverified diagnostic |
| `101744_092d5940_0` | 1,890 | Pass | Pass | Recovered CAD |
| `127372_1e3b5df8_2` | 12 | Pass | Pass | Recovered CAD |
| `30675_8c9704a7_0` | 13,642 | Pass | No | Faceted fallback |
| `54506_cb98479f_0` | 147,990 | Pass | No | Faceted fallback |
| `51692_3f21a1b1_1` | 12 | Pass | Pass | Recovered CAD |
| `28888_da6e6e94_0` | 1,632 | Pass | Pass | Recovered CAD |

Across this selected ten-case set, 8/10 pass geometry and
5/10 pass strict CAD recovery. Both rejected inputs have non-manifold edges.
These counts describe this selected set, not a corpus-wide rate.

The dense fillet fallback has 12,000 faces, verified against all
147,990 cleaned source triangles. Its maximum deviation is 0.0120492 mm,
local deviation 0.0170747 mm and volume error 0.0892198 percent.

Use `tools/benchmark_surface_corpus.py` with an existing local dataset manifest,
the listed `--id` selections, `--workers 1 --case-timeout 240 --no-resume`, an
`--output` file and an `--artifacts-root` directory. The existing
[release CSV](release-results-2026-09-18.csv) records membership and input/reference
SHA-256 identities. Logs, commands, runtime metadata, per-case results and CAD
artifacts were retained locally; generated datasets and artifacts are excluded
from Git.

## Independent geometry checks

All seven original binary STL adversarial fixtures retained their recorded bytes.
Exact points subdivide correctly. The three floating-cancellation cases at
1e-16 mm, 1e-17 mm and the smallest positive float32 offset stay open and do not
acquire a closed volume.

For thin frame `146853_40cadbdd_27`, exact rational checks prove that all 96 original
nondegenerate triangles partition into 124 children. Vertices and original face
provenance are unchanged. Children remain inside the parent plane/triangle,
retain orientation and conserve area, with non-overlapping interiors. All 28
boundary subdivisions remain valid.

The saved 124-face STEP also passes current verification independently: one valid
solid, all import roots included and no geometry outside the solids. Maximum
deviation is 1.668e-12 mm, local deviation 1.573e-10 mm and volume error
4.748e-12 percent. The fresh conversion reproduced the saved quality metrics.

## Remaining limits

The frame is still a verified faceted `best_effort` result, and both additional
fillet cases also use verified facets. Strict CAD recovery has not become complete.
Inputs `60977_4bb093ec_0` and `41117_1b3271f2_96` retain four non-manifold edges each
and remain unverified. Geometry gates, fitting budgets and fallback labels were
not relaxed.

This validates ten current-revision release cases. The full 500-case current
revision evaluation was not run, so the historical release totals remain
unchanged. A clean installation, network dependency audit and automated browser
interaction were outside this installed-runtime validation. The frontend build
and API lifecycle checks passed.
