# STL to STEP Converter

[![CI](https://github.com/LeoHougaard/demeshify/actions/workflows/ci.yml/badge.svg)](https://github.com/LeoHougaard/demeshify/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/LeoHougaard/demeshify?quickstart=1)

STL to STEP Converter turns mechanical STL meshes into STEP CAD models. It runs
a local FastAPI geometry service and a browser-based Three.js viewer. The
recommended engine recognizes and joins analytic or fitted surfaces. An
experimental engine reconstructs an editable CadQuery feature history.

This is public beta software. Inspect every best-effort result before machining,
printing, or using it as a manufacturing reference.

## Use it in a browser

The quickest path is the **Open in GitHub Codespaces** button above. GitHub builds
the environment, starts the converter, and opens port 8421 in a private browser
tab. The first setup can take several minutes and uses roughly 1.1 GiB for the
Python environment. Codespaces usage may count against your GitHub allowance.

To run it on your own computer, install:

- Git
- [Node.js 22 LTS](https://nodejs.org/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/), or Python if the
  launch script needs to install uv for your user account

On Windows, open PowerShell:

```powershell
git clone https://github.com/LeoHougaard/demeshify.git
cd demeshify
.\run.ps1
```

On macOS or Linux:

```bash
git clone https://github.com/LeoHougaard/demeshify.git
cd demeshify
bash run.sh
```

Open <http://127.0.0.1:8421>. Both launch scripts install Python 3.12 into the
project environment, sync the locked dependencies, rebuild the browser app, and
start one local server process. Press Ctrl+C in the terminal to stop it.

The app includes a small sample plate, so you can test the full conversion and
download flow without finding an STL first.

Choose the STL export units before converting and check the displayed output
size. STL files do not store units. For example, a 0.18 × 0.25 model exported in
metres should become 180 × 250 mm. The viewer loads the file in your browser;
conversion and downloads require the local server to remain running.

## Reconstruction modes

| Mode | Best for | Result |
| --- | --- | --- |
| Recognized surfaces, recommended | Final-shape recovery, analytic faces, freeform residuals, multiple bodies | STEP B-rep, preview STL, surface graph, verification report |
| Parametric feature history, experimental | Single-body mechanical parts that need editable dimensions and operations | STEP, CadQuery source, feature plan, preview STL, verification report |

The surface engine fits planes, cylinders, cones, spheres, tori, extrusions,
revolutions, and B-spline residual faces. It joins their boundaries when the
topology closes. If native fitting crashes or exceeds its time limit, the
converter attempts a clearly labelled faceted recovery within the total job
deadline.

The API supervises the complete conversion or feature edit in a separate
process with a 240-second total deadline, including recovery, export, and
verification. Closed bodies with disjoint bounding boxes use their own fitting scale. Input
cleanup removes duplicate and zero-area triangles without moving vertices;
the report exposes remaining open and non-manifold edges. Non-finite input
coordinates are rejected before cleanup. Fully enclosed closed shells form
cavities according to containment depth; islands inside those cavities remain
separate solids. Containment is checked against the surfaces, not bounds alone.

The feature-history engine detects extrusions, steps, pockets, bosses, holes,
countersinks, oriented cylinders, revolutions, lofts, fillets, chamfers, arcs,
and splines. Its browser editor can change dimensions, suppress or reorder
operations, undo changes, rebuild the solid, and rerun geometric
verification.

## Reading the result

`complete` means the delivered STEP was re-imported and passed solid validity,
sampled surface deviation, local deviation, shell-count, solid-count, and volume checks.
Verification tessellates that STEP with absolute millimetre deflection; preview
STL files cannot establish a successful conversion. `best_effort` means CAD
surface recovery is incomplete or a geometry check failed. The report explains
why. A "Verified faceted fallback" has passed the geometry checks but still uses
triangle faces. Dense fallbacks may simplify the triangles; verification always
compares the exported STEP with the full-resolution input.

Artifacts are stored under `runs/<run-id>/` by default. Set
`STL_TO_STEP_RUNS_DIR` to keep them elsewhere. Feature-history edits retain older
revisions under the run's `history/` directory. The converter does not delete runs
automatically. Stop the server and remove the run directory when you no longer
need its input STL or generated CAD.

The documented launchers bind to `127.0.0.1`. Do not expose this beta directly
to the internet. It has no user accounts or per-user file permissions and is
designed for one person on one server process. Codespaces keeps the forwarded
port private unless you change its visibility.

## Current beta limits

The September 17 release test evaluated 500 distinct mechanical-part STLs.
497 passed the delivered-STEP geometry checks (99.4%); 382 also passed strict
CAD-surface recovery checks without triangle fallbacks (76.4%). Another 17
produced verified non-faceted models below the CAD-quality threshold, and 98
used verified faceted fallbacks. Three open or non-manifold inputs remained
unverified. No conversion exceeded the 240-second limit in the final batch.

CAD recovery is still well below 100%, particularly for fillets and complex
printed parts. See the [release report](docs/release-report.md) for the corpus
scope, unchanged acceptance gates, known failures, and all 500 per-file results.
These development-corpus results support a local public beta, not a guarantee
for arbitrary STL files.

A [September 18 evaluation](docs/design-evaluation-2026-09-18.md) retains all
previous passes on those 500 files and improves strict CAD recovery to 383/500
(76.6%). It also tests 56 additional files: 54 pass geometry and 19 have verified
output without facets, up from 13. Only 7 of the 36 reference-backed additional
cases pass strict CAD recovery, up from 5. These harder designs show a substantial
remaining gap in surface recovery even when the source meshes are watertight.

Expect weaker results for damaged scans, very noisy meshes, decorative freeform
shapes, assemblies, and designs whose original history is ambiguous. The surface
engine can preserve multiple closed bodies. The feature-history engine currently
targets one connected mechanical body. Large files can take minutes. The browser
skips the input preview above 50 MB to avoid duplicating a large mesh in memory,
but the server accepts STL files up to 250 MB.

## Development

Install and verify the locked project:

```powershell
uv sync --locked
uv run ruff check .
uv run pytest
npm --prefix web ci
npm --prefix web audit --audit-level=high
npm --prefix web run build
```

The current suite reconstructs generated STEP-derived meshes and checks feature
structure, surface topology, validity, metrics, progress, API downloads, dataset
helpers, and revision editing. Dataset instructions live in
[datasets/README.md](datasets/README.md). CI runs the same checks on Windows.

This repository is a source application, not an installable Python wheel. The
browser build must sit beside the backend, which is why the launch scripts and
Codespace build both parts together.

## Troubleshooting

- If port 8421 is busy, run `.\run.ps1 -Port 8422` on Windows or set
  `STL_TO_STEP_PORT=8422` before `bash run.sh`.
- If startup reports an old Node version, install Node.js 22 and open a new
  terminal.
- If the browser says the converter is offline, check the terminal for the first
  error and confirm <http://127.0.0.1:8421/api/health> opens.
- If a saved run stops loading after you delete its directory, remove the
  `?run=...` query from the browser address.

Please use the issue templates for reproducible bugs and focused feature
requests. Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing code and
[SECURITY.md](SECURITY.md) before reporting a vulnerability. Release changes are
recorded in [CHANGELOG.md](CHANGELOG.md).

STL to STEP Converter is available under the [MIT License](LICENSE).
Dependencies keep their own licenses.
