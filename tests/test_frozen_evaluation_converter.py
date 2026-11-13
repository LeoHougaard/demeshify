import importlib.util
import json
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
