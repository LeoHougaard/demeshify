"""Describe retained multiresolution outcomes without changing acceptance gates.

Case rates describe evaluated cases. A source succeeds only when all ten distinct
presets passed; the full source rate is absent until every expected source has
ten results. Reused timings/memory are historical measurements, not cache lookup
costs. RSS is sampled for the controller's owned process tree, not an exact peak
or converter-only measurement. Missing, negative and nonfinite values stay absent.

The CLI only postprocesses saved outcomes. It performs no conversion and does not
certify cache identities. Adding this module to the live runner naturally changes
its full-source fingerprint; old runs remain historical evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean, median

from tools.analyze_surface_failures import classify_failure

PRESETS = {f'q{i:02d}' for i in range(10)}
MEASUREMENTS = (
    'p95_mm', 'maximum_mm', 'local_max_mm', 'volume_error_percent',
    'reference_p95_mm', 'reference_maximum_mm', 'reference_local_max_mm',
    'reference_volume_error_percent', 'reference_dimension_max_error_mm',
)
PERFORMANCE = (
    'conversion_wall_seconds', 'verification_wall_seconds', 'case_wall_seconds',
    'sampled_peak_tree_rss_bytes',
)


def _number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def distribution(rows: list[dict], field: str) -> dict:
    values = sorted(row[field] for row in rows if _number(row.get(field)))
    result = {'available_cases': len(values), 'missing_or_invalid_cases': len(rows) - len(values)}
    if not values:
        return {**result, **dict.fromkeys(('minimum', 'median', 'p95', 'maximum', 'mean'))}
    position = (len(values) - 1) * .95
    low, high = math.floor(position), math.ceil(position)
    return {**result, 'minimum': values[0], 'median': median(values),
            'p95': values[low] + (values[high] - values[low]) * (position - low),
            'maximum': values[-1], 'mean': fmean(values)}


def failure_categories(row: dict) -> list[str]:
    if row.get('accepted'):
        return []
    categories = classify_failure(row)
    status = row.get('status')
    if status == 'crash':
        categories.append('execution_failure')
    if status == 'timeout':
        categories.append('timeout')
    if row.get('reference_geometry_accepted') is False:
        categories.append('reference_geometry_failure')
        if row.get('stl_strict_accepted') is True:
            categories.append('reference_only_failure')
    for field, category in (
        ('reference_solid_count_match', 'reference_solid_count_mismatch'),
        ('reference_shell_count_match', 'reference_shell_count_mismatch'),
    ):
        if row.get(field) is False:
            categories.append(category)
    limit = row.get('reference_limit_mm')
    if _number(limit):
        if any(_number(row.get(field)) and row[field] > limit for field in (
            'reference_maximum_mm', 'reference_local_max_mm',
        )):
            categories.append('reference_surface_deviation')
        dimension = row.get('reference_dimension_max_error_mm')
        if _number(dimension) and dimension > limit:
            categories.append('reference_dimension_deviation')
    volume = row.get('reference_volume_error_percent')
    if _number(volume) and volume > 2:
        categories.append('reference_volume_deviation')
    if len(set(categories)) > 1 and 'acceptance_gate_only' in categories:
        categories.remove('acceptance_gate_only')
    return list(dict.fromkeys(categories))


def _fallbacks(rows: list[dict]) -> dict:
    face_rows = [row for row in rows if _number(row.get('faceted_face_count'))]
    patch_rows = [row for row in rows if _number(row.get('faceted_patch_count'))]
    return {
        'reported_faceted_cases': sum(bool(row.get('faceted_fallback')) or
                                     (_number(row.get('faceted_face_count')) and
                                      row['faceted_face_count'] > 0) or
                                     (_number(row.get('faceted_patch_count')) and
                                      row['faceted_patch_count'] > 0) for row in rows),
        'reported_source_mesh_fallback_cases': sum(bool(row.get('source_mesh_fallback'))
                                                   for row in rows),
        'faceted_face_count': sum(row['faceted_face_count'] for row in face_rows),
        'faceted_face_count_available_cases': len(face_rows),
        'faceted_patch_count': sum(row['faceted_patch_count'] for row in patch_rows),
        'faceted_patch_count_available_cases': len(patch_rows),
    }


def _performance(rows: list[dict]) -> dict:
    groups = {'fresh': [], 'reused': [], 'unmarked': []}
    for row in rows:
        group = ('reused' if row.get('reused') is True else
                 'fresh' if row.get('reused') is False else 'unmarked')
        groups[group].append(row)
    return {name: {'cases': len(group), **{
        field: distribution(group, field) for field in PERFORMANCE
    }} for name, group in groups.items()}


def _cohort(rows: list[dict]) -> dict:
    categories = Counter(category for row in rows for category in failure_categories(row))
    return {
        'cases': len(rows), 'strict_passes': sum(bool(row.get('accepted')) for row in rows),
        'status_counts': dict(sorted(Counter(str(row.get('status', 'unknown'))
                                           for row in rows).items())),
        'crash_cases': sum(row.get('status') in ('crash', 'crashed') for row in rows),
        'timeout_cases': sum('timeout' in failure_categories(row) for row in rows),
        'failure_categories': dict(sorted(categories.items())),
        'fallbacks': _fallbacks(rows),
        'measurements': {field: distribution(rows, field) for field in MEASUREMENTS},
        'performance': _performance(rows),
    }


def aggregate_details(results: list[dict], expected: list[dict]) -> dict:
    """Additional reporting only; never computes or overwrites accepted flags."""
    expected_models = {row['source_model_id'] for row in expected}
    by_model, by_preset = defaultdict(list), defaultdict(list)
    for row in results:
        by_model[row['source_model_id']].append(row)
        by_preset[row['preset']].append(row)
    expected_pairs = {(row['source_model_id'], row['preset']) for row in expected}
    evaluated_pairs = {(row['source_model_id'], row['preset']) for row in results}
    complete = [rows for model, rows in by_model.items() if model in expected_models
                and len(rows) == 10 and {row['preset'] for row in rows} == PRESETS]
    passed = sum(all(row.get('accepted') for row in rows) for rows in complete)
    return {
        'coverage': {
            'expected_cases': len(expected), 'evaluated_cases': len(results),
            'evaluated_case_fraction': len(expected_pairs & evaluated_pairs) / len(expected_pairs)
                if expected_pairs else None,
            'expected_distinct_case_pairs': len(expected_pairs),
            'evaluated_expected_case_pairs': len(expected_pairs & evaluated_pairs),
            'unexpected_case_pairs': len(evaluated_pairs - expected_pairs),
            'duplicate_evaluated_case_pairs': len(results) - len(evaluated_pairs),
            'expected_source_models': len(expected_models),
            'evaluated_source_models': len(by_model),
            'source_models_with_all_ten_evaluated': len(complete),
        },
        'source_model_rates': {
            'required_distinct_presets': 10,
            'all_ten_strict_pass_source_models': passed,
            'all_ten_strict_rate_among_complete_sources': passed / len(complete)
                if complete else None,
            'all_ten_strict_rate_full_expected_sources': passed / len(expected_models)
                if expected_models and len(complete) == len(expected_models)
                and evaluated_pairs == expected_pairs and len(results) == len(expected)
                else None,
        },
        'outcomes': _cohort(results),
        'per_preset_details': {preset: _cohort(rows)
                               for preset, rows in sorted(by_preset.items())},
        'measurement_notes': [
            'No conversion, grading or acceptance gate is changed by this summary.',
            'Fresh/reused flags describe the retained run, never a later postprocessing command.',
            'Reused performance values are historical case measurements, not cache lookup cost.',
            'Unmarked performance rows have no explicit fresh/reused provenance.',
            'RSS is a sampled owned-process-tree measurement, not an exact or converter-only peak.',
            'Failure categories overlap; missing/nonfinite measurements are excluded and counted.',
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Preserve existing reporting evidence; choose a new output path')
    result_bytes, manifest_bytes = args.results.read_bytes(), args.manifest.read_bytes()
    report = aggregate_details(json.loads(result_bytes)['results'],
                               json.loads(manifest_bytes)['cases'])
    report['postprocessing'] = {
        'conversion_performed': False, 'cache_identity_certified': False,
        'results_sha256': hashlib.sha256(result_bytes).hexdigest(),
        'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'summary_tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'failure_classifier_sha256': hashlib.sha256(
            Path(__file__).with_name('analyze_surface_failures.py').read_bytes()).hexdigest(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
