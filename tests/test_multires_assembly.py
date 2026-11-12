import hashlib
import json
import sys
from pathlib import Path

import pytest

from tools.assemble_multires_corpus import assemble, digest, main


def collection(root: Path, label: str, signature=1.0, source_bytes=None):
    """Protocol fixtures only; native exports are covered in test_multires_corpus."""
    root.mkdir()
    source = root / 'source.step'
    source.write_bytes(source_bytes or label.encode())
    sha = digest(source)
    identifier = sha[:20]
    mesh_dir = root / identifier
    mesh_dir.mkdir()
    rows = []
    for i in range(10):
        mesh = mesh_dir / f'q{i:02d}.stl'
        mesh.write_text(f'protocol-test-{label}-{i}')
        rows.append({'id': f'{identifier}-q{i:02d}', 'source_model_id': identifier,
                     'source_sha256': sha, 'source_step': str(source),
                     'stl': str(mesh), 'input_sha256': digest(mesh),
                     'mesh_geometry_sha256': digest(mesh), 'preset': f'q{i:02d}',
                     'geometry_family': label, 'split': 'development',
                     'license_sha256': 'fixture-license'})
    records = root / 'records'
    records.mkdir()
    (records / f'{sha}.json').write_text(json.dumps({
        'source_sha256': sha, 'status': 'usable', 'cases': rows,
        'geometry_signature': [signature, signature + 1, signature + 2,
                               signature + 3, signature + 4]}))
    manifest = root / 'manifest.json'
    # CRLF exercises byte-exact snapshots across Windows text modes.
    manifest.write_bytes((json.dumps({'contract': {
        'presets': list(range(10)), 'materializer_sha256': 'test-exporter',
        'provenance': {'exporter': {'cadquery': 'test-version'}}},
        'cases': rows}, indent=2) + '\n').replace('\n', '\r\n').encode())
    return manifest, rows


def test_composition_keeps_full_target_and_all_ten_variants(tmp_path):
    a, _ = collection(tmp_path / 'a', 'a')
    b, _ = collection(tmp_path / 'b', 'b', signature=2)
    report = assemble([(a, 980), (b, 20)], [])
    assert report['requested_models'] == 1000 and report['requested_cases'] == 10000
    assert report['source_model_count'] == 2 and report['case_count'] == 20
    assert not report['complete']
    assert {row['preset'] for row in report['cases']} == {f'q{i:02d}' for i in range(10)}


@pytest.mark.parametrize('same_bytes', [False, True])
def test_composition_excludes_renamed_or_equivalent_geometry(tmp_path, same_bytes):
    a, _ = collection(tmp_path / 'a', 'first')
    b, _ = collection(tmp_path / 'b', 'second', signature=1.0000001,
                      source_bytes=b'first' if same_bytes else None)
    report = assemble([(a, 1), (b, 1)], [])
    assert report['source_model_count'] == 1 and report['case_count'] == 10
    assert not report['complete']
    assert report['exclusions'][0]['reason'] == (
        'duplicate_source_bytes' if same_bytes
        else 'duplicate_or_near_duplicate_geometry_signature')


def test_indexed_family_applies_to_every_variant_and_retains_license(tmp_path):
    manifest, rows = collection(tmp_path / 'a', 'first')
    license_file = tmp_path / 'LICENSE'
    license_file.write_text('test license')
    label = 'one-related-component-family'
    family = hashlib.sha256(label.encode()).hexdigest()
    number = int(family[:8], 16) % 10
    split = 'development' if number < 6 else 'validation' if number < 8 else 'holdout'
    index = tmp_path / 'index.json'
    index.write_text(json.dumps({'sources': [{
        'source_sha256': rows[0]['source_sha256'], 'family_label': label,
        'geometry_family': family, 'split': split, 'source_url': 'https://example.test/model',
        'source_commit': 'pinned', 'git_blob_sha1': 'fixture', 'source_license': 'test',
        'license_sha256': digest(license_file), 'license_file': str(license_file),
        'asset': 'model.step'}]}))
    report = assemble([(manifest, 1)], [index])
    assert report['complete']
    assert {(c['geometry_family'], c['split']) for c in report['cases']} == {(family, split)}
    assert all(c['license_sha256'] == digest(license_file) for c in report['cases'])


def test_changed_mesh_is_rejected_instead_of_reusing_stale_identity(tmp_path):
    manifest, rows = collection(tmp_path / 'a', 'first')
    Path(rows[0]['stl']).write_text('changed')
    with pytest.raises(ValueError, match='Frozen source or STL bytes changed'):
        assemble([(manifest, 1)], [])


def test_manifest_cannot_replace_materialization_evidence(tmp_path):
    manifest, _ = collection(tmp_path / 'a', 'first')
    data = json.loads(manifest.read_text())
    data['cases'][0]['mesh_geometry_sha256'] = 'unverified-geometry'
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='contradicts its materialization evidence'):
        assemble([(manifest, 1)], [])


def test_cli_preserves_exact_snapshot_and_refuses_incomplete_freeze(tmp_path, monkeypatch):
    manifest, _ = collection(tmp_path / 'a', 'first')
    output = tmp_path / 'combined.json'
    argv = ['assemble', '--collection', str(manifest), '2', '--output', str(output), '--freeze']
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(ValueError, match='Cannot freeze an incomplete'):
        main()
    assert not output.exists()
    argv.remove('--freeze')
    main()
    report = json.loads(output.read_text())
    snapshot = Path(report['collections'][0]['snapshot_path'])
    assert snapshot.read_bytes() == manifest.read_bytes()
    assert report['collections'][0]['snapshot_sha256'] == digest(manifest)
    assert assemble([(snapshot, 2)], [])['cases'] == report['cases']
    with pytest.raises(ValueError, match='Preserve existing composed evidence'):
        main()
