import json
from contextlib import contextmanager

import pytest

from tools.evaluation_identity import (
    EVALUATION_TOOLS,
    case_identity,
    digest,
    runtime_identity,
    source_identity,
)


def test_runtime_identity_survives_the_frozen_contract_json_roundtrip():
    runtime = runtime_identity()
    assert json.loads(json.dumps(runtime)) == runtime


def test_evaluation_identity_tracks_dependencies_without_unrelated_acquisition(tmp_path):
    (tmp_path / 'app').mkdir()
    (tmp_path / 'tools').mkdir()
    for name in ('pyproject.toml', 'uv.lock', 'app/fit.py',
                 *(f'tools/{name}' for name in EVALUATION_TOOLS)):
        (tmp_path / name).write_text('original')
    original = source_identity(tmp_path)
    (tmp_path / 'tools/acquire_kicad_step.py').write_text('new acquisition controller')
    (tmp_path / 'README.md').write_text('new documentation')
    assert source_identity(tmp_path) == original
    (tmp_path / 'app/fit.py').write_text('new fit')
    assert source_identity(tmp_path) != original
    (tmp_path / 'app/fit.py').write_text('original')
    (tmp_path / 'app/new_geometry.py').write_text('new geometry helper')
    assert source_identity(tmp_path) != original


def test_case_identity_changes_for_geometry_settings_code_and_runtime():
    case = {'input_sha256': 'stl-a', 'source_sha256': 'step-a', 'input_units': 'mm'}
    sources, runtime = {'app/fit.py': 'code-a'}, {'python': 'runtime-a'}
    original = case_identity(case, sources, runtime)
    assert original == case_identity(dict(case), dict(sources), dict(runtime))
    for key, value in (('input_sha256', 'stl-b'), ('source_sha256', 'step-b'),
                       ('input_units', 'm')):
        assert case_identity({**case, key: value}, sources, runtime) != original
    assert case_identity(case, {'app/fit.py': 'code-b'}, runtime) != original
    assert case_identity(case, sources, {'python': 'runtime-b'}) != original


def test_progressive_cache_reuses_only_identical_cases_and_preserves_real_revision(
    tmp_path, monkeypatch,
):
    # Protocol checks: the worker is replaced, so these bytes are not CAD fixtures
    # and are never included in reconstruction-yield evidence.
    import tools.benchmark_multires_corpus as benchmark

    stl, step = tmp_path / 'input.stl', tmp_path / 'reference.step'
    stl.write_bytes(b'protocol STL')
    step.write_bytes(b'protocol STEP')
    case = {'id': 'model-q00', 'source_model_id': 'model', 'split': 'development',
            'preset': 'q00', 'stl': str(stl), 'input_sha256': digest(stl),
            'source_step': str(step), 'source_sha256': digest(step)}
    first, expanded = tmp_path / 'first.json', tmp_path / 'expanded.json'
    first.write_text(json.dumps({'cases': [case]}))
    expanded.write_text(json.dumps({'cases': [case, {**case, 'id': 'model-q05', 'preset': 'q05'}]}))
    revision = ['conversion-head']
    runtime = {'python': 'runtime-a'}
    monkeypatch.setattr(benchmark, 'runtime_identity', lambda: dict(runtime))
    monkeypatch.setattr(benchmark, 'source_identity', lambda root: {'fit': 'code-a'})
    monkeypatch.setattr(benchmark.subprocess, 'check_output', lambda *a, **k: revision[0])
    calls = []

    @contextmanager
    def resources():
        yield {}

    def worker(command, **kwargs):
        output = command[command.index('--output') + 1]
        identifier = command[command.index('--worker-id') + 1]
        calls.append(identifier)
        from pathlib import Path
        Path(output).write_text(json.dumps({'id': identifier, 'accepted': True,
                                          'status': 'complete'}))
        return 0

    monkeypatch.setattr(benchmark, 'sampled_resources', resources)
    monkeypatch.setattr(benchmark, 'run_bounded_process', worker)

    def run(manifest, label, *extra):
        output = tmp_path / f'{label}-output.json'
        monkeypatch.setattr('sys.argv', ['benchmark', str(manifest), '--output', str(output),
                                        *extra])
        benchmark.main()
        return json.loads(output.read_text())

    assert run(first, 'first')['summary']['fresh_cases'] == 1
    revision[0] = 'documentation-head'
    report = run(expanded, 'expanded')
    assert calls == ['model-q00', 'model-q05']
    assert report['summary']['fresh_cases'] == report['summary']['reused_cases'] == 1
    assert report['results'][0]['reused_from'] == 'shared_cache'
    assert report['results'][0]['conversion_revision'] == 'conversion-head'
    assert report['revision'] == 'documentation-head'
    report = run(expanded, 'expanded')
    assert len(calls) == 2 and report['summary']['reused_cases'] == 2
    runtime['python'] = 'runtime-b'
    with pytest.raises(ValueError, match='contract changed'):
        run(expanded, 'expanded')
    runtime['python'] = 'runtime-a'
    report = run(expanded, 'fresh', '--fresh')
    assert len(calls) == 4 and report['summary']['fresh_cases'] == 2
    # A fresh run does not overwrite the original immutable shared record.
    report = run(first, 'another')
    assert report['results'][0]['conversion_revision'] == 'conversion-head'
    assert report['summary']['reused_cases'] == 1


def test_interrupted_worker_never_publishes_a_mislabeled_cache_record(tmp_path, monkeypatch):
    import tools.benchmark_multires_corpus as benchmark

    stl, step = tmp_path / 'input.stl', tmp_path / 'reference.step'
    stl.write_bytes(b'protocol STL')
    step.write_bytes(b'protocol STEP')
    case = {'id': 'model-q00', 'source_model_id': 'model', 'split': 'development',
            'preset': 'q00', 'stl': str(stl), 'input_sha256': digest(stl),
            'source_step': str(step), 'source_sha256': digest(step)}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'cases': [case]}))
    output = tmp_path / 'run.json'
    monkeypatch.setattr('sys.argv', ['benchmark', str(manifest), '--output', str(output)])
    sources = {'fit': 'original'}
    monkeypatch.setattr(benchmark, 'source_identity', lambda root: dict(sources))
    monkeypatch.setattr(benchmark, 'runtime_identity', lambda: {})
    monkeypatch.setattr(benchmark.subprocess, 'check_output', lambda *a, **k: 'head')

    @contextmanager
    def resources():
        yield {}

    attempts = []

    def worker(command, **kwargs):
        from pathlib import Path
        path = Path(command[command.index('--output') + 1])
        path.write_text(json.dumps({'id': case['id'], 'accepted': True, 'status': 'complete'}))
        attempts.append(path)
        if len(attempts) == 1:
            sources['fit'] = 'changed during conversion'
        return 0

    monkeypatch.setattr(benchmark, 'sampled_resources', resources)
    monkeypatch.setattr(benchmark, 'run_bounded_process', worker)
    with pytest.raises(RuntimeError, match='attempt preserved'):
        benchmark.main()
    assert attempts[0].exists()
    assert not (tmp_path / 'run_cases/model-q00.json').exists()
    assert not list((tmp_path / 'multires-case-cache').glob('*.json'))
    sources['fit'] = 'original'
    benchmark.main()
    assert attempts[0].exists() and len(attempts) == 2
    assert json.loads(output.read_text())['summary']['fresh_cases'] == 1
