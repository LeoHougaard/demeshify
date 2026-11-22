# Browser conversion tests

Run the complex suite against a production browser build after installing the
test runner with `npm ci` in this directory and setting up the project's native
Python environment.

```text
node tools/browser-smoke/smoke.mjs https://leohougaard.github.io/demeshify/ --complex
```

The suite picks ten parts from the local Fusion and Voron corpora described in
[datasets/README.md](../../datasets/README.md). Set `SMOKE_CORPUS_ROOT` when the
corpora are elsewhere. The optional argument after `--complex` filters by dataset
prefix, such as `fusion` or `voron`. The fixtures and reference STEP files stay
local and follow their dataset terms.

The test selects each STL through the UI, downloads the delivered STEP, and checks
its geometry with native OpenCascade. It records source and delivered face counts,
solid and shell correspondence, curved surface area recall and precision, and
whether the result meets the strict CAD checks. Geometry verification and strict
CAD recovery are recorded separately in `geometry.json` and `reference-audit.json`.
Each run also keeps the app reports, timings, and recorded network requests.

Generate additional synthetic fixtures with
`python tools/browser-smoke/cad.py generate-extended <directory>`. The chamfered
slot has an exact reference STEP. To verify that a layered test uses the intended
browser path, pass its STL to the smoke runner with
`SMOKE_EXPECT_IMPLEMENTATION=layered`.
