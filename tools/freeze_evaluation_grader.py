"""Freeze a committed host-side grader, separately from converter bytecode.

The retained-output entrypoint replaces conversion with a sealed artifact copy.
It never calls a benchmark CLI, creates profiles/grants, or changes global state.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import freeze_evaluation_converter as converter  # noqa: E402
from tools.freeze_evaluation_converter import (  # noqa: E402
    compile_module,
    digest,
    interpreter_magic,
    inventory,
)

OUTPUT_FILES = (
    'reconstruction.step', 'reconstruction.stl', 'report.json', 'surface_graph.json',
    'analytic_diagnostic.step', 'analytic_diagnostic.stl',
    'analytic_diagnostic_joined_surfaces.step', 'analytic_diagnostic_surface_graph.json',
    'analytic_diagnostic_report.json', 'closed_shell_roundtrip_diagnostic.step',
    'closed_shell_roundtrip_score.json', 'surface-worker-progress.log',
    'surface-worker.stderr.log', 'surface-worker.stdout.log',
)
ENTRY = r'''import atexit
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
readiness_only = len(sys.argv) == 4 and sys.argv[1] == '--reference-readiness'
if not readiness_only and len(sys.argv) not in (4, 5):
    raise ValueError('Expected case JSON, retained outputs, new result JSON, optional artifacts')
snapshot = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(snapshot / 'application'))
from tools.freeze_evaluation_converter import digest, inventory, verify
for value in sys.argv[1:]:
    path = Path(value)
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('Grading input and output roots cannot be links')
if readiness_only:
    case_path, destination = [Path(value).resolve() for value in sys.argv[2:4]]
    retained, artifacts = None, None
else:
    case_path, retained, destination = [Path(value).resolve() for value in sys.argv[1:4]]
    artifacts = Path(sys.argv[4]).resolve() if len(sys.argv) == 5 else None
if destination.exists() or (artifacts is not None and artifacts.exists()):
    raise ValueError('Result and optional artifact outputs must be new')
case_identity = digest(case_path)
configuration = json.loads(case_path.read_text(encoding='utf-8'))
identity = configuration['grader_manifest_sha256']
metadata = verify(snapshot, identity)
if metadata.get('artifact_kind') != 'grader':
    raise ValueError('Expected a frozen grader artifact')
if inventory(snapshot / 'dependencies') != metadata['dependency_files']:
    raise ValueError('Frozen dependency declarations changed')
runtime = Path(metadata['python_runtime'])
for target in (destination, artifacts):
    if target is not None and any(target.is_relative_to(root)
                                  for root in (snapshot, runtime, retained) if root is not None):
        raise ValueError('Grading outputs must be outside sealed artifacts and retained outputs')
case = configuration['case']
parent = Path(configuration['manifest_parent'])
parent = (parent if parent.is_absolute() else case_path.parent / parent).resolve()
expected = configuration.get('retained_sha256', {}) if readiness_only else (
    configuration['retained_sha256'])
allowed = __OUTPUT_FILES__
if not readiness_only and (not isinstance(expected, dict)
        or not {'reconstruction.step', 'report.json'} <= expected.keys()
        or not expected.keys() <= set(allowed)):
    raise ValueError('Expected only pinned converter output names, including STEP and report')
def sealed_outputs():
    if readiness_only:
        return {}
    found = {}
    for name in allowed:
        path = retained / name
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Retained output links are not permitted')
        if path.is_file():
            found[name] = digest(path)
    if found != expected:
        raise ValueError('Retained converter outputs differ from pinned hashes')
    return found
def fixture_hashes():
    result = {}
    fields = (('source_step', 'source_sha256'),) if readiness_only else (
        ('stl', 'input_sha256'), ('source_step', 'source_sha256'))
    for field, hash_field in fields:
        path = Path(case[field])
        path = path if path.is_absolute() else parent / path
        actual = digest(path)
        if actual != case[hash_field]:
            raise ValueError('Frozen source fixture differs from pinned identity')
        result[field] = actual
    return result
actual_outputs = sealed_outputs()
inputs = fixture_hashes()
destination.parent.mkdir(parents=True, exist_ok=True)
# The result hierarchy can be much deeper than Windows' native CAD file APIs
# permit. Keep scratch independently selectable without changing global paths.
scratch = Path(configuration.get('scratch_root', str(destination.parent)))
if not scratch.is_absolute() or not scratch.is_dir():
    raise ValueError('Grader scratch root must be an existing absolute directory')
for ancestor in (scratch, *scratch.parents):
    if ancestor.is_symlink() or (hasattr(ancestor, 'is_junction') and ancestor.is_junction()):
        raise ValueError('Grader scratch root cannot traverse links or junctions')
scratch = scratch.resolve(strict=True)
if any(scratch.is_relative_to(root) for root in (snapshot, runtime, retained)
       if root is not None):
    raise ValueError('Grader scratch must be outside sealed artifacts and retained outputs')
working = tempfile.TemporaryDirectory(dir=scratch, prefix='grader-work-')
atexit.register(working.cleanup)
work = Path(working.name)
for name, directory in (('STL_TO_STEP_RUNS_DIR', 'runs'), ('MPLCONFIGDIR', 'mpl'),
                        ('TEMP', 'tmp'), ('TMP', 'tmp'), ('TMPDIR', 'tmp')):
    path = work / directory
    path.mkdir(exist_ok=True)
    os.environ[name] = str(path)
tempfile.tempdir = str(work / 'tmp')
from app.schemas import ReconstructionReport
from app.step import load_step_shape
from tools import benchmark_multires_corpus as grader
from tools.reference_mesh import ReferenceFixtureError, validated_reference_mesh
calls = 0
def supplied(_run, stl, _name, _units, *, engine):
    global calls
    calls += 1
    if calls != 1 or engine != 'surface_brep':
        raise ValueError('Retained artifacts must supply exactly one surface conversion result')
    for name in expected:
        shutil.copyfile(retained / name, stl.parent / name)
    return ReconstructionReport.model_validate_json((retained / 'report.json').read_text())
original = grader.reconstruct_in_worker
# Keep fixture meshing separate from the shape subsequently used by the
# original scorer: meshing this object must not change its scoring bounds.
source = Path(case['source_step'])
source = source if source.is_absolute() else parent / source
reference_shape = None
try:
    reference_shape = load_step_shape(source)
    bounds = reference_shape.BoundingBox()
    diagonal = (bounds.xlen**2 + bounds.ylen**2 + bounds.zlen**2)**0.5
    readiness = validated_reference_mesh(
        reference_shape, max(min(diagonal * 0.00001, 0.002), 1e-7),
        expected_solid_count=case.get('ground_truth', {}).get('solid_count'),
    ).diagnostics
except ReferenceFixtureError as exc:
    readiness = exc.diagnostics
except Exception as exc:
    readiness = {'fixture_ready': False, 'reason': 'reference_preparation_error',
                 'preparation_error': f'{type(exc).__name__}: {exc}'}
del reference_shape
grader.reconstruct_in_worker = supplied
try:
    if readiness_only:
        result = {'accepted': False, 'status': 'reference_ready' if readiness.get(
            'fixture_ready') is True else 'fixture_error', 'conversion_wall_seconds': 0.0}
        if readiness.get('fixture_ready') is not True:
            result['evaluation_failure_kind'] = 'fixture'
    elif readiness.get('fixture_ready') is True:
        result = grader.evaluate(case, parent / 'frozen-grading-case.json', artifacts)
    else:
        result = {'accepted': False, 'evaluation_failure_kind': 'fixture',
                  'status': 'fixture_error', 'conversion_wall_seconds': 0.0}
finally:
    grader.reconstruct_in_worker = original
if calls != (1 if not readiness_only and readiness.get('fixture_ready') is True else 0):
    raise ValueError('Grader did not consume the retained conversion result exactly once')
result['artifact_copy_wall_seconds'] = result.pop('conversion_wall_seconds')
result.update(**{name: case[name] for name in ('id', 'source_model_id', 'split', 'preset')
                 if name in case}, fixture_readiness=readiness, second_conversion_not_run=True,
              reference_readiness_only=readiness_only,
              grader_source_revision=metadata['source_revision'],
              grader_manifest_sha256=identity, grader_case_sha256=case_identity,
              grader_runtime_files_sha256=hashlib.sha256(json.dumps(
                  metadata['runtime_files'], sort_keys=True,
                  separators=(',', ':')).encode()).hexdigest(),
              actual_output_sha256=actual_outputs, fixture_sha256=inputs,
              grading_workflow_qualified=False)
sealed_outputs()
fixture_hashes()
if digest(case_path) != case_identity:
    raise ValueError('Frozen grading case changed during evaluation')
verify(snapshot, identity)
if inventory(snapshot / 'dependencies') != metadata['dependency_files']:
    raise ValueError('Frozen dependency declarations changed during evaluation')
destination.parent.mkdir(parents=True, exist_ok=True)
with destination.open('x', encoding='utf-8') as stream:
    json.dump(result, stream, indent=2)
    stream.write('\n')
print(json.dumps({'result': str(destination), 'second_conversion_not_run': True,
                  'grader_source_revision': metadata['source_revision']}))
'''.replace('__OUTPUT_FILES__', repr(OUTPUT_FILES))


def _not_link(path: Path) -> None:
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('Frozen grader roots and metadata cannot be links or junctions')


def git_sources(project: Path, revision: str) -> tuple[str, dict[str, bytes], dict[str, bytes]]:
    """Read committed app sources and the static tools import closure, not working files."""
    commit, sources = converter.git_sources(project, revision)

    def committed(name: str) -> bytes:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Unexpected committed grader path')
        return subprocess.check_output(['git', 'show', commit + ':' + name], cwd=project)

    pending = {'tools/benchmark_multires_corpus.py', 'tools/freeze_evaluation_converter.py',
               'tools/reference_mesh.py'}
    while pending:
        name = min(pending)
        pending.remove(name)
        if name in sources:
            continue
        source = sources[name] = committed(name)
        for node in ast.walk(ast.parse(source, filename=name)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                if node.module == 'tools':
                    modules = ['tools.' + alias.name for alias in node.names]
                elif node.module:
                    modules = [node.module]
            for module in modules:
                if module.startswith('tools.'):
                    dependency = module.replace('.', '/') + '.py'
                    if dependency not in sources:
                        pending.add(dependency)
    declarations = {name: committed(name) for name in ('pyproject.toml', 'uv.lock')}
    return commit, sources, declarations


def freeze(project: Path, python_runtime: Path, revision: str, output: Path) -> dict:
    _not_link(python_runtime)
    _not_link(output)
    python_runtime, output = python_runtime.resolve(strict=True), output.resolve()
    if output.exists() or output.is_relative_to(python_runtime):
        raise ValueError('Use a new grader directory outside the base runtime')
    builder_hash = digest(Path(__file__))
    helper_hash = digest(Path(converter.__file__))
    base_files = inventory(python_runtime)
    executable = 'python.exe' if sys.platform == 'win32' else 'bin/python'
    if executable not in base_files:
        raise ValueError('Expected the declared runtime interpreter')
    if interpreter_magic(python_runtime / executable) != importlib.util.MAGIC_NUMBER.hex():
        raise ValueError('Base interpreter cannot load this build interpreter bytecode')
    commit, sources, declarations = git_sources(project, revision)
    committed_helper = sources['tools/freeze_evaluation_converter.py'].replace(b'\r\n', b'\n')
    working_helper = Path(converter.__file__).read_bytes().replace(b'\r\n', b'\n')
    if committed_helper != working_helper:
        raise ValueError('Committed artifact helper differs from the builder dependency')
    output.mkdir(parents=True, exist_ok=False)
    application, dependencies = output / 'application', output / 'dependencies'
    dependencies.mkdir()
    hashes = {}
    for name, data in sources.items():
        hashes[name] = hashlib.sha256(data).hexdigest()
        if name.endswith('.py'):
            compile_module(data, name, application / Path(name).with_suffix('.pyc'))
        elif name == 'LICENSE':
            (output / name).write_bytes(data)
        else:
            raise ValueError('Unexpected non-Python grader source; partial artifact preserved')
    for name, data in declarations.items():
        (dependencies / name).write_bytes(data)
    compile_module(ENTRY.encode(), 'grader.py', application / 'grader.pyc')
    manifest = {'schema': 1, 'artifact_kind': 'grader', 'source_revision': commit,
                'source_sha256': hashes, 'builder_sha256': builder_hash,
                'builder_dependencies_sha256': {
                    'tools/freeze_evaluation_converter.py': helper_hash},
                'entry_source_sha256': hashlib.sha256(ENTRY.encode()).hexdigest(),
                'entrypoint': 'application/grader.pyc', 'python_version': sys.version,
                'python_magic': importlib.util.MAGIC_NUMBER.hex(),
                'python_runtime': str(python_runtime), 'runtime_files': base_files,
                'application_files': inventory(application),
                'dependency_files': inventory(dependencies),
                'license_sha256': digest(output / 'LICENSE'),
                'workflow_qualified': False, 'isolation_qualified': False,
                'contains_reference_geometry': False}
    if (inventory(python_runtime) != base_files or digest(Path(__file__)) != builder_hash
            or digest(Path(converter.__file__)) != helper_hash):
        raise ValueError('Runtime or builder changed; partial grader artifact preserved')
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def verify(output: Path, manifest_sha256: str) -> dict:
    for path in (output, output / 'application', output / 'dependencies',
                 output / 'manifest.json', output / 'LICENSE'):
        _not_link(path)
    manifest = converter.verify(output, manifest_sha256)
    if manifest.get('artifact_kind') != 'grader' or not manifest.get('dependency_files'):
        raise ValueError('Expected a frozen grader artifact with dependency declarations')
    _not_link(Path(manifest['python_runtime']))
    if inventory(output / 'dependencies') != manifest['dependency_files']:
        raise ValueError('Frozen dependency declarations changed')
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--python-runtime', type=Path)
    parser.add_argument('--revision')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    if args.verify:
        if not args.manifest_sha256:
            parser.error('--manifest-sha256 is required to verify a frozen grader')
        result = verify(args.output, args.manifest_sha256)
    else:
        if not args.python_runtime or not args.revision:
            parser.error('--python-runtime and --revision are required to freeze')
        result = freeze(args.project, args.python_runtime, args.revision, args.output)
    print(json.dumps({'source_revision': result['source_revision'],
                      'manifest_sha256': digest(args.output / 'manifest.json'),
                      'entrypoint': result['entrypoint'],
                      'workflow_qualified': result['workflow_qualified']}, indent=2))


if __name__ == '__main__':
    main()
