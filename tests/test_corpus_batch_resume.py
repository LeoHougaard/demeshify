import hashlib
import json
import sys
from pathlib import Path

import pytest

from tools import build_multires_corpus as corpus


def controller(tmp_path, monkeypatch, *, prior_source=None):
    inputs, output = tmp_path / 'sources', tmp_path / 'fixtures'
    inputs.mkdir()
    output.mkdir()
    license_path = tmp_path / 'LICENSE'
    license_path.write_text('test-only fixture provenance')
    sources = [inputs / f'model-{i}.step' for i in range(4)]
    for index, source in enumerate(sources):
        source.write_text(str(index))
    sources.sort(key=lambda p: hashlib.sha256(p.stem.encode()).hexdigest())
    calls = []

    def worker(command, **kwargs):
        source = Path(command[2])
        calls.append(source.name)
        identifier = corpus.digest(source)
        stl = output / (identifier + '.stl')
        stl.write_bytes(b'exported-test-mesh')
        record = {'status': 'usable', 'source_sha256': identifier,
                  'geometry_signature': [sources.index(source)], 'cases': [
                      {'id': f'{identifier}-q{i:02d}', 'source_model_id': identifier,
                       'geometry_family': identifier, 'stl': str(stl),
                       'input_sha256': corpus.digest(stl)} for i in range(10)]}
        corpus.save(output / 'worker-result.json', record)
        return 0

    monkeypatch.setattr(corpus, 'run_bounded_process', worker)
    if prior_source is not None:
        monkeypatch.setattr(corpus.subprocess, 'check_output', lambda *a, **k: prior_source)
    argv = ['build_multires_corpus.py', str(inputs), '--output', str(output), '--models', '4',
            '--source-url', 'https://example.test/license', '--source-license', 'test-only',
            '--license-file', str(license_path), '--batch-models', '1']
    monkeypatch.setattr(sys, 'argv', argv)
    return output, calls, argv


def test_batch_counts_new_acceptances_and_retains_full_target(tmp_path, monkeypatch):
    output, calls, _ = controller(tmp_path, monkeypatch)
    corpus.main()
    first = json.loads((output / 'manifest.json').read_text())
    assert first['source_model_count'] == 1
    assert first['requested_models'] == 4 and not first['complete']
    assert first['batch_new_models'] == 1
    corpus.main()
    second = json.loads((output / 'manifest.json').read_text())
    assert second['source_model_count'] == 2
    assert second['case_count'] == 20 and not second['complete']
    assert second['batch_new_models'] == 1
    assert len(calls) == 2  # Cached source was not exported again.


def test_interrupted_cache_replay_preserves_larger_manifest(tmp_path, monkeypatch):
    output, _, _ = controller(tmp_path, monkeypatch)
    corpus.main()
    corpus.main()
    original = (output / 'manifest.json').read_bytes()
    original_digest = corpus.digest
    seen = 0

    def interrupted(path):
        nonlocal seen
        if path.suffix == '.step':
            seen += 1
            if seen == 2:
                raise RuntimeError('interrupted cached scan')
        return original_digest(path)

    monkeypatch.setattr(corpus, 'digest', interrupted)
    with pytest.raises(RuntimeError, match='interrupted cached scan'):
        corpus.main()
    assert (output / 'manifest.json').read_bytes() == original


@pytest.mark.parametrize('change', ['controller', 'export'])
def test_explicit_controller_migration_checks_frozen_export_semantics(
    tmp_path, monkeypatch, change,
):
    current = Path(corpus.__file__).read_bytes()
    prior = (current.replace(b'--batch-models', b'--prior-batch') if change == 'controller'
             else current.replace(b'relative=False', b'relative=True'))
    output, _, argv = controller(tmp_path, monkeypatch, prior_source=prior)
    corpus.main()
    contract_path = output / 'contract.json'
    contract = json.loads(contract_path.read_text())
    contract['materializer_sha256'] = hashlib.sha256(prior).hexdigest()
    corpus.save(contract_path, contract)
    argv.extend(['--resume-controller-from', 'test-prior'])
    if change == 'export':
        original = contract_path.read_bytes()
        with pytest.raises(ValueError, match='Export semantics changed'):
            corpus.main()
        assert contract_path.read_bytes() == original
    else:
        corpus.main()
        assert json.loads(contract_path.read_text())['materializer_sha256'] == corpus.digest(
            Path(corpus.__file__))
        assert list((output / 'contract-history').glob('*-migration.json'))
