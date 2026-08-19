# STL to STEP Converter

[![CI](https://github.com/LeoHougaard/stl-to-step-converter/actions/workflows/ci.yml/badge.svg)](https://github.com/LeoHougaard/stl-to-step-converter/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/LeoHougaard/stl-to-step-converter?quickstart=1)

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
git clone https://github.com/LeoHougaard/stl-to-step-converter.git
cd stl-to-step-converter
.\run.ps1
```

On macOS or Linux:

```bash
git clone https://github.com/LeoHougaard/stl-to-step-converter.git
cd stl-to-step-converter
bash run.sh
```

Open <http://127.0.0.1:8421>. Both launch scripts install Python 3.12 into the
project environment, sync the locked dependencies, rebuild the browser app, and
start one local server process. Press Ctrl+C in the terminal to stop it.

The app includes a small sample plate, so you can test the full conversion and
download flow without finding an STL first.

## Reconstruction modes

| Mode | Best for | Result |
| --- | --- | --- |
| Recognized surfaces, recommended | Final-shape recovery, analytic faces, freeform residuals, multiple bodies | STEP B-rep, preview STL, surface graph, verification report |
| Parametric feature history, experimental | Single-body mechanical parts that need editable dimensions and operations | STEP, CadQuery source, feature plan, preview STL, verification report |

The surface engine fits planes, cylinders, cones, spheres, tori, extrusions,
revolutions, and B-spline residual faces. It joins their boundaries when the
topology closes. If native fitting crashes or exceeds its time limit, an isolated
worker returns a clearly labelled faceted recovery instead of taking down the
server.

The feature-history engine detects extrusions, steps, pockets, bosses, holes,
countersinks, oriented cylinders, revolutions, lofts, fillets, chamfers, arcs,
and splines. Its browser editor can change dimensions, suppress or reorder
operations, undo changes, rebuild the solid, and rerun geometric
verification.

## Reading the result

`complete` means the automatic solid, STEP round trip, surface deviation, and
volume checks passed. `best_effort` means the app produced a usable artifact but
one or more confidence gates failed. The report explains why. A faceted fallback
preserves source topology, but it is not an analytic reconstruction.

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

The strict surface stress corpus currently completes 53 of 60 cases. Four cases
exceeded 240 seconds, and three open or internally inconsistent source meshes did
not produce an accepted result. See the dated
[surface failure ledger](docs/surface-failure-ledger.md) for exact cases and
[research notes](docs/RESEARCH_AND_ROADMAP.md) for the current engineering work.

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
