import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import freeze_evaluation_grader as frozen

SCHEMAS = b'''import json
import os
from pathlib import Path
from types import SimpleNamespace
Path(os.environ['STL_TO_STEP_RUNS_DIR'], 'storage-import-probe').write_text('import side effect')
class ReconstructionReport:
    @classmethod
    def model_validate_json(cls, value):
        return SimpleNamespace(**json.loads(value))
'''
STEP = b'''from pathlib import Path
from types import SimpleNamespace
def load_step_shape(path):
    data = Path(path).read_bytes()
    return SimpleNamespace(data=data,
                           BoundingBox=lambda: SimpleNamespace(xlen=1., ylen=2., zlen=3.))
'''
REFERENCE = b'''from types import SimpleNamespace
class ReferenceFixtureError(ValueError):
    def __init__(self):
        self.diagnostics = {'fixture_ready': False, 'reason': 'unrepresented_native_faces'}
def validated_reference_mesh(shape, deflection, *, expected_solid_count=None):
    if b'unready reference' in shape.data: raise ReferenceFixtureError()
    return SimpleNamespace(diagnostics={'fixture_ready': True, 'deflection_mm': deflection})
'''
EVALUATOR = b'''import hashlib
import os
import tempfile
from pathlib import Path
def reconstruct_in_worker(*args, **kwargs):
    raise AssertionError('A second conversion was attempted')
def main():
    raise AssertionError('Benchmark CLI cannot grade a frozen artifact')
def evaluate(case, manifest, artifacts):
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        source = Path(case['stl'])
        if not source.is_absolute(): source = manifest.parent / source
        (work / 'input.stl').write_bytes(source.read_bytes())
        report = reconstruct_in_worker('case', work / 'input.stl', 'input.stl', 'mm',
                                       engine='surface_brep')
        assert sorted(p.name for p in work.iterdir()) == [
            'input.stl', 'reconstruction.step', 'report.json']
        if case.get('mutate'):
            Path(case['mutate']).write_bytes(b'changed during grading')
        return {'accepted': report.status == 'complete',
                'retained_report_value': report.value,
                'copied_step_sha256': hashlib.sha256(
                    (work / 'reconstruction.step').read_bytes()).hexdigest(),
                'input_sha256': hashlib.sha256((work / 'input.stl').read_bytes()).hexdigest(),
                'grader_environment': {k: os.environ[k] for k in (
                    'STL_TO_STEP_RUNS_DIR', 'MPLCONFIGDIR', 'TEMP', 'TMP', 'TMPDIR')},
                'conversion_wall_seconds': 0.25, 'verification_wall_seconds': 0.5}
'''


def artifact(tmp_path, monkeypatch, *, evaluator=EVALUATOR):
    runtime = tmp_path / 'runtime'
    executable = runtime / ('python.exe' if sys.platform == 'win32' else 'bin/python')
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'protocol-only runtime, not native isolation proof')
    monkeypatch.setattr(frozen, 'interpreter_magic',
                        lambda _: importlib.util.MAGIC_NUMBER.hex())
    sources = {'app/__init__.py': b'', 'app/schemas.py': SCHEMAS, 'app/step.py': STEP,
               'tools/reference_mesh.py': REFERENCE,
               'tools/benchmark_multires_corpus.py': evaluator,
               'tools/freeze_evaluation_converter.py': Path(frozen.converter.__file__).read_bytes(),
               'LICENSE': b'test repository license'}
    dependencies = {'pyproject.toml': b'[project]\nname="test-grader"\n',
                    'uv.lock': b'version = 1\n'}
    monkeypatch.setattr(frozen, 'git_sources',
                        lambda *_: ('exact-test-commit', sources, dependencies))
    snapshot = tmp_path / 'grader'
    frozen.freeze(tmp_path, runtime, 'requested-commit', snapshot)
    identity = frozen.digest(snapshot / 'manifest.json')
    return runtime, snapshot, identity


def grading_case(tmp_path, snapshot, identity):
    fixtures = tmp_path / 'fixtures'
    fixtures.mkdir()
    (fixtures / 'input.stl').write_bytes(b'original STL fixture')
    (fixtures / 'reference.step').write_bytes(b'original STEP fixture, host only')
    retained = tmp_path / 'retained'
    retained.mkdir()
    (retained / 'reconstruction.step').write_bytes(b'actual converter STEP')
    (retained / 'report.json').write_text(json.dumps({'status': 'complete', 'value': 73}))
    # Unexpected files are never copied or executed by the callback.
    (retained / 'evil.py').write_text('raise AssertionError("must not execute")')
    case = {'id': 'source-q05', 'stl': 'input.stl', 'source_step': 'reference.step',
            'input_sha256': frozen.digest(fixtures / 'input.stl'),
            'source_sha256': frozen.digest(fixtures / 'reference.step')}
    configuration = {'case': case, 'manifest_parent': str(fixtures),
                     'grader_manifest_sha256': identity,
                     'retained_sha256': {name: frozen.digest(retained / name)
                                         for name in ('reconstruction.step', 'report.json')}}
    path = tmp_path / 'case.json'
    path.write_text(json.dumps(configuration))
    result = tmp_path / 'grading' / 'result.json'
    command = [sys.executable, '-I', '-B', str(snapshot / 'application/grader.pyc'),
               str(path), str(retained), str(result)]
    return configuration, path, retained, result, command


def run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


def test_generated_entry_grades_actual_outputs_without_second_conversion(tmp_path, monkeypatch):
    runtime, snapshot, identity = artifact(tmp_path, monkeypatch)
    configuration, _, retained, destination, command = grading_case(tmp_path, snapshot, identity)
    before = {p.name: p.read_bytes() for p in retained.iterdir()}
    result = run(command)
    assert result.returncode == 0, result.stderr
    graded = json.loads(destination.read_text())
    assert graded['accepted'] and graded['retained_report_value'] == 73
    assert graded['fixture_readiness']['fixture_ready']
    assert graded['second_conversion_not_run']
    assert graded['artifact_copy_wall_seconds'] == .25
    assert 'conversion_wall_seconds' not in graded
    assert graded['verification_wall_seconds'] == .5
    assert graded['actual_output_sha256'] == configuration['retained_sha256']
    assert graded['copied_step_sha256'] == frozen.digest(retained / 'reconstruction.step')
    assert graded['input_sha256'] == configuration['case']['input_sha256']
    assert graded['grader_source_revision'] == 'exact-test-commit'
    assert graded['grader_manifest_sha256'] == identity
    assert not graded['grading_workflow_qualified']
    assert before == {p.name: p.read_bytes() for p in retained.iterdir()}
    manifest = frozen.verify(snapshot, identity)
    assert manifest['python_runtime'] == str(runtime.resolve())
    assert not list((snapshot / 'application').rglob('*.py'))
    assert not any(p.name in ('python', 'python.exe') for p in snapshot.rglob('*'))
    assert (snapshot / 'LICENSE').read_bytes() == b'test repository license'
    assert not manifest['workflow_qualified'] and not manifest['contains_reference_geometry']
    assert all(Path(value).is_relative_to(destination.parent)
               for value in graded['grader_environment'].values())
    assert not list((snapshot / 'application').rglob('runs'))
    # Real generated bytecode was imported in the child; this is not a mocked evaluator call.
    assert 'second_conversion_not_run' in result.stdout


@pytest.mark.parametrize('mutation', ['application', 'runtime', 'dependency', 'license',
                                    'manifest', 'extra_cad'])
def test_pinned_grader_refuses_tampered_artifact_halves(tmp_path, monkeypatch, mutation):
    runtime, snapshot, identity = artifact(tmp_path, monkeypatch)
    if mutation == 'application':
        (snapshot / 'application/app/schemas.pyc').write_bytes(b'changed')
    elif mutation == 'runtime':
        (runtime / 'extra.py').write_bytes(b'changed')
    elif mutation == 'dependency':
        (snapshot / 'dependencies/uv.lock').write_bytes(b'changed')
    elif mutation == 'license':
        (snapshot / 'LICENSE').write_bytes(b'changed')
    elif mutation == 'manifest':
        path = snapshot / 'manifest.json'
        value = json.loads(path.read_text())
        value['source_revision'] = 'renamed'
        path.write_text(json.dumps(value))
    else:
        (runtime / 'reference.step.gz').write_bytes(b'forbidden')
    with pytest.raises(ValueError):
        frozen.verify(snapshot, identity)


def test_existing_output_and_incompatible_interpreter_are_preserved(tmp_path, monkeypatch):
    runtime, snapshot, identity = artifact(tmp_path, monkeypatch)
    original = (snapshot / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='new grader'):
        frozen.freeze(tmp_path, runtime, 'revision', snapshot)
    assert (snapshot / 'manifest.json').read_bytes() == original
    monkeypatch.setattr(frozen, 'interpreter_magic', lambda _: 'wrong')
    output = tmp_path / 'incompatible'
    with pytest.raises(ValueError, match='bytecode'):
        frozen.freeze(tmp_path, runtime, 'revision', output)
    assert not output.exists()
    frozen.verify(snapshot, identity)


@pytest.mark.parametrize('mutation', ['step', 'report', 'fixture', 'pin', 'extra_output'])
def test_generated_entry_refuses_unsealed_outputs_or_fixtures(tmp_path, monkeypatch, mutation):
    _, snapshot, identity = artifact(tmp_path, monkeypatch)
    configuration, case_path, retained, destination, command = grading_case(
        tmp_path, snapshot, identity)
    if mutation == 'step':
        (retained / 'reconstruction.step').write_bytes(b'changed')
    elif mutation == 'report':
        (retained / 'report.json').write_bytes(b'changed')
    elif mutation == 'fixture':
        (Path(configuration['manifest_parent']) / 'reference.step').write_bytes(b'changed')
    elif mutation == 'pin':
        configuration['grader_manifest_sha256'] = '0' * 64
        case_path.write_text(json.dumps(configuration))
    else:
        (retained / 'reconstruction.stl').write_bytes(b'not in pinned outputs')
    result = run(command)
    assert result.returncode != 0
    assert not destination.exists()


def test_generated_entry_never_overwrites_existing_result(tmp_path, monkeypatch):
    _, snapshot, identity = artifact(tmp_path, monkeypatch)
    _, _, _, destination, command = grading_case(tmp_path, snapshot, identity)
    destination.parent.mkdir()
    destination.write_bytes(b'preserved result')
    result = run(command)
    assert result.returncode != 0
    assert destination.read_bytes() == b'preserved result'


def test_short_scratch_root_handles_deep_result_hierarchy(tmp_path, monkeypatch):
    _, snapshot, identity = artifact(tmp_path, monkeypatch)
    configuration, case_path, _, _, command = grading_case(tmp_path, snapshot, identity)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    configuration['scratch_root'] = str(scratch)
    case_path.write_text(json.dumps(configuration))
    destination = tmp_path / ('nested-result-' * 7) / ('attempt-' * 7) / 'result.json'
    command[-1] = str(destination)
    process = run(command)
    assert process.returncode == 0, process.stderr
    graded = json.loads(destination.read_text())
    assert graded['accepted']
    assert all(Path(value).is_relative_to(scratch)
               for value in graded['grader_environment'].values())
    assert not any(scratch.iterdir())


def test_grader_scratch_cannot_be_inside_sealed_artifact(tmp_path, monkeypatch):
    _, snapshot, identity = artifact(tmp_path, monkeypatch)
    configuration, case_path, _, destination, command = grading_case(tmp_path, snapshot, identity)
    configuration['scratch_root'] = str(snapshot / 'application')
    case_path.write_text(json.dumps(configuration))
    process = run(command)
    assert process.returncode != 0 and not destination.exists()
    frozen.verify(snapshot, identity)


def test_runtime_changed_during_grading_cannot_publish_result(tmp_path, monkeypatch):
    runtime, snapshot, identity = artifact(tmp_path, monkeypatch)
    configuration, case_path, _, destination, command = grading_case(tmp_path, snapshot, identity)
    configuration['case']['mutate'] = str(runtime / 'late-change.py')
    case_path.write_text(json.dumps(configuration))
    result = run(command)
    assert result.returncode != 0
    assert not destination.exists()


def test_unready_reference_is_fixture_failure_without_consuming_a_solve(tmp_path, monkeypatch):
    _, snapshot, identity = artifact(tmp_path, monkeypatch)
    configuration, case_path, _, destination, command = grading_case(tmp_path, snapshot, identity)
    reference = Path(configuration['manifest_parent']) / 'reference.step'
    reference.write_bytes(b'unready reference')
    configuration['case']['source_sha256'] = frozen.digest(reference)
    case_path.write_text(json.dumps(configuration))
    process = run(command)
    assert process.returncode == 0, process.stderr
    result = json.loads(destination.read_text())
    assert not result['accepted'] and result['evaluation_failure_kind'] == 'fixture'
    assert result['fixture_readiness']['reason'] == 'unrepresented_native_faces'
    assert result['actual_output_sha256'] == configuration['retained_sha256']
    assert result['second_conversion_not_run']
    assert 'retained_report_value' not in result


@pytest.mark.parametrize('ready', [True, False])
def test_readiness_only_qualifies_source_without_converter_outputs_or_stl(
    tmp_path, monkeypatch, ready,
):
    _, snapshot, identity = artifact(tmp_path, monkeypatch)
    configuration, case_path, retained, destination, _ = grading_case(tmp_path, snapshot, identity)
    reference = Path(configuration['manifest_parent']) / 'reference.step'
    if not ready:
        reference.write_bytes(b'unready reference')
    configuration['case']['source_sha256'] = frozen.digest(reference)
    configuration['case']['stl'] = 'unavailable-input.stl'
    configuration.pop('retained_sha256')
    case_path.write_text(json.dumps(configuration))
    before = {p.name: p.read_bytes() for p in retained.iterdir()}
    command = [sys.executable, '-I', '-B', str(snapshot / 'application/grader.pyc'),
               '--reference-readiness', str(case_path), str(destination)]
    process = run(command)
    assert process.returncode == 0, process.stderr
    result = json.loads(destination.read_text())
    assert result['reference_readiness_only'] and not result['accepted']
    assert result['fixture_readiness']['fixture_ready'] is ready
    assert result['second_conversion_not_run']
    assert result['fixture_sha256'] == {'source_step': configuration['case']['source_sha256']}
    assert result['actual_output_sha256'] == {}
    assert 'retained_report_value' not in result
    assert before == {p.name: p.read_bytes() for p in retained.iterdir()}


def test_runtime_changed_during_build_preserves_partial_artifact(tmp_path, monkeypatch):
    runtime, _, _ = artifact(tmp_path, monkeypatch)
    compile_original = frozen.compile_module

    def changing_compile(*args):
        compile_original(*args)
        (runtime / 'late-change.py').write_bytes(b'changed during build')

    monkeypatch.setattr(frozen, 'compile_module', changing_compile)
    partial = tmp_path / 'partial'
    with pytest.raises(ValueError, match='partial grader artifact preserved'):
        frozen.freeze(tmp_path, runtime, 'revision', partial)
    assert list((partial / 'application').rglob('*.pyc'))
    assert not (partial / 'manifest.json').exists()


def test_git_source_closure_reads_committed_grading_imports_and_declarations(monkeypatch, tmp_path):
    committed = {
        'tools/benchmark_multires_corpus.py': b'from tools import benchmark_surface_corpus\n',
        'tools/benchmark_surface_corpus.py': (
            b'def grade():\n    from tools.analysis import check\n'),
        'tools/analysis.py': b'def check(): return 42\n',
        'tools/freeze_evaluation_converter.py': b'import hashlib\n',
        'tools/reference_mesh.py': b'import math\n',
        'pyproject.toml': b'[project]\nname="exact-commit"\n', 'uv.lock': b'version = 1\n',
    }
    monkeypatch.setattr(frozen.converter, 'git_sources', lambda *_: (
        'resolved-full-commit', {'app/__init__.py': b'', 'LICENSE': b'license'}))
    calls = []

    def git_show(arguments, *, cwd):
        assert arguments[:2] == ['git', 'show'] and cwd == tmp_path
        assert arguments[2].startswith('resolved-full-commit:')
        name = arguments[2].split(':', 1)[1]
        calls.append(name)
        return committed[name]

    monkeypatch.setattr(frozen.subprocess, 'check_output', git_show)
    revision, sources, dependencies = frozen.git_sources(tmp_path, 'requested-tag')
    assert revision == 'resolved-full-commit'
    assert sources['tools/analysis.py'] == committed['tools/analysis.py']
    assert dependencies == {name: committed[name] for name in ('pyproject.toml', 'uv.lock')}
    assert len(calls) == len(set(calls)) == len(committed)


def test_changed_builder_helper_is_rejected_but_line_endings_are_recorded(tmp_path, monkeypatch):
    runtime, _, _ = artifact(tmp_path, monkeypatch)
    sources_function = frozen.git_sources

    def changed_helper(*args):
        revision, sources, declarations = sources_function(*args)
        return (revision, {**sources, 'tools/freeze_evaluation_converter.py': b'changed'},
                declarations)

    monkeypatch.setattr(frozen, 'git_sources', changed_helper)
    rejected = tmp_path / 'different-helper'
    with pytest.raises(ValueError, match='builder dependency'):
        frozen.freeze(tmp_path, runtime, 'revision', rejected)
    assert not rejected.exists()

    def crlf_helper(*args):
        revision, sources, declarations = sources_function(*args)
        helper = sources['tools/freeze_evaluation_converter.py'].replace(b'\r\n', b'\n')
        return revision, {**sources, 'tools/freeze_evaluation_converter.py':
                          helper.replace(b'\n', b'\r\n')}, declarations

    monkeypatch.setattr(frozen, 'git_sources', crlf_helper)
    accepted = tmp_path / 'same-helper-crlf'
    manifest = frozen.freeze(tmp_path, runtime, 'revision', accepted)
    assert manifest['builder_dependencies_sha256']['tools/freeze_evaluation_converter.py'] == (
        frozen.digest(Path(frozen.converter.__file__)))
    frozen.verify(accepted, frozen.digest(accepted / 'manifest.json'))
