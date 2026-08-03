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
