"""Assemble source-grouped fixture collections without copying their geometry."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import uuid
from collections import defaultdict
from pathlib import Path

WITNESS_FIELDS = (
    'id', 'source_model_id', 'geometry_family', 'preset', 'source_step', 'source_sha256',
    'source_url', 'source_license', 'license_sha256', 'exporter', 'stl', 'input_sha256',
    'mesh_geometry_sha256', 'input_units', 'tessellation_tolerance_mm',
    'angular_tolerance_rad', 'relative', 'parallel', 'triangles', 'ground_truth',
)
PROVENANCE_FIELDS = ('source_url', 'source_license', 'license_sha256', 'exporter')


def required(mapping: dict, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(mapping, dict):
        raise ValueError(f'{label} missing required metadata object')
    missing = [field for field in fields if field not in mapping]
    if missing:
        raise ValueError(f'{label} missing required metadata: {", ".join(missing)}')


def positive_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def verify_contract(contract: dict) -> None:
    required(contract, ('presets', 'materializer_sha256', 'provenance'), 'Export contract')
    presets = contract['presets']
    if (not isinstance(presets, list) or len(presets) != 10 or any(
        not isinstance(pair, list) or len(pair) != 2 or not all(map(positive_number, pair))
        for pair in presets
    ) or any(a <= c or b <= d for (a, b), (c, d) in zip(
        presets[:-1], presets[1:], strict=True,
    ))):
        raise ValueError('Export contract must contain ten tightening linear/angular presets')
    provenance = contract['provenance']
    required(provenance, PROVENANCE_FIELDS, 'Export provenance')
    if any(not isinstance(provenance[field], str) or not provenance[field]
           for field in PROVENANCE_FIELDS if field != 'exporter'):
        raise ValueError('Export provenance must retain source/license references')
    versions = provenance['exporter']
    required(versions, ('cadquery', 'cadquery-ocp', 'trimesh'), 'Exporter versions')
    if any(not isinstance(value, str) or not value for value in versions.values()):
        raise ValueError('Exporter versions must be nonempty strings')


def verify_case_witness(case: dict, witness: dict, contract: dict) -> None:
    required(case, (*WITNESS_FIELDS, 'split'), 'Manifest case')
    required(witness, (*WITNESS_FIELDS, 'split'), 'Materialization witness')
    fields = set(case) | set(witness)
    fields -= {'split', 'historically_exposed'}
    required(case, tuple(sorted(fields)), 'Manifest case')
    required(witness, tuple(sorted(fields)), 'Materialization witness')
    for field in fields:
        if case[field] != witness[field]:
            raise ValueError(f'Manifest contradicts its materialization evidence: {field}')
    for field in PROVENANCE_FIELDS:
        if case[field] != contract['provenance'][field]:
            raise ValueError(f'Materialization witness contradicts export provenance: {field}')
    if (case['input_units'] != 'mm' or case['relative'] is not False
            or case['parallel'] is not False):
        raise ValueError('Materialization witness lacks absolute serial millimetre export settings')
    preset = case['preset']
    if preset not in {f'q{i:02d}' for i in range(10)}:
        raise ValueError('Materialization witness has an unknown preset')
    if [case['tessellation_tolerance_mm'], case['angular_tolerance_rad']] != contract['presets'][
        int(preset[1:])
    ]:
        raise ValueError('Materialization witness contradicts its tessellation preset')
    if (case['source_model_id'] != case['source_sha256'][:20]
            or case['id'] != f'{case["source_model_id"]}-{preset}'):
        raise ValueError('Materialization witness has inconsistent source/case identities')
    if not isinstance(case['triangles'], int) or isinstance(case['triangles'], bool) or (
        case['triangles'] <= 0
    ):
        raise ValueError('Materialization witness has an invalid triangle count')
    truth = case['ground_truth']
    required(truth, ('solid_count', 'volume_mm3', 'dimensions_mm', 'brep_face_type_counts'),
             'Materialization ground truth')
    dimensions = truth['dimensions_mm']
    counts = truth['brep_face_type_counts']
    if (not isinstance(truth['solid_count'], int) or isinstance(truth['solid_count'], bool)
            or truth['solid_count'] < 1 or not positive_number(truth['volume_mm3'])
            or not isinstance(dimensions, list) or len(dimensions) != 3
            or not all(map(positive_number, dimensions)) or not isinstance(counts, dict)
            or not counts or any(not isinstance(n, int) or isinstance(n, bool) or n < 1
                                 for n in counts.values())):
        raise ValueError('Materialization witness has invalid source geometry metadata')
    if case['split'] not in ('development', 'validation', 'holdout') or witness['split'] not in (
        'development', 'validation', 'holdout',
    ):
        raise ValueError('Materialization witness has an unknown split')
    if 'historically_exposed' in case and case['historically_exposed'] is not True:
        raise ValueError('Historical exposure must be an explicit true annotation')
    if case['split'] != witness['split'] and not (
        case.get('historically_exposed') is True and case['split'] == 'development'
    ):
        raise ValueError('Manifest changed its witnessed split without development exposure')
    if witness.get('historically_exposed') and case.get('historically_exposed') is not True:
        raise ValueError('Manifest erased witnessed historical exposure')


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_source_index(source: dict, case: dict, verified_paths: dict) -> None:
    fields = ('source_sha256', 'path', 'asset', 'source_url', 'source_commit',
              'git_blob_sha1', 'source_license', 'license_sha256', 'license_file',
              'family_label', 'geometry_family', 'split')
    required(source, fields, 'Source provenance index')
    if any(not isinstance(source[field], str) or not source[field] for field in fields):
        raise ValueError('Source provenance index has missing or invalid references')
    path = Path(source['path']).resolve()
    if path != Path(case['source_step']).resolve():
        raise ValueError('Source provenance index points to a different asset')
    if any(source[field] != case[field]
           for field in ('source_sha256', 'source_license', 'license_sha256')):
        raise ValueError('Source provenance index contradicts witnessed source/license identity')
    raw = path.read_bytes()
    blob = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw, usedforsecurity=False).hexdigest()
    if blob != source['git_blob_sha1']:
        raise ValueError('Source provenance index Git blob differs from actual source bytes')
    if len(source['source_commit']) != 40 or any(
        c not in '0123456789abcdef' for c in source['source_commit']
    ):
        raise ValueError('Source provenance index requires a full pinned commit')
    license_path = Path(source['license_file']).resolve()
    if license_path not in verified_paths:
        verified_paths[license_path] = digest(license_path)
    if verified_paths[license_path] != source['license_sha256']:
        raise ValueError('Pinned source license changed')
    family = hashlib.sha256(source['family_label'].encode()).hexdigest()
    if family != source['geometry_family']:
        raise ValueError('Family declaration has an inconsistent hash')
    number = int(family[:8], 16) % 10
    split = 'development' if number < 6 else 'validation' if number < 8 else 'holdout'
    if split != source['split']:
        raise ValueError('Family declaration has an inconsistent split')


def save_snapshot(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('Existing snapshot bytes changed')
        return
    # Repeating a SHA-256 target filename can exceed legacy Windows MAX_PATH
    # even when the final snapshot path is valid. Keep the unique temp short.
    temporary = path.with_name(f'.{uuid.uuid4().hex}.partial')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def assemble(
    collections: list[tuple[Path, int]], indices: list[Path],
    license_files: list[Path] | None = None,
) -> dict:
    overrides = {}
    index_contracts = []
    indexed_licenses = set()
    for path in indices:
        raw = path.read_bytes()
        index_contracts.append({'path': str(path.resolve()),
                                'sha256': hashlib.sha256(raw).hexdigest(),
                                'snapshot_text': raw.decode('utf-8')})
        for source in json.loads(raw)['sources']:
            identifier = source['source_sha256']
            if identifier in overrides and overrides[identifier] != source:
                raise ValueError('Conflicting source-family declarations')
            overrides[identifier] = source
            indexed_licenses.add(source['license_sha256'])
    accepted, exclusions, contracts = [], [], []
    seen_bytes, signatures = set(), []
    verified_paths, verified_licenses = {}, {}
    for path in license_files or []:
        path = path.resolve()
        verified_paths[path] = digest(path)
        verified_licenses[verified_paths[path]] = str(path)
    export_contract = None
    for manifest_path, quota in collections:
        if quota < 1:
            raise ValueError('Collection quotas must be positive')
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        contract = manifest['contract']
        verify_contract(contract)
        geometry_contract = {key: contract[key] for key in ('presets', 'materializer_sha256')}
        geometry_contract['exporter'] = contract['provenance']['exporter']
        if export_contract is not None and geometry_contract != export_contract:
            raise ValueError('Collections have different export geometry contracts')
        export_contract = geometry_contract
        grouped = defaultdict(list)
        for case in manifest['cases']:
            grouped[case['source_model_id']].append(case)
        collection_count, verified_records = 0, []
        for identifier, cases in grouped.items():
            if collection_count == quota:
                break
            source_hash = cases[0]['source_sha256']
            reason = None
            if (len(cases) != 10 or {c['preset'] for c in cases} != {
                f'q{i:02d}' for i in range(10)
            } or len({c['mesh_geometry_sha256'] for c in cases}) != 10
                    or len({c['input_sha256'] for c in cases}) != 10):
                reason = 'incomplete_or_repeated_tessellation_bundle'
            elif len({c['source_sha256'] for c in cases}) != 1:
                raise ValueError('Source identity changes inside one model bundle')
            if source_hash in seen_bytes:
                reason = 'duplicate_source_bytes'
            # Records remain beside the original mesh directory even when a
            # frozen manifest snapshot is supplied from another directory.
            mesh_path = Path(cases[0]['stl'])
            record_path = mesh_path.parent.parent / 'records' / f'{source_hash}.json'
            record_bytes = record_path.read_bytes()
            record = json.loads(record_bytes)
            if record['status'] != 'usable' or record['source_sha256'] != source_hash:
                raise ValueError('Bundle lacks its verified materialization record')
            witnesses = {row['id']: row for row in record['cases']}
            if len(record['cases']) != 10 or len(witnesses) != 10 or set(witnesses) != {
                case['id'] for case in cases
            }:
                raise ValueError('Bundle lacks ten distinct matching materialization witnesses')
            signature = record['geometry_signature']
            if len(signature) != 5 or not all(math.isfinite(v) for v in signature):
                raise ValueError('Invalid geometry signature')
            if reason is None and any(
                all(abs(a - b) <= max(abs(a), abs(b), 1e-12) * 1e-6
                    for a, b in zip(signature, previous, strict=True))
                for previous in signatures
            ):
                reason = 'duplicate_or_near_duplicate_geometry_signature'
            if reason:
                exclusions.append({'source_model_id': identifier, 'source_sha256': source_hash,
                                   'reason': reason, 'collection': str(manifest_path)})
                continue
            override = overrides.get(source_hash)
            if cases[0].get('license_sha256') in indexed_licenses and override is None:
                raise ValueError('Indexed collection lacks a source-family declaration')
            rows = []
            for case in sorted(cases, key=lambda c: c['preset']):
                witness = witnesses.get(case['id'], {})
                verify_case_witness(case, witness, contract)
                for key, expected in (('stl', 'input_sha256'), ('source_step', 'source_sha256')):
                    path = Path(case[key]).resolve()
                    if path not in verified_paths:
                        verified_paths[path] = digest(path)
                    if verified_paths[path] != case[expected]:
                        raise ValueError('Frozen source or STL bytes changed')
                row = dict(case)
                if override:
                    if not rows:
                        verify_source_index(override, case, verified_paths)
                        verified_licenses[override['license_sha256']] = str(
                            Path(override['license_file']).resolve())
                    row.update({key: override[key] for key in (
                        'geometry_family', 'family_label', 'split', 'source_url',
                        'source_commit', 'git_blob_sha1', 'source_license', 'license_sha256',
                    )})
                    row['source_asset'] = override['asset']
                rows.append(row)
            if len({c['geometry_family'] for c in rows}) != 1:
                raise ValueError('One model crosses geometry families')
            accepted.extend(rows)
            seen_bytes.add(source_hash)
            signatures.append(signature)
            verified_records.append({'source_sha256': source_hash,
                                     'path': str(record_path.resolve()),
                                     'sha256': hashlib.sha256(record_bytes).hexdigest()})
            collection_count += 1
        license_verified = contract['provenance']['license_sha256'] in verified_licenses
        if license_files and not license_verified:
            raise ValueError('No supplied/indexed license bytes match '
                             'the pinned collection license')
        contracts.append({'path': str(manifest_path.resolve()), 'quota': quota,
                          'accepted_models': collection_count,
                          'available_models': len(grouped),
                          'materialization_witnesses': verified_records,
                          'license_bytes_verified': license_verified,
                          'verified_license_file': verified_licenses.get(
                              contract['provenance']['license_sha256']),
                          'acquisition_exclusions': manifest.get('exclusions', []),
                          'sha256': hashlib.sha256(raw).hexdigest(), 'contract': contract,
                          'snapshot_text': raw.decode('utf-8')})
    exposed = {c['geometry_family'] for c in accepted if c.get('historically_exposed')}
    families = defaultdict(set)
    for case in accepted:
        if case['geometry_family'] in exposed:
            case.update(split='development', historically_exposed=True)
        families[case['geometry_family']].add(case['split'])
    if any(len(splits) != 1 for splits in families.values()):
        raise ValueError('Geometry family crosses splits')
    if len({c['id'] for c in accepted}) != len(accepted):
        raise ValueError('Duplicate case identities in composed corpus')
    target = sum(quota for _, quota in collections)
    return {'schema': 1, 'requested_models': target, 'requested_cases': target * 10,
            'assembly_contract': {'assembler_sha256': digest(Path(__file__)),
                                  'signature_relative_tolerance': 1e-6,
                                  'witness_fields_verified': list(WITNESS_FIELDS),
                                  'witness_settings_verified_against_contract': True,
                                  'license_metadata_policy': (
                                      'Every case matches record and contract. '
                                      'Actual license bytes require an index or '
                                      'supplied file providing the missing path.'
                                  )},
            'source_model_count': len(seen_bytes), 'case_count': len(accepted),
            'complete': all(c['accepted_models'] == c['quota'] for c in contracts),
            'geometry_family_count': len(families), 'cases': accepted,
            'collections': contracts, 'source_indices': index_contracts,
            'exclusions': exclusions,
            'deduplication': ('Source bytes plus volume/area/inertia signature rtol1e-6; '
                              'signature collisions excluded, not counted as new models')}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection', nargs=2, action='append', required=True,
                        metavar=('MANIFEST', 'MODEL_QUOTA'))
    parser.add_argument('--source-index', type=Path, action='append', default=[])
    parser.add_argument('--license-file', type=Path, action='append', default=[],
                        help='Rehash actual license text for contracts that retain only its digest')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--freeze', action='store_true', help='Require all collection quotas')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Preserve existing composed evidence; choose a new output path')
    report = assemble([(Path(path), int(quota)) for path, quota in args.collection],
                      args.source_index, args.license_file)
    if args.freeze and not report['complete']:
        raise ValueError('Cannot freeze an incomplete corpus; acquire replacement fixtures')
    if args.freeze and not all(c['license_bytes_verified'] for c in report['collections']):
        raise ValueError('Cannot freeze without pinned license bytes; provide --license-file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    snapshots = args.output.parent / (args.output.stem + '-inputs')
    snapshots.mkdir(exist_ok=True)
    for collection in report['collections']:
        snapshot = snapshots / (collection['sha256'] + '.json')
        save_snapshot(snapshot, collection.pop('snapshot_text').encode('utf-8'))
        collection['snapshot_path'] = str(snapshot.resolve())
        collection['snapshot_sha256'] = digest(snapshot)
    for index in report['source_indices']:
        snapshot = snapshots / (index['sha256'] + '-index.json')
        save_snapshot(snapshot, index.pop('snapshot_text').encode('utf-8'))
        index['snapshot_path'] = str(snapshot.resolve())
        index['snapshot_sha256'] = digest(snapshot)
    save_snapshot(args.output, (json.dumps(report, indent=2) + '\n').encode('utf-8'))
    print(f"{report['source_model_count']}/{report['requested_models']} distinct source models, "
          f"{report['case_count']}/{report['requested_cases']} inputs; "
          f"complete={report['complete']}")


if __name__ == '__main__':
    main()
