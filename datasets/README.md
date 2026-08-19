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
