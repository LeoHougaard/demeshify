import hashlib
import json
import sys
from pathlib import Path

import pytest

from tools.assemble_multires_corpus import assemble, digest, main, save_snapshot


def collection(root: Path, label: str, signature=1.0, source_bytes=None):
    """Protocol fixtures only; native exports are covered in test_multires_corpus."""
    root.mkdir()
    source = root / 'source.step'
    source.write_bytes(source_bytes or label.encode())
    sha = digest(source)
    identifier = sha[:20]
    mesh_dir = root / identifier
    mesh_dir.mkdir()
    license_file = root / 'LICENSE'
    license_file.write_text('fixture license')
    provenance = {'source_url': 'https://example.test/collection', 'source_license': 'test',
                  'license_sha256': digest(license_file),
                  'exporter': {name: 'test-version' for name in (
                      'cadquery', 'cadquery-ocp', 'trimesh',
                  )}}
    presets = [[1 / (i + 1), .5 / (i + 1)] for i in range(10)]
    rows = []
    for i in range(10):
        mesh = mesh_dir / f'q{i:02d}.stl'
        mesh.write_text(f'protocol-test-{label}-{i}')
        rows.append({'id': f'{identifier}-q{i:02d}', 'source_model_id': identifier,
                     'source_sha256': sha, 'source_step': str(source),
                     'stl': str(mesh), 'input_sha256': digest(mesh),
                     'mesh_geometry_sha256': digest(mesh), 'preset': f'q{i:02d}',
                     'geometry_family': label, 'split': 'development',
                     **provenance, 'input_units': 'mm', 'relative': False, 'parallel': False,
                     'tessellation_tolerance_mm': presets[i][0],
                     'angular_tolerance_rad': presets[i][1], 'triangles': 10 + i,
                     'ground_truth': {'solid_count': 1, 'volume_mm3': 1,
                                      'dimensions_mm': [1, 2, 3],
                                      'brep_face_type_counts': {'CYLINDER': 1}}})
    records = root / 'records'
    records.mkdir()
    (records / f'{sha}.json').write_text(json.dumps({
        'source_sha256': sha, 'status': 'usable', 'cases': rows,
        'geometry_signature': [signature, signature + 1, signature + 2,
                               signature + 3, signature + 4]}))
    manifest = root / 'manifest.json'
    # CRLF exercises byte-exact snapshots across Windows text modes.
    manifest.write_bytes((json.dumps({'contract': {
        'presets': presets, 'materializer_sha256': 'test-exporter',
        'provenance': provenance},
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
    license_file = tmp_path / 'a/LICENSE'
    label = 'one-related-component-family'
    family = hashlib.sha256(label.encode()).hexdigest()
    number = int(family[:8], 16) % 10
    split = 'development' if number < 6 else 'validation' if number < 8 else 'holdout'
    index = tmp_path / 'index.json'
    raw_source = Path(rows[0]['source_step']).read_bytes()
    blob = hashlib.sha1(f'blob {len(raw_source)}\0'.encode() + raw_source,
                        usedforsecurity=False).hexdigest()
    index.write_text(json.dumps({'sources': [{
        'source_sha256': rows[0]['source_sha256'], 'family_label': label,
        'geometry_family': family, 'split': split, 'source_url': 'https://example.test/model',
        'path': rows[0]['source_step'], 'source_commit': 'c' * 40, 'git_blob_sha1': blob,
        'source_license': 'test',
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


@pytest.mark.parametrize('field,value', [
    ('input_units', 'in'), ('tessellation_tolerance_mm', .123),
    ('angular_tolerance_rad', .321), ('relative', True), ('parallel', True),
    ('exporter', {'cadquery': 'altered', 'cadquery-ocp': 'altered', 'trimesh': 'altered'}),
    ('source_url', 'https://example.test/altered-source'), ('source_license', 'altered license'),
    ('license_sha256', '0' * 64), ('triangles', 12345),
    ('ground_truth', {'solid_count': 2, 'volume_mm3': 10, 'dimensions_mm': [1, 2, 3],
                      'brep_face_type_counts': {'CYLINDER': 2}}),
])
def test_export_metadata_changes_are_rejected_even_when_geometry_hashes_match(
    tmp_path, field, value,
):
    manifest, _ = collection(tmp_path / 'a', 'first')
    document = json.loads(manifest.read_text())
    document['cases'][0][field] = value
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError, match=f'materialization evidence: {field}'):
        assemble([(manifest, 1)], [])


@pytest.mark.parametrize('field', [
    'input_units', 'tessellation_tolerance_mm', 'exporter', 'ground_truth',
])
def test_historical_witness_missing_metadata_is_refused_instead_of_inferred(tmp_path, field):
    manifest, rows = collection(tmp_path / 'a', 'first')
    record = tmp_path / 'a/records' / (rows[0]['source_sha256'] + '.json')
    document = json.loads(record.read_text())
    del document['cases'][0][field]
    record.write_text(json.dumps(document))
    message = f'Materialization witness missing required metadata: {field}'
    with pytest.raises(ValueError, match=message):
        assemble([(manifest, 1)], [])


@pytest.mark.parametrize('field,value,message', [
    ('input_units', 'in', 'millimetre export settings'),
    ('relative', 0, 'millimetre export settings'),
    ('tessellation_tolerance_mm', .123, 'tessellation preset'),
    ('angular_tolerance_rad', .123, 'tessellation preset'),
    ('exporter', {'cadquery': 'wrong', 'cadquery-ocp': 'wrong', 'trimesh': 'wrong'},
     'export provenance: exporter'),
    ('license_sha256', '0' * 64, 'export provenance: license_sha256'),
])
def test_matching_manifest_and_witness_still_must_obey_frozen_export_contract(
    tmp_path, field, value, message,
):
    manifest, rows = collection(tmp_path / 'a', 'first')
    record = tmp_path / 'a/records' / (rows[0]['source_sha256'] + '.json')
    for path in (manifest, record):
        document = json.loads(path.read_text())
        document['cases'][0][field] = value
        path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match=message):
        assemble([(manifest, 1)], [])


def test_unwitnessed_extra_provenance_cannot_be_copied_as_verified_metadata(tmp_path):
    manifest, _ = collection(tmp_path / 'a', 'first')
    document = json.loads(manifest.read_text())
    document['cases'][0]['source_commit'] = 'unwitnessed'
    manifest.write_text(json.dumps(document))
    message = 'Materialization witness missing required metadata: source_commit'
    with pytest.raises(ValueError, match=message):
        assemble([(manifest, 1)], [])


def test_historical_exposure_can_only_move_a_witnessed_split_to_development(tmp_path):
    manifest, rows = collection(tmp_path / 'a', 'first')
    record = tmp_path / 'a/records' / (rows[0]['source_sha256'] + '.json')
    witness = json.loads(record.read_text())
    for row in witness['cases']:
        row['split'] = 'holdout'
    record.write_text(json.dumps(witness))
    document = json.loads(manifest.read_text())
    for row in document['cases']:
        row.update(split='development', historically_exposed=True)
    manifest.write_text(json.dumps(document))
    assert all(row['split'] == 'development' for row in assemble([(manifest, 1)], [])['cases'])
    document['cases'][0]['split'] = 'validation'
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='witnessed split'):
        assemble([(manifest, 1)], [])


def test_complete_freeze_requires_license_bytes_and_records_exact_witness_receipt(
    tmp_path, monkeypatch,
):
    manifest, rows = collection(tmp_path / 'a', 'first')
    output = tmp_path / 'complete.json'
    argv = ['assemble', '--collection', str(manifest), '1', '--output', str(output), '--freeze']
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(ValueError, match='pinned license bytes'):
        main()
    assert not output.exists()
    argv.extend(['--license-file', str(tmp_path / 'a/LICENSE')])
    main()
    document = json.loads(output.read_text())
    contract = document['collections'][0]
    assert document['complete'] and contract['license_bytes_verified']
    receipt = contract['materialization_witnesses'][0]
    record = tmp_path / 'a/records' / (rows[0]['source_sha256'] + '.json')
    assert receipt['sha256'] == digest(record)
    assert receipt['source_sha256'] == rows[0]['source_sha256']


def test_supplied_license_bytes_must_match_the_witnessed_contract(tmp_path):
    manifest, _ = collection(tmp_path / 'a', 'first')
    license_file = tmp_path / 'wrong-license'
    license_file.write_text('not the witnessed license')
    with pytest.raises(ValueError, match='No supplied/indexed license bytes match'):
        assemble([(manifest, 1)], [], [license_file])


def test_snapshot_atomic_temp_fits_windows_path_budget_for_a_hashed_target(tmp_path, monkeypatch):
    # Reproduce the actual nested evidence failure, without changing Windows
    # path policy. The final target fits; repeating its hash in the temp did not.
    root_length = len(str(tmp_path.resolve()))
    if root_length >= 180:
        pytest.skip('The test temp root leaves insufficient legacy Windows path budget')
    directory = tmp_path / ('x' * (180 - root_length - 1))
    directory.mkdir()
    target = directory / ('a' * 64 + '.json')
    assert len(str(target.resolve())) < 260
    original_write = Path.write_bytes

    def legacy_write(path, data):
        if len(str(path.resolve())) >= 260:
            raise FileNotFoundError('Legacy Windows path budget exceeded')
        return original_write(path, data)

    monkeypatch.setattr(Path, 'write_bytes', legacy_write)
    save_snapshot(target, b'preserved snapshot')
    assert target.read_bytes() == b'preserved snapshot'
    assert list(directory.iterdir()) == [target]
