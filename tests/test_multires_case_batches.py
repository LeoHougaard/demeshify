import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools.evaluation_identity import digest


def test_bounded_case_batches_resume_without_reconverting_or_hiding_failures(tmp_path, monkeypatch):
    import tools.benchmark_multires_corpus as benchmark

    # Protocol fixtures, not CAD assets or reconstruction-yield evidence.
    stl, step = tmp_path / 'input.stl', tmp_path / 'reference.step'
    stl.write_bytes(b'protocol STL')
    step.write_bytes(b'protocol STEP')
    cases = [{'id': f'model-q{i:02}', 'source_model_id': 'model', 'split': 'development',
              'preset': f'q{i:02}', 'stl': str(stl), 'input_sha256': digest(stl),
              'source_step': str(step), 'source_sha256': digest(step)} for i in range(5)]
    manifest, output = tmp_path / 'manifest.json', tmp_path / 'run.json'
    manifest.write_text(json.dumps({'cases': cases}))
    monkeypatch.setattr(benchmark, 'source_identity', lambda _: {'fit': 'stable'})
    monkeypatch.setattr(benchmark, 'runtime_identity', lambda: {})
    monkeypatch.setattr(benchmark.subprocess, 'check_output', lambda *a, **k: 'head')
    calls = []

    @contextmanager
    def resources():
        yield {}

    def worker(command, **_kwargs):
        identifier = command[command.index('--worker-id') + 1]
        calls.append(identifier)
        accepted = identifier != 'model-q01'
        Path(command[command.index('--output') + 1]).write_text(json.dumps({
            'id': identifier, 'accepted': accepted,
            'status': 'complete' if accepted else 'best_effort',
        }))
        return 0

    monkeypatch.setattr(benchmark, 'sampled_resources', resources)
    monkeypatch.setattr(benchmark, 'run_bounded_process', worker)

    def run(limit):
        monkeypatch.setattr('sys.argv', ['benchmark', str(manifest), '--output', str(output),
                                        '--batch-cases', str(limit)])
        benchmark.main()
        return json.loads(output.read_text())

    first = run(2)
    assert len(calls) == first['summary']['fresh_cases'] == 2
    assert not first['summary']['complete'] and first['summary']['evaluated_cases'] == 2
    assert first['summary']['strict_case_passes'] == 1
    second = run(1)
    assert len(calls) == second['summary']['evaluated_cases'] == 3
    assert second['summary']['fresh_cases'] == 1 and second['summary']['reused_cases'] == 2
    assert not second['summary']['complete']
    final = run(2)
    assert len(calls) == final['summary']['evaluated_cases'] == 5
    assert final['summary']['complete'] and final['summary']['fresh_cases'] == 2
    assert final['summary']['reused_cases'] == 3 and not final['results'][1]['accepted']
    resumed = run(1)
    assert len(calls) == 5 and resumed['summary']['complete']
    assert resumed['summary']['fresh_cases'] == 0 and resumed['summary']['reused_cases'] == 5
    launches = json.loads(output.with_suffix('.launches.json').read_text())
    assert [row['batch_cases'] for row in launches] == [2, 1, 2, 1]


@pytest.mark.parametrize('limit', [0, -1])
def test_nonpositive_case_batch_is_rejected_before_opening_inputs(tmp_path, monkeypatch, limit):
    import tools.benchmark_multires_corpus as benchmark

    output = tmp_path / 'never.json'
    monkeypatch.setattr('sys.argv', ['benchmark', 'missing-manifest.json', '--output', str(output),
                                    '--batch-cases', str(limit)])
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2 and not output.exists()
