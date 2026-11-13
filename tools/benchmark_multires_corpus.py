"""Grade multiresolution fixtures with a reference-blind converter subprocess."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.jobs import reconstruct_in_worker, run_bounded_process  # noqa: E402
from app.storage import atomic_write_text  # noqa: E402
from tools import benchmark_surface_corpus as benchmark  # noqa: E402
from tools.evaluation_identity import (  # noqa: E402
    cached_result,
    case_identity,
    digest,
    runtime_identity,
    source_identity,
)
from tools.process_resources import sampled_resources  # noqa: E402


def evaluate(case: dict, manifest: Path, artifacts: Path | None) -> dict:
    import numpy as np

    from app.mesh import surface_samples
    from app.scoring import _local_samples, _point_mesh_distances, _step_mesh
    from app.verification import acceptance_threshold

    original_metrics = benchmark.step_surface_metrics
    original_convert = benchmark.reconstruct_surfaces
    reference = {}
    output_shape = None

    def convert(run_id, stl_path, original_name, input_units):
        # No manifest, STEP path, source name or reference object goes to the
        # converter. Windows spawn does not inherit the grader's Python heap.
        return reconstruct_in_worker(run_id, stl_path, 'input.stl', input_units,
                                     engine='surface_brep')

    def metrics(path):
        nonlocal output_shape
        result = original_metrics(path)
        if output_shape is None:
            output_shape = result['shape']
        else:
            source_shape = result['shape']
            bounds = source_shape.BoundingBox()
            diagonal = (bounds.xlen**2 + bounds.ylen**2 + bounds.zlen**2)**0.5
            deflection = max(min(diagonal * 0.00001, 0.002), 1e-7)
            source_mesh = _step_mesh(source_shape, deflection)
            output_mesh = _step_mesh(output_shape, deflection)
            distances = []
            local = []
            for target, probes in ((source_mesh, output_mesh), (output_mesh, source_mesh)):
                distances.extend(_point_mesh_distances(target, surface_samples(probes)))
                local.extend(_point_mesh_distances(target, _local_samples(probes)))
            volume_error = abs(output_shape.Volume() - source_shape.Volume()) / abs(
                source_shape.Volume()) * 100
            output_bounds = output_shape.BoundingBox()
            dimension_error = max(abs(a - b) for a, b in zip(
                (bounds.xlen, bounds.ylen, bounds.zlen),
                (output_bounds.xlen, output_bounds.ylen, output_bounds.zlen), strict=True))
            solid_match = len(source_shape.Solids()) == len(output_shape.Solids())
            shell_match = len(source_shape.Shells()) == len(output_shape.Shells())
            limit = acceptance_threshold(diagonal)
            reference.update(
                reference_p95_mm=float(np.percentile(distances, 95)),
                reference_maximum_mm=float(np.max(distances)),
                reference_local_max_mm=float(np.max(local)),
                reference_volume_error_percent=volume_error,
                reference_dimension_max_error_mm=dimension_error,
                reference_solid_count_match=solid_match, reference_shell_count_match=shell_match,
                reference_mesh_deflection_mm=deflection, reference_limit_mm=limit,
                reference_geometry_accepted=bool(
                    solid_match and shell_match and volume_error <= 2
                    and max(distances) <= limit and max(local) <= limit
                    and dimension_error <= limit),
            )
        return result

    benchmark.reconstruct_surfaces = convert
    benchmark.step_surface_metrics = metrics
    try:
        result = benchmark.benchmark_case(case, manifest.parent, artifacts, 240)
    finally:
        benchmark.reconstruct_surfaces = original_convert
        benchmark.step_surface_metrics = original_metrics
    result.update(reference)
    result['stl_strict_accepted'] = result['accepted']
    result['accepted'] = bool(result['accepted'] and reference.get('reference_geometry_accepted'))
    result.update({k: case[k] for k in ('source_model_id', 'split', 'preset')})
    return result


def summary(results: list[dict], expected: list[dict]) -> dict:
    by_model, by_preset, by_split = defaultdict(list), defaultdict(list), defaultdict(list)
    for result in results:
        by_model[result['source_model_id']].append(result)
        by_preset[result['preset']].append(result)
        by_split[result['split']].append(result)
    def counts(rows):
        return {'cases': len(rows), 'strict_passes': sum(bool(r.get('accepted')) for r in rows)}
    return {
        'expected_cases': len(expected), 'evaluated_cases': len(results),
        'complete': len(results) == len(expected),
        'fresh_cases': sum(not r.get('reused', False) for r in results),
        'reused_cases': sum(bool(r.get('reused')) for r in results),
        'strict_case_passes': sum(bool(r.get('accepted')) for r in results),
        'strict_case_rate': (sum(bool(r.get('accepted')) for r in results) / len(results)
                             if results else None),
        'expected_source_models': len({c['source_model_id'] for c in expected}),
        'source_models_all_ten_pass': sum(len(rows) == 10 and all(
            r.get('accepted') for r in rows) for rows in by_model.values()),
        'source_models_ten_evaluated': sum(len(rows) == 10 for rows in by_model.values()),
        'per_preset': {k: counts(v) for k, v in sorted(by_preset.items())},
        'per_split': {k: counts(v) for k, v in sorted(by_split.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--split', choices=['development', 'validation', 'holdout'])
    parser.add_argument('--worker-id', help=argparse.SUPPRESS)
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--case-cache', type=Path,
                        help='Shared immutable case results (default: beside output)')
    parser.add_argument('--fresh', action='store_true',
                        help='Skip shared results; still resume completed cases in this run')
    parser.add_argument('--batch-cases', type=int,
                        help='Stop after this many new case evaluations; cached cases do not count')
    args = parser.parse_args()
    if args.batch_cases is not None and args.batch_cases <= 0:
        parser.error('--batch-cases must be positive')
    manifest = json.loads(args.manifest.read_text())
    cases = [c for c in manifest['cases'] if not args.split or c['split'] == args.split]
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Duplicate case identities in manifest')
    if args.worker_id:
        case = next(c for c in cases if c['id'] == args.worker_id)
        try:
            result = evaluate(case, args.manifest, args.artifacts)
        except Exception as exc:
            result = {'id': case['id'], 'accepted': False, 'status': 'error',
                      'error': f'{type(exc).__name__}: {exc}'}
        args.output.write_text(json.dumps(result, indent=2))
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cache = args.output.parent / f'{args.output.stem}_cases'
    cache.mkdir(exist_ok=True)
    source_hashes = source_identity(ROOT)
    runtime = runtime_identity()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    shared_cache = args.case_cache or args.output.parent / 'multires-case-cache'
    shared_cache.mkdir(parents=True, exist_ok=True)
    run_contract = {'schema': 2, 'source_sha256': source_hashes, 'runtime': runtime,
                    'manifest_sha256': digest(args.manifest), 'fresh': args.fresh,
                    'split': args.split, 'conversion_timeout': 240, 'case_timeout': 360,
                    'case_cache': str(shared_cache.resolve())}
    contract_path = args.output.with_suffix('.contract.json')
    if contract_path.exists() and json.loads(contract_path.read_text()) != run_contract:
        raise ValueError('Evaluation contract changed; preserve evidence and use a new output path')
    atomic_write_text(contract_path, json.dumps(run_contract, indent=2))
    # Commits containing only documentation/acquisition work must not prevent
    # resuming a frozen evaluation. Retain each invocation's actual revision.
    launches_path = args.output.with_suffix('.launches.json')
    launches = json.loads(launches_path.read_text()) if launches_path.exists() else []
    launches.append({'revision': revision, 'started_unix_seconds': time.time(),
                     'batch_cases': args.batch_cases})
    atomic_write_text(launches_path, json.dumps(launches, indent=2))
    results = []
    fresh_completed = 0
    for case in cases:
        if shutil.disk_usage(args.output.parent).free < 15 * 1024**3:
            raise RuntimeError('Storage floor reached; results preserved')
        for key, expected in (('stl', 'input_sha256'), ('source_step', 'source_sha256')):
            if digest(Path(case[key])) != case[expected]:
                raise ValueError(f'Frozen geometry changed for {case["id"]}')
        fingerprint = case_identity(case, source_hashes, runtime)
        path = cache / f'{case["id"]}.json'
        shared_path = shared_cache / f'{fingerprint}.json'
        result = cached_result(path, fingerprint)
        reused_from = 'this_run' if result is not None else None
        if result is None and not args.fresh:
            result = cached_result(shared_path, fingerprint)
            if result is not None:
                reused_from = 'shared_cache'
                atomic_write_text(path, json.dumps(result, indent=2))
        reused = result is not None
        if not reused:
            started = time.perf_counter()
            attempt = cache / f'{case["id"]}.{uuid.uuid4().hex}.worker.json'
            command = [sys.executable, str(Path(__file__).resolve()), str(args.manifest.resolve()),
                       '--output', str(attempt.resolve()), '--worker-id', case['id']]
            if args.artifacts:
                command += ['--artifacts', str(args.artifacts.resolve())]
            resources = {}
            try:
                with sampled_resources() as resources:
                    code = run_bounded_process(
                        command, cwd=ROOT, timeout=360,
                        stdout_path=attempt.with_suffix('.stdout.log'),
                        stderr_path=attempt.with_suffix('.stderr.log'))
                if code:
                    raise RuntimeError(f'Grader exited {code}')
                result = json.loads(attempt.read_text())
            except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError, OSError) as exc:
                result = {'id': case['id'], 'accepted': False,
                          'status': 'timeout' if isinstance(exc, subprocess.TimeoutExpired)
                          else 'crash', 'error': str(exc)}
            # A child output is not a certified cache record. Check the frozen
            # code/runtime/inputs before publishing it; interrupted attempts and
            # their diagnostics remain separate and never block a clean resume.
            if source_identity(ROOT) != source_hashes or runtime_identity() != runtime:
                raise RuntimeError('Code or runtime changed during evaluation; attempt preserved')
            if any(digest(Path(case[key])) != case[expected] for key, expected in (
                ('stl', 'input_sha256'), ('source_step', 'source_sha256'),
            )):
                raise RuntimeError('Frozen geometry changed during evaluation; attempt preserved')
            result.update(case_wall_seconds=time.perf_counter() - started,
                          evaluation_fingerprint=fingerprint, conversion_revision=revision,
                          worker_attempt=str(attempt.resolve()))
            result.update(resources)
            result.update({k: case[k] for k in ('source_model_id', 'split', 'preset')})
            if args.artifacts and (args.artifacts / case['id']).is_dir():
                result['artifact_location'] = str((args.artifacts / case['id']).resolve())
            atomic_write_text(path, json.dumps(result, indent=2))
            if not shared_path.exists():
                atomic_write_text(shared_path, json.dumps(result, indent=2))
            fresh_completed += 1
        results.append({**result, 'reused': reused, 'reused_from': reused_from})
        if source_identity(ROOT) != source_hashes:
            raise RuntimeError('Source changed during evaluation; do not combine results')
        aggregate = summary(results, cases)
        batch_complete = bool(args.batch_cases and fresh_completed >= args.batch_cases)
        atomic_write_text(args.output.with_suffix('.summary.json'), json.dumps(aggregate, indent=2))
        # Individual cache records are durable after each case. Avoid rewriting
        # tens of megabytes 10,000 times; complete snapshots every 100 cases.
        if (len(results) == 1 or len(results) % 100 == 0 or len(results) == len(cases)
                or batch_complete):
            atomic_write_text(args.output, json.dumps({
                'summary': aggregate, 'revision': revision, 'results': results}, indent=2))
        print(f'[{len(results)}/{len(cases)}] {case["id"]}: '
              f'{result["status"]}, strict={result["accepted"]}, reused={reused}', flush=True)
        if batch_complete:
            print(f'Batch complete: {fresh_completed} new case evaluations; '
                  f'full run complete={aggregate["complete"]}', flush=True)
            break


if __name__ == '__main__':
    for name in (
        'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
    ):
        os.environ[name] = '1'
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    main()
