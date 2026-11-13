"""Freeze committed converter bytecode against an existing local Python runtime.

This builds an artifact, not a checkout. It creates no profiles, ACLs or global
configuration. OS isolation and native workflow qualification are separate.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import marshal
import struct
import subprocess
import sys
from pathlib import Path, PurePosixPath

FORBIDDEN = {'.step', '.stp', '.stl', '.brep', '.3mf'}
ENTRY = '''import json
from pathlib import Path
import sys
if len(sys.argv) != 3 or sys.argv[2] not in ('mm', 'cm', 'm', 'in'):
    raise ValueError('Expected a neutral STL job directory and supported units')
job = Path(sys.argv[1]).resolve()
sys.stdout = (job / 'bootstrap.stdout.log').open('w')
sys.stderr = (job / 'bootstrap.stderr.log').open('w')
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.jobs import reconstruct_in_worker
report = reconstruct_in_worker('case', job / 'input.stl', 'input.stl', sys.argv[2],
                               engine='surface_brep')
(job / 'bootstrap-result.json').write_text(json.dumps({'complete': True,
                                                     'status': report.status}) + '\\n')
'''


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root: Path) -> dict[str, str]:
    root = root.resolve(strict=True)
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Runtime artifacts cannot contain links or junctions')
        if not path.is_file():
            continue
        if (not path.resolve().is_relative_to(root)
                or any(suffix.lower() in FORBIDDEN for suffix in path.suffixes)):
            raise ValueError('Runtime artifact contains an escaped path or CAD asset')
        result[path.relative_to(root).as_posix()] = digest(path)
    if not result:
        raise ValueError('Empty runtime artifact')
    return result


def compile_module(source: bytes, name: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    code = compile(source, name, 'exec', dont_inherit=True)
    # Sourceless bytecode: fixed timestamp header, exact interpreter magic.
    destination.write_bytes(importlib.util.MAGIC_NUMBER +
                            struct.pack('<III', 0, 0, len(source)) + marshal.dumps(code))


def git_sources(project: Path, revision: str) -> tuple[str, dict[str, bytes]]:
    commit = subprocess.check_output([
        'git', 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}',
    ], cwd=project, text=True).strip()
    names = subprocess.check_output([
        'git', 'ls-tree', '-r', '--name-only', commit, 'app',
    ], cwd=project, text=True).splitlines()
    sources = {}
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or path.parts[0] != 'app':
            raise ValueError('Unexpected committed application path')
        if path.suffix == '.py':
            sources[name] = subprocess.check_output(['git', 'show', commit + ':' + name],
                                                     cwd=project)
    if 'app/__init__.py' not in sources or 'app/jobs.py' not in sources:
        raise ValueError('Expected the committed Demeshify converter package')
    sources['LICENSE'] = subprocess.check_output(['git', 'show', commit + ':LICENSE'], cwd=project)
    return commit, sources


def interpreter_magic(executable: Path) -> str:
    return subprocess.check_output([
        str(executable), '-I', '-B', '-S', '-c',
        'import importlib.util; print(importlib.util.MAGIC_NUMBER.hex())',
    ], text=True).strip()


def freeze(project: Path, python_runtime: Path, revision: str, output: Path) -> dict:
    builder_hash = digest(Path(__file__))
    python_runtime = python_runtime.resolve(strict=True)
    output = output.resolve()
    if output.exists() or output.is_relative_to(python_runtime):
        raise ValueError('Use a new artifact directory outside the base runtime')
    base_files = inventory(python_runtime)
    executable = 'python.exe' if sys.platform == 'win32' else 'bin/python'
    if executable not in base_files:
        raise ValueError('Expected the declared runtime interpreter')
    if interpreter_magic(python_runtime / executable) != importlib.util.MAGIC_NUMBER.hex():
        raise ValueError('Base interpreter cannot load this build interpreter bytecode')
    commit, sources = git_sources(project, revision)
    output.mkdir(parents=True, exist_ok=False)
    application = output / 'application'
    hashes = {}
    for name, data in sources.items():
        hashes[name] = hashlib.sha256(data).hexdigest()
        if name.endswith('.py'):
            compile_module(data, name, application / Path(name).with_suffix('.pyc'))
        else:
            (output / name).write_bytes(data)
    compile_module(ENTRY.encode(), 'converter.py', application / 'converter.pyc')
    manifest = {'schema': 1, 'source_revision': commit, 'source_sha256': hashes,
                'builder_sha256': builder_hash,
                'entry_source_sha256': hashlib.sha256(ENTRY.encode()).hexdigest(),
                'python_version': sys.version, 'python_magic': importlib.util.MAGIC_NUMBER.hex(),
                'python_runtime': str(python_runtime), 'runtime_files': base_files,
                'application_files': inventory(application),
                'license_sha256': digest(output / 'LICENSE'),
                'isolation_qualified': False, 'contains_reference_geometry': False}
    # Freeze both halves of the artifact; base libraries are reused, not copied.
    if inventory(python_runtime) != base_files or digest(Path(__file__)) != builder_hash:
        raise ValueError('Base runtime or builder changed; partial artifact preserved')
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def verify(output: Path, manifest_sha256: str) -> dict:
    path = output / 'manifest.json'
    if digest(path) != manifest_sha256:
        raise ValueError('Frozen manifest differs from the pinned identity')
    manifest = json.loads(path.read_text())
    if manifest.get('schema') != 1 or manifest['python_magic'] != importlib.util.MAGIC_NUMBER.hex():
        raise ValueError('Unsupported snapshot or interpreter bytecode version')
    if (inventory(Path(manifest['python_runtime'])) != manifest['runtime_files']
            or inventory(output / 'application') != manifest['application_files']
            or digest(output / 'LICENSE') != manifest['license_sha256']):
        raise ValueError('Frozen runtime, application or license changed')
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--python-runtime', type=Path)
    parser.add_argument('--revision')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--manifest-sha256', help='Required pinned identity when verifying')
    args = parser.parse_args()
    if args.verify:
        if not args.manifest_sha256:
            parser.error('--manifest-sha256 is required to verify a frozen artifact')
        result = verify(args.output, args.manifest_sha256)
    else:
        if not args.python_runtime or not args.revision:
            parser.error('--python-runtime and --revision are required to freeze')
        result = freeze(args.project, args.python_runtime, args.revision, args.output)
    print(json.dumps({'source_revision': result['source_revision'],
                      'manifest_sha256': digest(args.output / 'manifest.json'),
                      'runtime_files': len(result['runtime_files']),
                      'application_files': len(result['application_files']),
                      'isolation_qualified': result['isolation_qualified']}, indent=2))


if __name__ == '__main__':
    main()
