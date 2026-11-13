"""Compare complete matched controller cohorts without rerunning or changing gates."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.frozen_evaluation_controller import (  # noqa: E402
    CACHE_SCHEMA,
    ControllerError,
    _cases,
    _hash,
)
from tools.multires_summary import (  # noqa: E402
    MEASUREMENTS,
    PERFORMANCE,
    PRESETS,
    aggregate_details,
    distribution,
    failure_categories,
)

IDENTITY_FIELDS = (
    'backend', 'tools_sha256', 'grader_manifest_sha256', 'grader_source_revision',
    'converter_runtime_inventory_sha256', 'grader_runtime_inventory_sha256',
    'grader_dependencies_sha256', 'platform', 'process', 'profile', 'deadlines',
    'retain_failure_artifacts', 'verification_policy',
)
CASE_FIELDS = (
    'id', 'source_model_id', 'geometry_family', 'preset', 'split', 'input_sha256',
    'source_sha256', 'input_units', 'tessellation_tolerance_mm', 'angular_tolerance_rad',
    'relative', 'parallel', 'exporter', 'mesh_geometry_sha256',
)
TOPOLOGY = (
    'valid_brep', 'valid_solid', 'closed', 'free_edge_count', 'component_count_match',
    'reference_solid_count_match', 'reference_shell_count_match', 'step_roundtrip_valid',
    'step_roundtrip_solids', 'canonical_vertex_count', 'canonical_edge_count',
    'recognized_surface_count',
)
QUALITY = ('faceted_fallback', 'source_mesh_fallback', 'faceted_face_count',
           'faceted_patch_count', 'clean_analytic', 'minimum_analytic_area_recall',
           'minimum_analytic_area_precision', 'circular_edge_length_recall')
STATUSES = {'complete', 'best_effort', 'crash', 'crashed', 'timeout', 'failed', 'error',
            'fixture_error', 'isolation_error', 'grader_error', 'converter_protocol_error'}


class ComparisonError(ValueError):
    """The records do not establish the same fully scored comparison cohort."""


def _identifier(value: str, kind: str) -> str:
    # Hash human filenames/names; existing content-addressed IDs remain useful.
    return value if re.fullmatch(r'[a-f0-9]{12,64}(?:-q[0-9]{2})?', value) else (
        kind + '-' + hashlib.sha256(value.encode()).hexdigest()[:20])


def _number(value) -> bool:
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _scalar(value):
    return value if type(value) is bool or _number(value) else None


def _context(identity: dict) -> dict:
    if not isinstance(identity, dict) or any(field not in identity for field in IDENTITY_FIELDS):
        raise ComparisonError('Missing frozen evaluation identity fields')
    for field in ('converter_manifest_sha256', 'grader_manifest_sha256',
                  'converter_runtime_inventory_sha256', 'grader_runtime_inventory_sha256',
                  'grader_dependencies_sha256'):
        if not re.fullmatch('[a-f0-9]{64}', str(identity.get(field, ''))):
            raise ComparisonError('Missing or invalid frozen identity hash')
    for field in ('converter_source_revision', 'grader_source_revision'):
        if not re.fullmatch('[a-f0-9]{40}', str(identity.get(field, ''))):
            raise ComparisonError('Missing or invalid exact source revision')
    result = dict(identity)
    result.pop('converter_manifest_sha256')
    result.pop('converter_source_revision')
    policy = result['verification_policy'] = dict(result['verification_policy'])
    catalog = policy.pop('catalog_sha256', None)
    if not re.fullmatch('[a-f0-9]{64}', str(catalog or '')):
        raise ComparisonError('Missing live verification catalog identity')
    if not result['tools_sha256']:
        raise ComparisonError('Missing controller/tool source identities')
    return result


def _validated_rows(run: dict, contract: dict, expected: list[dict], manifest_sha: str) -> dict:
    identity = run.get('identity')
    _context(identity)
    if (contract.get('schema') != 1 or contract.get('identity') != identity
            or contract.get('manifest_sha256') != manifest_sha
            or contract.get('expected_cases') != len(expected)):
        raise ComparisonError('Run contract does not match the pinned manifest and cohort')
    if run.get('halt_reason') is not None:
        raise ComparisonError('A halted controller run is not a qualified matched cohort')
    cases = {case['id']: case for case in expected}
    rows = {}
    for row in run.get('results', []):
        key = row.get('id')
        if key not in cases or key in rows:
            raise ComparisonError('Duplicate or unexpected result case')
        case = cases[key]
        if any(row.get(field) != case[field]
               for field in ('source_model_id', 'preset', 'split')):
            raise ComparisonError('Result source, preset or split differs from its fixture')
        if (row.get('evaluation_fingerprint') != _hash(
                {'schema': CACHE_SCHEMA, 'case': case, 'identity': identity})):
            raise ComparisonError('Result fingerprint differs from pinned case/settings/identity')
        if any(row.get(field) is not True for field in
               ('certified', 'execution_certified', 'solver_outcome_scored')):
            raise ComparisonError('Uncertified or unscored results cannot establish a decision')
        if type(row.get('accepted')) is not bool:
            raise ComparisonError('Strict acceptance must be an explicit original boolean')
        readiness = row.get('fixture_readiness', {})
        if readiness.get('fixture_ready') is not True:
            raise ComparisonError('Unready or unqualified reference fixture')
        if row.get('frozen_converter_manifest_sha256') != identity['converter_manifest_sha256']:
            raise ComparisonError('Result converter identity differs from its run')
        if row.get('frozen_grader_manifest_sha256') != identity['grader_manifest_sha256']:
            raise ComparisonError('Result grader identity differs from its run')
        hashes = row.get('fixture_sha256')
        execution_failure = row.get('failure_kind') in (
            'converter_exit', 'converter_timeout', 'case_timeout')
        if hashes is not None and hashes != {
                'stl': case['input_sha256'], 'source_step': case['source_sha256']}:
            raise ComparisonError('Recorded fixture hashes differ from pinned inputs')
        if hashes is None and not execution_failure:
            raise ComparisonError('Scored geometry lacks original fixture hashes')
        if not execution_failure and any(row.get(field) != identity[identity_field]
                for field, identity_field in (
                    ('grader_manifest_sha256', 'grader_manifest_sha256'),
                    ('grader_source_revision', 'grader_source_revision'),
                    ('grader_runtime_files_sha256', 'grader_runtime_inventory_sha256'))):
            raise ComparisonError('Actual grade identity differs from frozen run identity')
        if row.get('input_units', case['input_units']) != case['input_units']:
            raise ComparisonError('Result units differ from pinned input units')
        provenance = 'historical_verified_case' if row.get('reused') is True else 'fresh'
        if type(row.get('reused')) is not bool or row.get('timing_provenance') != provenance:
            raise ComparisonError('Missing or inconsistent timing provenance')
        rows[key] = row
    if not expected or set(rows) != set(cases):
        raise ComparisonError('Missing expected cases; use a separately frozen complete cohort')
    return rows


def _observations(row: dict) -> dict:
    return {
        'accepted': row['accepted'],
        'status': row.get('status') if row.get('status') in STATUSES else 'other',
        'failure_categories': failure_categories(row),
        'measurements': {field: row.get(field) if _number(row.get(field)) else None
                         for field in MEASUREMENTS},
        'topology': {field: _scalar(row.get(field)) for field in TOPOLOGY},
        'quality': {field: _scalar(row.get(field)) for field in QUALITY},
        'performance': {'provenance': row['timing_provenance'],
                        **{field: row.get(field) if _number(row.get(field)) else None
                           for field in PERFORMANCE}},
    }


def _group(keys: list[str], before: dict, after: dict) -> dict:
    gains = [key for key in keys if not before[key]['accepted'] and after[key]['accepted']]
    losses = [key for key in keys if before[key]['accepted'] and not after[key]['accepted']]
    fresh = [key for key in keys if not before[key]['reused'] and not after[key]['reused']]
    return {
        'cases': len(keys), 'baseline_strict_passes': sum(before[k]['accepted'] for k in keys),
        'candidate_strict_passes': sum(after[k]['accepted'] for k in keys),
        'gains': [_identifier(key, 'case') for key in gains],
        'lost_passes': [_identifier(key, 'case') for key in losses],
        'fresh_performance_pairs': len(fresh),
        'historical_or_mixed_performance_pairs': len(keys) - len(fresh),
        'baseline_outcomes': _outcomes([before[k] for k in keys]),
        'candidate_outcomes': _outcomes([after[k] for k in keys]),
        'baseline_topology': _topology([before[k] for k in keys]),
        'candidate_topology': _topology([after[k] for k in keys]),
        'fresh_matched_performance': {field: {
            'baseline': distribution([before[k] for k in fresh], field),
            'candidate': distribution([after[k] for k in fresh], field),
        } for field in PERFORMANCE},
    }


def _outcomes(rows: list[dict]) -> dict:
    sanitized = [{**row, 'status': row.get('status') if row.get('status') in STATUSES else 'other'}
                 for row in rows]
    return aggregate_details(sanitized, sanitized)['outcomes']


def _topology(rows: list[dict]) -> dict:
    result = {}
    for field in TOPOLOGY:
        values = [row.get(field) for row in rows]
        numbers = [value for value in values if _number(value)]
        booleans = [value for value in values if type(value) is bool]
        result[field] = {
            'true_cases': sum(value is True for value in booleans),
            'false_cases': sum(value is False for value in booleans),
            'numeric_cases': len(numbers), 'minimum': min(numbers) if numbers else None,
            'maximum': max(numbers) if numbers else None,
            'missing_or_invalid_cases': len(rows) - len(numbers) - len(booleans),
        }
    return result


def compare_runs(baseline: dict, candidate: dict, manifest: dict, *,
                 manifest_sha256: str, manifest_parent: Path,
                 baseline_contract: dict, candidate_contract: dict) -> dict:
    """Validate a frozen complete cohort and describe changes, never promote code."""
    context = _context(baseline.get('identity'))
    if context != _context(candidate.get('identity')):
        raise ComparisonError('Grader, tools, runtime, process, profile or deadlines differ')
    split = baseline_contract.get('split')
    if split != candidate_contract.get('split'):
        raise ComparisonError('Run split contracts differ')
    for case in manifest.get('cases', []):
        if any(field not in case for field in CASE_FIELDS):
            raise ComparisonError('Frozen manifest lacks fixture/settings/family identities')
        if (not isinstance(case['geometry_family'], str) or not case['geometry_family']
                or case['preset'] not in PRESETS
                or case['input_units'] not in ('mm', 'cm', 'm', 'in')
                or type(case['relative']) is not bool or type(case['parallel']) is not bool
                or not case['exporter']
                or any(not _number(case[field]) or case[field] <= 0 for field in (
                    'tessellation_tolerance_mm', 'angular_tolerance_rad'))
                or any(not re.fullmatch('[a-f0-9]{64}', str(case[field])) for field in (
                    'input_sha256', 'source_sha256', 'mesh_geometry_sha256'))):
            raise ComparisonError('Invalid frozen fixture settings or content identities')
    try:
        expected = _cases(manifest, manifest_parent, split)
    except (ControllerError, ValueError, KeyError, TypeError) as exc:
        raise ComparisonError('Invalid or duplicate frozen fixture identities') from exc
    before = _validated_rows(baseline, baseline_contract, expected, manifest_sha256)
    after = _validated_rows(candidate, candidate_contract, expected, manifest_sha256)
    keys = [case['id'] for case in expected]
    models, presets = defaultdict(list), defaultdict(list)
    cases = []
    for case in expected:
        key = case['id']
        models[case['source_model_id']].append(key)
        presets[case['preset']].append(key)
        cases.append({
            'id': _identifier(key, 'case'),
            'source_model_id': _identifier(case['source_model_id'], 'source'),
            'geometry_family': _identifier(case['geometry_family'], 'family'),
            'preset': case['preset'], 'split': case['split'],
            'input_sha256': case['input_sha256'], 'source_sha256': case['source_sha256'],
            'baseline': _observations(before[key]), 'candidate': _observations(after[key]),
        })
    # Existing reporting owns failure categories, distributions and ten-preset
    # source rates. Paths, free-text errors and human IDs never enter the report.
    def details(rows):
        sanitized = [{**row, 'source_model_id': _identifier(row['source_model_id'], 'source'),
                      'status': row.get('status') if row.get('status') in STATUSES else 'other'}
                     for row in rows]
        fixtures = [{**case, 'source_model_id': _identifier(case['source_model_id'], 'source')}
                    for case in expected]
        return aggregate_details(sanitized, fixtures)

    by_model = {}
    for model, ids in sorted(models.items()):
        observed = {before[key]['preset'] for key in ids}
        complete = observed == PRESETS and len(ids) == 10
        by_model[_identifier(model, 'source')] = {
            **_group(ids, before, after), 'evaluated_presets': sorted(observed),
            'all_ten_evaluated': complete,
            'baseline_all_ten_strict': (
                all(before[k]['accepted'] for k in ids) if complete else None),
            'candidate_all_ten_strict': (
                all(after[k]['accepted'] for k in ids) if complete else None),
        }
    return {
        'schema': 1, 'comparison_valid': True, 'promotion_decision': 'not_made',
        'common_context_sha256': _hash(context), 'manifest_sha256': manifest_sha256,
        'coverage_scope': {
            'matched_cohort_complete': True, 'expected_cases': len(expected),
            'expected_source_models': len(models),
            'manifest_declares_complete': manifest.get('complete') is True,
            'full_1000x10_matched': (
                manifest.get('complete') is True and len(expected) == 10000
                and len(models) == 1000
                and all(group['all_ten_evaluated'] for group in by_model.values())),
        },
        'baseline_converter': {key: baseline['identity'][key] for key in
                               ('converter_source_revision', 'converter_manifest_sha256')},
        'candidate_converter': {key: candidate['identity'][key] for key in
                                ('converter_source_revision', 'converter_manifest_sha256')},
        'grader_manifest_sha256': context['grader_manifest_sha256'],
        'summary': _group(keys, before, after), 'per_source_model': by_model,
        'per_preset': {preset: _group(ids, before, after)
                       for preset, ids in sorted(presets.items())},
        'baseline_details': details(list(before.values())),
        'candidate_details': details(list(after.values())), 'cases': cases,
        'notes': [
            'Original accepted booleans are compared; no acceptance gate or promotion is invented.',
            'A completed small cohort is not a full-corpus baseline or ten independent designs.',
            'A source-model all-ten outcome remains absent until ten distinct presets are scored.',
            'Only fresh/fresh pairs enter matched performance; reused timings remain history.',
            'RSS is sampled owned-process-tree memory, not an exact or converter-only peak.',
            'Paths/errors are omitted; human identifiers are replaced with stable hashes.',
            'Saved attestations are compared; native isolation is not revalidated.',
        ],
    }


def _read(path: Path) -> tuple[dict, str]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ComparisonError('Duplicate JSON object key in saved evidence')
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ComparisonError('Nonfinite JSON value in saved evidence')

    raw = path.read_bytes()
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=invalid_constant), hashlib.sha256(raw).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--baseline-contract', type=Path)
    parser.add_argument('--candidate-contract', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ComparisonError('Choose a new report path; preserve existing comparison evidence')
    baseline, baseline_sha = _read(args.baseline)
    candidate, candidate_sha = _read(args.candidate)
    manifest, manifest_sha = _read(args.manifest)
    first, first_sha = _read(args.baseline_contract or args.baseline.with_suffix('.contract.json'))
    second, second_sha = _read(
        args.candidate_contract or args.candidate.with_suffix('.contract.json'))
    report = compare_runs(baseline, candidate, manifest, manifest_sha256=manifest_sha,
                          manifest_parent=args.manifest.parent.resolve(),
                          baseline_contract=first, candidate_contract=second)
    report['postprocessing'] = {
        'baseline_results_sha256': baseline_sha, 'candidate_results_sha256': candidate_sha,
        'baseline_contract_sha256': first_sha, 'candidate_contract_sha256': second_sha,
        'conversion_performed': False, 'cache_identity_certified': False,
        'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'dependencies_sha256': {name: hashlib.sha256(
            Path(__file__).with_name(name).read_bytes()).hexdigest() for name in (
                'frozen_evaluation_controller.py', 'multires_summary.py',
                'analyze_surface_failures.py')},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
