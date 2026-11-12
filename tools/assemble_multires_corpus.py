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


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_snapshot(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('Existing snapshot bytes changed')
        return
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.partial')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def assemble(collections: list[tuple[Path, int]], indices: list[Path]) -> dict:
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
    verified_paths = {}
    export_contract = None
    for manifest_path, quota in collections:
        if quota < 1:
            raise ValueError('Collection quotas must be positive')
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        contract = manifest['contract']
        geometry_contract = {key: contract[key] for key in ('presets', 'materializer_sha256')}
        geometry_contract['exporter'] = contract['provenance']['exporter']
        if export_contract is not None and geometry_contract != export_contract:
            raise ValueError('Collections have different export geometry contracts')
        export_contract = geometry_contract
        grouped = defaultdict(list)
        for case in manifest['cases']:
            grouped[case['source_model_id']].append(case)
        collection_count = 0
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
            record = json.loads(record_path.read_text())
            if record['status'] != 'usable' or record['source_sha256'] != source_hash:
                raise ValueError('Bundle lacks its verified materialization record')
            witnesses = {row['id']: row for row in record['cases']}
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
                if any(case[key] != witness.get(key) for key in (
                    'source_sha256', 'input_sha256', 'mesh_geometry_sha256', 'preset',
                )):
                    raise ValueError('Manifest contradicts its materialization evidence')
                for key, expected in (('stl', 'input_sha256'), ('source_step', 'source_sha256')):
                    path = Path(case[key]).resolve()
                    if path not in verified_paths:
                        verified_paths[path] = digest(path)
                    if verified_paths[path] != case[expected]:
                        raise ValueError('Frozen source or STL bytes changed')
                row = dict(case)
                if override:
                    family = hashlib.sha256(override['family_label'].encode()).hexdigest()
                    if family != override['geometry_family']:
                        raise ValueError('Family declaration has an inconsistent hash')
                    number = int(family[:8], 16) % 10
                    split = ('development' if number < 6 else 'validation' if number < 8
                             else 'holdout')
                    if split != override['split']:
                        raise ValueError('Family declaration has an inconsistent split')
                    if digest(Path(override['license_file'])) != override['license_sha256']:
                        raise ValueError('Pinned source license changed')
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
            collection_count += 1
        contracts.append({'path': str(manifest_path.resolve()), 'quota': quota,
                          'accepted_models': collection_count,
                          'available_models': len(grouped),
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
                                  'signature_relative_tolerance': 1e-6},
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
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--freeze', action='store_true', help='Require all collection quotas')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Preserve existing composed evidence; choose a new output path')
    report = assemble([(Path(path), int(quota)) for path, quota in args.collection],
                      args.source_index)
    if args.freeze and not report['complete']:
        raise ValueError('Cannot freeze an incomplete corpus; acquire replacement fixtures')
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
