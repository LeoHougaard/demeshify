// The OCP wheel targets CPython 3.14 and the 2026_0 Emscripten ABI.
const indexURL = "https://cdn.jsdelivr.net/pyodide/v314.0.8/full/";
const wheels = [
  "7e/0c/b3cd8223432500f4562c4153d4d150ccd9f9693fd2e99cd5936b669e25de/cadquery_ocp_novtk_ocp_wasm-7.9.3.1.post202609191800-cp314-cp314-pyemscripten_2026_0_wasm32.whl",
  "42/3c/57a6c1683576c9a626207399972c2e83420dc6f87c239446190021d649db/cadquery-2.8.0-py3-none-any.whl",
  "a0/09/5ecb6f82d35a2f4a4334d0a608fcf764827fa69dccdf5987ce309c16b6c1/ezdxf-1.4.4-py3-none-any.whl",
  "af/98/cff14d53a2f2f67d7fe8a4e235a383ee71aba6a1da12aeea24b325d0c72a/multimethod-1.12-py3-none-any.whl",
  "80/db/879902580d720925092c86eaab2ceee7493e2fadbb5b718f54289b04d277/runtype-0.5.3-py3-none-any.whl",
  "05/98/716a473cfb24750858ddd5d14e6527539dd206583a46408d08eeb2844a75/trimesh-4.12.2-py3-none-any.whl",
].map(path => "https://files.pythonhosted.org/packages/" + path);

// This worker uses STEP and STL. Defer imports for VTK visualization and
// constraint solvers in the pinned CadQuery wheel's in-memory copy.
const deferOptionalImports = `
def _defer_optional_imports():
    import ast
    from importlib.metadata import distribution
    root = distribution("cadquery").locate_file("cadquery/occ_impl")
    exporters = root / "exporters/__init__.py"
    source = exporters.read_text().replace("from .vtk import exportVTP\\n", "")
    source = source.replace("        exportVTP(shape,", "        from .vtk import exportVTP\\n        exportVTP(shape,")
    exporters.write_text(source)
    for name in ("shapes.py", "assembly.py", "exporters/assembly.py", "solver.py", "sketch_solver.py"):
        path = root / name
        source = path.read_text()
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        imports = {}
        for node in tree.body:
            visualization = isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("vtkmodules.", "OCP.IVtk", "exporters.vtk", "vtk")
            )
            solver = isinstance(node, ast.Import) and node.names[0].name in ("casadi", "nlopt")
            if visualization or solver:
                statement = ast.unparse(node)
                for alias in node.names:
                    imports[alias.asname or alias.name] = statement
                for i in range(node.lineno - 1, node.end_lineno):
                    lines[i] = ""
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                names = {n.id for stmt in fn.body for n in ast.walk(stmt) if isinstance(n, ast.Name)}
                required = sorted({imports[n] for n in names & imports.keys()})
                if required:
                    first = fn.body[1 if ast.get_docstring(fn) else 0]
                    i = first.lineno - 1
                    lines[i] = "".join(" " * first.col_offset + s + "\\n" for s in required) + lines[i]
        annotations = "from __future__ import annotations\\n" if name == "solver.py" else ""
        path.write_text(annotations + "".join(lines))
_defer_optional_imports()
del _defer_optional_imports
import cadquery
`;

let runtimePromise;

async function start(onProgress) {
  onProgress("Loading Python");
  const { loadPyodide } = await import(/* @vite-ignore */ indexURL + "pyodide.mjs");
  const py = await loadPyodide({ indexURL });
  onProgress("Loading geometry libraries");
  await py.loadPackage([
    "numpy", "scipy", "shapely", "networkx", "pydantic", "pillow",
    "pyparsing", "typing-extensions", "fonttools", ...wheels,
  ]);
  onProgress("Initializing CadQuery");
  await py.runPythonAsync(deferOptionalImports);
  return py;
}

export function bootGeometryRuntime({ onProgress = () => {} } = {}) {
  return runtimePromise ??= start(onProgress).catch(error => {
    runtimePromise = undefined;
    throw error;
  });
}
