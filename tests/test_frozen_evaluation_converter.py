import importlib.util
import json
import os
import subprocess
import sys

import pytest

from tools import freeze_evaluation_converter as frozen


def artifact(tmp_path, monkeypatch):
    runtime = tmp_path / 'python'
    runtime.mkdir()
    executable = 'python.exe' if sys.platform == 'win32' else 'bin/python'
    path = runtime / executable
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'protocol runtime, not executable or isolation proof')
    monkeypatch.setattr(frozen, 'interpreter_magic',
                        lambda _: importlib.util.MAGIC_NUMBER.hex())
    committed = {'app/__init__.py': b'', 'app/jobs.py': b'VALUE = 42\n',
                 'LICENSE': b'fixture license'}
    monkeypatch.setattr(frozen, 'git_sources', lambda *_: ('commit', committed))
    output = tmp_path / 'snapshot'
    frozen.freeze(tmp_path, runtime, 'commit', output)
    return runtime, output, frozen.digest(output / 'manifest.json')


def test_compiled_snapshot_imports_exact_source_without_copying_base(tmp_path, monkeypatch):
    runtime, output, identity = artifact(tmp_path, monkeypatch)
    manifest = frozen.verify(output, identity)
    assert not manifest['isolation_qualified'] and not manifest['contains_reference_geometry']
    assert manifest['python_runtime'] == str(runtime.resolve())
    assert (output / 'LICENSE').read_bytes() == b'fixture license'
    assert not list((output / 'application').rglob('*.py'))
    assert not [p for p in output.rglob('*') if p.name in ('python.exe', 'python')]
    # Load actual generated sourceless bytecode; no mocked import/compiler.
    result = subprocess.check_output([
        sys.executable, '-I', '-B', '-c',
        'import sys; sys.path.insert(0,sys.argv[1]); import app.jobs; print(app.jobs.VALUE)',
        str(output / 'application'),
    ], text=True)
    assert result.strip() == '42'
    with pytest.raises(ValueError, match='new artifact'):
        frozen.freeze(tmp_path, runtime, 'commit', output)


@pytest.mark.parametrize('mutation', ['application', 'runtime', 'extra_cad', 'manifest'])
def test_pinned_verification_rejects_changed_or_extra_artifacts(tmp_path, monkeypatch, mutation):
    runtime, output, identity = artifact(tmp_path, monkeypatch)
    if mutation == 'application':
        (output / 'application/app/jobs.pyc').write_bytes(b'changed')
    elif mutation == 'runtime':
        (runtime / 'extra.py').write_bytes(b'changed')
    elif mutation == 'extra_cad':
        (runtime / 'reference.step.gz').write_bytes(b'not permitted in runtime')
    else:
        path = output / 'manifest.json'
        data = json.loads(path.read_text())
        data['source_revision'] = 'renamed'
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        frozen.verify(output, identity)


def test_incompatible_base_interpreter_is_rejected_before_creation(tmp_path, monkeypatch):
    runtime, _, _ = artifact(tmp_path, monkeypatch)
    monkeypatch.setattr(frozen, 'interpreter_magic', lambda _: 'wrong')
    output = tmp_path / 'incompatible'
    with pytest.raises(ValueError, match='interpreter bytecode'):
        frozen.freeze(tmp_path, runtime, 'commit', output)
    assert not output.exists()


def test_inventory_preserves_nested_catalog_order_and_rehashes_changed_bytes(tmp_path):
    names = ['z.dll', 'Alpha.py', 'a/module.pyc', 'a/deep/module.pyc', 'a.py',
             'b.X/module.PYD', 'unicode/part-\u00e9.dat', '.hidden/config']
    paths = []
    for index, name in enumerate(names):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes([index, 0, 255]))
        paths.append(path)
    expected = {path.relative_to(tmp_path).as_posix(): frozen.digest(path)
                for path in sorted(paths)}
    actual = frozen.inventory(tmp_path)
    assert actual == expected and list(actual) == list(expected)
    changed = tmp_path / 'z.dll'
    original_stat = changed.stat()
    changed.write_bytes(b'xyz')  # Same size and restored timestamps cannot hide new bytes.
    os.utime(changed, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert frozen.inventory(tmp_path)['z.dll'] != actual['z.dll']


@pytest.mark.parametrize('name', ['model.STEP.gz', 'shape.brep.tar.gz', 'mesh.stl.zip',
                                 'part.stp.xz', 'assembly.3mf.bak'])
def test_inventory_refuses_compound_cad_extensions(tmp_path, name):
    (tmp_path / 'library.dll').write_bytes(b'ordinary library')
    (tmp_path / name).write_bytes(b'reference geometry cannot enter the runtime')
    with pytest.raises(ValueError, match='CAD asset'):
        frozen.inventory(tmp_path)


def test_inventory_refuses_empty_and_non_directory_roots(tmp_path):
    with pytest.raises(ValueError, match='Empty runtime artifact'):
        frozen.inventory(tmp_path)
    file = tmp_path / 'file'
    file.write_bytes(b'not a catalog directory')
    with pytest.raises(ValueError, match='Empty runtime artifact'):
        frozen.inventory(file)


def test_inventory_checks_resolved_containment_before_hashing(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'library.dll').write_bytes(b'library')
    outside = tmp_path / 'outside.dll'
    outside.write_bytes(b'outside data')
    realpath = os.path.realpath

    def escaped(path, **options):
        return str(outside) if str(path).endswith('library.dll') else realpath(path, **options)

    monkeypatch.setattr(os.path, 'realpath', escaped)
    with pytest.raises(ValueError, match='escaped path'):
        frozen.inventory(runtime)


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows junction fixture')
def test_inventory_refuses_native_junction_before_external_traversal(tmp_path):
    runtime, outside = tmp_path / 'runtime', tmp_path / 'outside'
    runtime.mkdir()
    outside.mkdir()
    source = outside / 'sentinel.dll'
    source.write_bytes(b'external data must remain untouched')
    junction = runtime / 'alias'
    created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                             capture_output=True, text=True, check=False)
    if created.returncode:
        pytest.skip('The filesystem does not permit a task-owned junction fixture')
    try:
        with pytest.raises(ValueError, match='links or junctions'):
            frozen.inventory(runtime)
        assert source.read_bytes() == b'external data must remain untouched'
    finally:
        junction.rmdir()  # Remove only this new junction, never its target.
