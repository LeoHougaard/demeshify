"""Serial, resumable STEP acquisition and ten-quality fixture materialization.

Geometry and generated manifests stay local. This does not score the converter.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.jobs import run_bounded_process  # noqa: E402
from app.storage import atomic_write_text  # noqa: E402

# Absolute millimetres and radians. Both restrictions tighten at every level.
PRESETS = tuple(zip(
    (0.20, 0.14, 0.10, 0.07, 0.05, 0.035, 0.025, 0.015, 0.008, 0.004),
    (0.65, 0.50, 0.38, 0.29, 0.22, 0.17, 0.13, 0.10, 0.07, 0.045), strict=True,
))


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, indent=2) + '\n')


def mesh_digest(mesh) -> str:
    """Ignore STL headers, triangle ordering, winding and vertex numbering."""
    import numpy as np

    triangles = np.asarray(mesh.triangles, dtype='<f4').copy()
    # Sorting structured coordinates orders whole vertices, not each axis.
    points = triangles.view([('x', '<f4'), ('y', '<f4'), ('z', '<f4')]).reshape(-1, 3)
    points.sort(axis=1)
    rows = triangles.reshape(-1, 9)
    rows = rows[np.lexsort(rows.T[::-1])]
    return hashlib.sha256(rows.tobytes()).hexdigest()


def materialize(source: Path, directory: Path, provenance: dict) -> dict:
    import cadquery as cq
    import numpy as np
    import trimesh
    from OCP.BRepTools import BRepTools

    from app.step import load_step_shape, unowned_geometry_counts

    shape = load_step_shape(source)
    solids = shape.Solids()
    if (not solids or not shape.isValid() or shape.Volume() <= 0
            or unowned_geometry_counts(shape)):
        raise ValueError('Invalid or empty reference solid')
    box = shape.BoundingBox()
    counts = dict(Counter(face.geomType() for face in shape.Faces()))
    # Conservative duplicate screen: collisions are excluded, never counted as
    # additional designs. Rigid transforms retain volume, area and inertia.
    inertia = np.linalg.eigvalsh(np.asarray(cq.Shape.matrixOfInertia(shape)))
    signature = [float(f'{v:.7g}') for v in (shape.Volume(), shape.Area(), *inertia)]
    family = source.stem.rsplit('_', 1)[0]
    family_hash = hashlib.sha256(family.encode()).hexdigest()
    split_number = int(family_hash[:8], 16) % 10
    split = 'development' if split_number < 6 else 'validation' if split_number < 8 else 'holdout'
    source_hash = digest(source)
    identifier = source_hash[:20]
    target = directory / identifier
    target.mkdir(exist_ok=True)
    cases, fingerprints = [], set()
    for index, (linear, angular) in enumerate(PRESETS):
        # OCCT retains prior triangulations; clean before EVERY export so the
        # preset is independent of execution order and previous finer meshes.
        BRepTools.Clean_s(shape.wrapped)
        path = target / f'q{index:02d}.stl'
        if not shape.exportStl(str(path), tolerance=linear, angularTolerance=angular,
                               relative=False, parallel=False):
            raise ValueError(f'STL export failed at q{index:02d}')
        mesh = trimesh.load(path, force='mesh', process=True)
        if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0:
            raise ValueError(f'Unusable STL fixture at q{index:02d}')
        fingerprint = mesh_digest(mesh)
        if fingerprint in fingerprints:
            raise ValueError(f'Presets produce identical triangle geometry at q{index:02d}')
        fingerprints.add(fingerprint)
        cases.append({
            'id': f'{identifier}-q{index:02d}', 'source_model_id': identifier,
            'geometry_family': family_hash, 'split': split, 'preset': f'q{index:02d}',
            'source_step': str(source.resolve()), 'source_sha256': source_hash,
            **provenance, 'stl': str(path.resolve()), 'input_sha256': digest(path),
            'mesh_geometry_sha256': fingerprint, 'input_units': 'mm',
            'tessellation_tolerance_mm': linear, 'angular_tolerance_rad': angular,
            'relative': False, 'parallel': False, 'triangles': len(mesh.faces),
            'ground_truth': {'solid_count': len(solids), 'volume_mm3': shape.Volume(),
                             'dimensions_mm': [box.xlen, box.ylen, box.zlen],
                             'brep_face_type_counts': counts},
        })
    return {'source_sha256': source_hash, 'geometry_signature': signature,
            'cases': cases, 'status': 'usable'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', type=int, default=1000)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--license-file', type=Path, required=True)
    parser.add_argument('--source-license', required=True)
    parser.add_argument('--known-manifest', type=Path, action='append', default=[])
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    provenance = {'source_url': args.source_url, 'source_license': args.source_license,
                  'license_sha256': digest(args.license_file),
                  'exporter': {name: importlib.metadata.version(name)
                               for name in ('cadquery', 'cadquery-ocp', 'trimesh')}}
    if args.worker:
        try:
            record = materialize(args.input, args.output, provenance)
        except Exception as exc:
            record = {'status': 'excluded', 'error': f'{type(exc).__name__}: {exc}'}
        save(args.output / 'worker-result.json', record)
        return
    known_families = set()
    for path in args.known_manifest:
        for case in json.loads(path.read_text())['cases']:
            if case.get('source_step'):
                family = Path(case['source_step']).stem.rsplit('_', 1)[0]
                known_families.add(hashlib.sha256(family.encode()).hexdigest())
    contract = {'presets': PRESETS, 'provenance': provenance, 'schema': 1,
                'materializer_sha256': digest(Path(__file__)),
                'known_manifest_sha256': [digest(p) for p in args.known_manifest]}
    contract_path = args.output / 'contract.json'
    if contract_path.exists() and json.loads(contract_path.read_text()) != json.loads(
        json.dumps(contract)
    ):
        raise ValueError('Frozen export contract changed; use a new output directory')
    save(contract_path, contract)
    sources = sorted(args.input.glob('*.st*p'), key=lambda p: hashlib.sha256(
        p.stem.encode()).hexdigest())
    records_dir = args.output / 'records'
    records_dir.mkdir(exist_ok=True)
    accepted, excluded, file_hashes, signatures = [], [], set(), set()
    for index, source in enumerate(sources):
        if len(accepted) == args.models:
            break
        if shutil.disk_usage(args.output).free < 15 * 1024**3:
            raise RuntimeError('Storage floor reached; preserved resumable records')
        source_hash = digest(source)
        if source_hash in file_hashes:
            excluded.append({'source': source.name, 'reason': 'duplicate_bytes'})
            continue
        file_hashes.add(source_hash)
        record_path = records_dir / f'{source_hash}.json'
        if record_path.exists():
            record = json.loads(record_path.read_text())
            if record.get('status') == 'usable' and any(
                not Path(c['stl']).is_file() or digest(Path(c['stl'])) != c['input_sha256']
                for c in record['cases']
            ):
                raise ValueError('Frozen fixture changed; restore it or use a new output directory')
        else:
            command = [sys.executable, str(Path(__file__).resolve()), str(source),
                       '--output', str(args.output), '--source-url', args.source_url,
                       '--source-license', args.source_license,
                       '--license-file', str(args.license_file), '--worker']
            result_path = args.output / 'worker-result.json'
            result_path.unlink(missing_ok=True)
            try:
                code = run_bounded_process(
                    command, cwd=ROOT, timeout=180,
                    stdout_path=args.output / 'export.stdout.log',
                    stderr_path=args.output / 'export.stderr.log',
                )
                if code:
                    raise RuntimeError(f'Exporter process exit {code}')
                record = json.loads(result_path.read_text())
            except (subprocess.TimeoutExpired, RuntimeError) as exc:
                record = {'status': 'excluded', 'error': str(exc)}
            save(record_path, record)
        signature = json.dumps(record.get('geometry_signature'))
        if record['status'] != 'usable' or signature in signatures:
            excluded.append({'source': source.name, 'source_sha256': source_hash,
                             'reason': record.get('error', 'duplicate_geometry_signature')})
        else:
            signatures.add(signature)
            for case in record['cases']:
                if case['geometry_family'] in known_families:
                    case['split'] = 'development'
                    case['historically_exposed'] = True
            accepted.append(record)
        manifest = {'contract': contract, 'requested_models': args.models,
                    'source_model_count': len(accepted), 'case_count': len(accepted) * 10,
                    'complete': len(accepted) == args.models,
                    'cases': [c for r in accepted for c in r['cases']], 'exclusions': excluded}
        save(args.output / 'manifest.json', manifest)
        print(f'[{index + 1}/{len(sources)}] {len(accepted)}/{args.models} usable models; '
              f'{len(excluded)} excluded', flush=True)
    if len(accepted) != args.models:
        raise RuntimeError('Insufficient usable distinct sources; manifest is explicitly partial')


if __name__ == '__main__':
    for name in (
        'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
    ):
        os.environ[name] = '1'
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    main()
