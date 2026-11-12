# Benchmark data

Large third-party CAD data is intentionally not committed to this repository.
Download a dataset under its own terms, then generate STL inputs with:

```powershell
python -m uv run python tools/build_step_corpus.py "D:\CAD Data\steps" `
  --output datasets/corpus `
  --source-license "dataset-name-and-version"

python -m uv run python tools/benchmark_corpus.py datasets/corpus/manifest.json
```

The manifest records the source path, SHA-256, license label, tessellation
settings, dimensions, triangle count, and volume. This makes failures
reproducible and allows the same STEP model to be tessellated at several
qualities.

Recommended sources:

- [ABC Dataset](https://archive.nyu.edu/handle/2451/43778): one million CAD
  models with STEP and tessellated representations. Individual chunks are
  large; review the dataset and underlying Onshape model terms.
- [Fusion 360 Gallery reconstruction
  dataset](https://github.com/AutodeskAILab/Fusion360GalleryDataset): 8,625
  human construction sequences. Its dataset license is non-commercial
  research only and restricts redistribution.
- [DeepCAD](https://github.com/rundiwu/DeepCAD): 178,238 parsed sketch/extrude
  sequences derived from public Onshape documents. Code is MIT; review the
  data provenance and source-model terms separately.

Do not scrape arbitrary model sites. A downloadable file is not automatically
licensed for training, redistribution, or commercial use.

## Ten-resolution hill-climb corpus

Use the resumable serial materializer with an already acquired, licensed STEP
directory. For Fusion Gallery, a design family is the filename prefix before
the last underscore. All variants and known design families stay in one split.
Pass manifests used in earlier development so those families cannot enter the
new holdout. Do not treat this filename convention as universal for other data.

```powershell
python tools/build_multires_corpus.py path/to/breps/step `
  --output benchmarks/multires-1000 --models 1000 --batch-models 25 `
  --source-url https://github.com/AutodeskAILab/Fusion360GalleryDataset `
  --source-license "Fusion 360 Gallery Dataset License (non-commercial research)" `
  --license-file path/to/LICENSE.md --known-manifest path/to/previous-manifest.json

python tools/benchmark_multires_corpus.py benchmarks/multires-1000/manifest.json `
  --output benchmarks/multires-1000/baseline.json
```

Ten distinct triangle geometries per usable reference are required, rather than
ten different filenames. Invalid references, unusable exports, duplicate bytes,
conservative geometric signature collisions and repeated preset meshes remain
in an exclusion ledger and are replaced from later sources. Partial acquisition
is explicitly marked incomplete. Excluding unchanged planar tessellations biases
this corpus toward curved shapes; retain planar regression anchors separately.

`--batch-models` limits **newly accepted** sources in one invocation, excluding
cached sources and fixture failures. It exits successfully at the batch boundary
while retaining the full 1,000-source target and an explicit incomplete manifest.
Omit it to continue until the target or a real resource limit. Cached replay
preserves the previous larger manifest until all its sources have been verified.
The existing frozen exporter can adopt this controller-only change with
`--resume-controller-from 85beea1`: it verifies the prior Git file against the
frozen hash, unchanged non-controller/worker syntax and unchanged settings and
provenance, then retains the original contract and migration proof. Changed
geometry/export code requires a separate corpus; the option cannot approve it.

The evaluator launches conversion with a neutral STL only and reads reference
geometry afterward. It adds reference deviation, dimensions, exact CAD volume
and solid/shell comparisons to the existing strict no-facet gate. This separates
data flow but does not establish an OS filesystem sandbox. Source model rates
require all ten variants to pass; case rates are not independent-design rates.
Sampled geometric deviations do not prove exact surface equivalence or original
feature-history recovery. See [hill-climb state](../docs/hillclimb-state.md).

Start improvements before the full corpus is materialized or evaluated. Freeze
a small development cohort from the available complete source bundles:

```powershell
python tools/select_multires_cohort.py benchmarks/multires-1000/manifest.json `
  --output benchmarks/inner-six.json --models 6
python tools/benchmark_multires_corpus.py benchmarks/inner-six.json `
  --output benchmarks/inner-baseline.json
```

This balances surface/solid strata and favors lower triangle cost within each
stratum. It defaults to coarse, middle and fine exports, without inspecting
conversion outcomes. It is a selected development cohort, not a corpus rate.
Use it for quick matched candidates alongside difficult personal anchors.
Expand to all ten variants of affected sources, then broader validation and
justified full-corpus evaluations as candidates earn those checks. Alternate
resumable acquisition batches with experiments; do not let acquisition occupy
the only local evaluation slot indefinitely. The full 1,000-source / 10,000-case
goal and complete frozen baseline remain required, without blocking early gains.
Reserve validation for promotion and holdout for justified final checks.
Never tune against holdout failures. The
runner saves one case record at a time, a live `.summary.json`, and full result
snapshots every 100 cases and at completion. It rejects changed run contracts;
use a new output name for a different candidate. Native work is serial, bounded
and below normal Windows priority, with a 15 GiB free-disk floor. Memory reports
are one-second sampled sums of process-tree working sets, not physical RAM peaks.
Generated geometry, records, reference paths and private anchors stay out of Git.

## Pinned open-source assemblies

`materialize_assembly_corpus.py` builds a strict corpus when a project publishes
both printable STL parts and an assembly STEP. It compares rotation-invariant
absolute volume and surface-area signatures, requires a unique one-to-one
match, rejects open meshes and ambiguous matches, and exports the matched STEP
solid as ground truth. The resulting manifest records the upstream URL, exact
commit, license, file hashes, signatures, and STEP face/edge types.

The checked benchmark artifacts use the GPL-3.0 Voron sources at these pinned
commits:

- Voron Tap: `29e900094a0f094aad88493c76ec5a6d39f94812`
- Voron Stealthburner: `8bcb9c246fac19d8ac03931ef97fa07c5e5f0f2b`

For example:

```powershell
.\.venv\Scripts\python.exe tools\materialize_assembly_corpus.py `
  path\to\Tap_R8.step path\to\Voron-Tap\STLs `
  --output datasets\voron_tap_r8 `
  --source-name "Voron Tap R8" `
  --source-url "https://github.com/VoronDesign/Voron-Tap" `
  --source-commit 29e900094a0f094aad88493c76ec5a6d39f94812 `
  --source-license GPL-3.0
```

Third-party model files and generated benchmark outputs remain ignored. Re-run
the materializer from the pinned source checkout to reproduce them; do not
commit or redistribute upstream geometry from this repository.
