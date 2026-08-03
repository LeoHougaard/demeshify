# MeshMind CAD

MeshMind CAD is a local, autonomous mechanical STL-to-STEP reconstructor. It
examines the mesh, infers a compact feature plan, builds editable CadQuery
geometry, compares the reconstructed surfaces with the original, and exports
the best valid solid.

The current engine handles constant and multi-level mechanical extrusions,
arbitrary-plane sketch extrusions, stepped parts, blind pockets, bosses,
cardinal and oblique cylinders/tubes, aligned openings, countersinks,
revolutions, tapered lofts, fillets/chamfers, and mixed line/circular-arc
and changing-curvature spline profiles. It deliberately reports **best effort**
instead of pretending that a geometrically poor reconstruction is exact.

## Run it on Windows

From PowerShell:

```powershell
git clone https://github.com/LeoHougaard/meshmind-cad.git
cd meshmind-cad
.\run.ps1
```

The first run installs a project-local Python 3.12 environment and JavaScript
packages. Open <http://127.0.0.1:8421>.

## Output

Every successful reconstruction produces:

- `reconstruction.step` — editable boundary-representation solid
- `reconstruction.py` — readable CadQuery construction history
- `plan.json` — validated, machine-editable feature parameters
- `reconstruction.stl` — preview tessellation
- `report.json` — fit metrics, assumptions, and warnings

`plan.json` uses the ordered v2 feature-tree schema. Every feature has a stable
id, timeline position, dependency list, readable name, suppression state, and
measured-parameter provenance. The browser editor can change scalar dimensions,
vectors, and sketch geometry; reorder or suppress operations; lock parameters;
undo/reset local changes; then rebuild and re-verify the result before replacing
the current export. Previous saved revisions are retained under
`runs/<id>/history/`.

Mixed sketches preserve straight lines, constant-radius circular arcs, and
changing-curvature tangent spline segments as separately editable geometry.
Measured cardinal cylindrical cuts are emitted as Round hole features on X,
Y, or Z; arbitrary-axis cylinders remain oriented cylinder features.

To reopen a saved reconstruction directly in the editor, use
`http://127.0.0.1:8421/?run=<run-id>`.

Browser reconstructions run as local background jobs. The progress overlay
reports the current geometry stage, elapsed time, candidates tested, and the
number of features in the construction currently being evaluated. The bar is
driven by reconstruction events rather than a looping time estimate.
Each valid intermediate solid is also rendered as provisional CAD in the
browser. This uses the browser GPU only for interactive visualization; the
OpenCascade reconstruction and geometric verification remain CPU operations.
The camera is preserved as newer feature-tree revisions replace the preview.

Run artifacts stay under `runs/<id>/`. The input never needs to leave the
computer unless you explicitly configure an external language-model endpoint.

## How reconstruction works

1. Load and normalize the triangle mesh with Trimesh.
2. Detect coherent planar change levels, analytic surface patches, and both
   cardinal and measured 3D feature axes.
3. Fit analytic lines, circles, circular arcs, and tangent spline segments to
   each sketch profile.
4. Group equal adjacent sections into extrusion layers and separate oblique
   analytic cylinders from polygonal slice approximations.
5. Build competing single- and multi-feature parametric CadQuery solids,
   including conical, revolved, lofted, and edge-finished alternatives.
6. Tessellate each solid and calculate bidirectional surface distance, volume
   error, validity, and a small complexity penalty.
7. Export the best candidate and label it high-confidence only when it passes
   automatic geometric thresholds.

The language model is an optional planner, not the geometry kernel. Model
responses must validate against the typed feature schema, are rebuilt by
CadQuery, and must retain their geometric score. Model-generated code is never
executed.

## Optional AI guidance

MeshMind accepts any OpenAI-compatible chat-completions endpoint. Copy
`.env.example` values into your PowerShell environment before starting:

```powershell
$env:MESHMIND_LLM_URL = "http://127.0.0.1:8000/v1"
$env:MESHMIND_LLM_MODEL = "your-model"
$env:MESHMIND_LLM_KEY = ""
.\run.ps1
```

You can then enter instructions such as “prefer nominal dimensions” or “keep
the mounting holes exact.” Without these variables, reconstruction stays fully
deterministic and local.

## Test

```powershell
python -m uv sync
python -m uv run pytest
python -m uv run ruff check .
cd web
npm run build
```

The tests begin with editable STEP geometry, tessellate it to STL, discard the
history, reconstruct it, and verify both the feature structure and resulting
solid. Cases include stepped shafts, blind pockets, mixed line/arc profiles,
rounded plates, and a pocket combined with a perpendicular hole.

For large dataset evaluation, see [datasets/README.md](datasets/README.md) and
the corpus tools under `tools/`. Research findings and the staged architecture
are in [docs/RESEARCH_AND_ROADMAP.md](docs/RESEARCH_AND_ROADMAP.md).

## Current boundaries

The feature plan still represents some pockets, bosses, and unusually complex
edge-finish transitions as equivalent boolean extrusion layers rather than the
original named design operation. Free-form decorative surfaces, assemblies,
multi-body reconstruction, damaged scan data, and general non-mechanical
meshes remain outside the verified scope. A `best_effort` result should always
be reviewed before machining.

## Technical lineage

The implementation is original and uses permissive/open CAD infrastructure:
CadQuery/OCCT for B-rep construction and STEP export, Trimesh/SciPy/Shapely for
geometry analysis, and FastAPI/Three.js for the local application.

Related work that informed the architecture includes
[CADFit](https://github.com/AutodeskAILab/CADFit), which demonstrates
render-and-compare search over CadQuery programs, and
[CAD-Recode](https://github.com/filaPro/cad-recode), which studies direct
point-cloud-to-CadQuery generation. CADFit's repository is non-commercially
licensed and patent-noted, so its implementation was not copied into this
project.

## License

MIT. Dependencies retain their own licenses.
