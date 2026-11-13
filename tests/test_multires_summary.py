import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools.multires_summary import aggregate_details, distribution, failure_categories, main


def source(model, accepted=True, **values):
    return [{'id': f'{model}-q{i:02d}', 'source_model_id': model, 'preset': f'q{i:02d}',
             'split': 'development', 'accepted': accepted, 'status': 'complete', **values}
            for i in range(10)]


def test_partial_and_complete_source_denominators_do_not_hide_unevaluated_models():
    expected = source('a') + source('b')
    partial = aggregate_details(expected[:10], expected)
    assert partial['coverage']['evaluated_case_fraction'] == .5
    assert partial['source_model_rates']['all_ten_strict_rate_among_complete_sources'] == 1
    assert partial['source_model_rates']['all_ten_strict_rate_full_expected_sources'] is None
    results = copy.deepcopy(expected)
    results[-1]['accepted'] = False
    report = aggregate_details(results, expected)
    assert report['source_model_rates']['all_ten_strict_pass_source_models'] == 1
    assert report['source_model_rates']['all_ten_strict_rate_full_expected_sources'] == .5
    assert report['source_model_rates']['all_ten_strict_rate_among_complete_sources'] == .5
    assert report['outcomes']['strict_passes'] == 19


def test_repeated_or_unexpected_variants_never_establish_a_complete_source_rate():
    expected = source('a')
    repeated = expected[:9] + [expected[0]]
    report = aggregate_details(repeated, expected)
    assert report['coverage']['evaluated_case_fraction'] == .9
    assert report['coverage']['duplicate_evaluated_case_pairs'] == 1
    assert report['coverage']['evaluated_expected_case_pairs'] == 9
    assert report['source_model_rates']['all_ten_strict_pass_source_models'] == 0
    assert report['source_model_rates']['all_ten_strict_rate_full_expected_sources'] is None
    report = aggregate_details(source('wrong'), expected)
    assert report['coverage']['evaluated_case_fraction'] == 0
    assert report['coverage']['unexpected_case_pairs'] == 10
    assert report['source_model_rates']['all_ten_strict_rate_full_expected_sources'] is None


def test_reference_only_failure_and_literal_crash_status_have_actionable_categories():
    failure = {'accepted': False, 'status': 'complete', 'stl_strict_accepted': True,
               'reference_geometry_accepted': False, 'reference_limit_mm': .12,
               'reference_dimension_max_error_mm': .3, 'reference_maximum_mm': .4,
               'reference_volume_error_percent': 3, 'reference_solid_count_match': False,
               'reference_shell_count_match': False}
    assert set(failure_categories(failure)) == {
        'reference_geometry_failure', 'reference_only_failure', 'reference_surface_deviation',
        'reference_dimension_deviation', 'reference_volume_deviation',
        'reference_solid_count_mismatch', 'reference_shell_count_mismatch',
    }
    assert failure_categories({'accepted': False, 'status': 'crash'}) == ['execution_failure']
    assert 'timeout' in failure_categories({'accepted': False, 'status': 'timeout'})
    assert failure_categories({'accepted': True}) == []


def test_missing_nonfinite_negative_and_boolean_measurements_are_counted_as_unavailable():
    rows = [{'x': value} for value in (1, 3, 5, None, float('nan'), float('inf'), -1, True)]
    report = distribution(rows, 'x')
    assert report['available_cases'] == 3 and report['missing_or_invalid_cases'] == 5
    assert report['minimum'] == 1 and report['maximum'] == 5
    assert report['median'] == report['mean'] == 3
    assert report['p95'] == pytest.approx(4.8)
    assert distribution([], 'x') == {'available_cases': 0, 'missing_or_invalid_cases': 0,
                                      'minimum': None, 'median': None, 'p95': None,
                                      'maximum': None, 'mean': None}


def test_fresh_and_historical_reused_performance_and_facet_availability_are_separate():
    rows = source('a')[:3]
    rows[0].update(reused=False, conversion_wall_seconds=10, sampled_peak_tree_rss_bytes=100,
                   faceted_face_count=132, faceted_patch_count=1, faceted_fallback=False)
    rows[1].update(reused=True, conversion_wall_seconds=100, sampled_peak_tree_rss_bytes=900,
                   source_mesh_fallback=True, faceted_face_count=0, faceted_patch_count=0)
    rows[2].update(conversion_wall_seconds=50)
    before = copy.deepcopy(rows)
    report = aggregate_details(rows, rows)
    performance = report['outcomes']['performance']
    assert performance['fresh']['conversion_wall_seconds']['mean'] == 10
    assert performance['reused']['conversion_wall_seconds']['mean'] == 100
    assert performance['unmarked']['conversion_wall_seconds']['mean'] == 50
    assert performance['unmarked']['sampled_peak_tree_rss_bytes']['available_cases'] == 0
    fallback = report['outcomes']['fallbacks']
    assert fallback['reported_faceted_cases'] == 1
    assert fallback['faceted_face_count'] == 132
    assert fallback['faceted_face_count_available_cases'] == 2
    assert fallback['reported_source_mesh_fallback_cases'] == 1
    assert report['per_preset_details']['q00']['cases'] == 1
    assert rows == before  # Reporting cannot rewrite acceptance or measurements.


def test_live_summary_preserves_original_counts_and_adds_honest_coverage():
    from tools.benchmark_multires_corpus import summary

    expected = source('a')
    rows = expected[:2]
    rows[1] = {**rows[1], 'accepted': False, 'status': 'crash', 'reused': True}
    report = summary(rows, expected)
    assert report['expected_cases'] == 10 and report['evaluated_cases'] == 2
    assert not report['complete'] and report['strict_case_rate'] == .5
    assert report['per_preset']['q01'] == {'cases': 1, 'strict_passes': 0}
    assert report['source_models_all_ten_pass'] == report['source_models_ten_evaluated'] == 0
    assert report['outcomes']['crash_cases'] == 1
    assert report['source_model_rates']['all_ten_strict_rate_full_expected_sources'] is None


def test_saved_outcome_postprocessing_records_provenance_and_preserves_existing_output(
    tmp_path, monkeypatch,
):
    rows = source('a')[:1]
    results, manifest, output = (tmp_path / name for name in (
        'old.json', 'manifest.json', 'new.json',
    ))
    results.write_text(json.dumps({'results': rows}))
    manifest.write_text(json.dumps({'cases': source('a')}))
    monkeypatch.setattr('sys.argv', ['summary', str(results), '--manifest', str(manifest),
                                    '--output', str(output)])
    main()
    report = json.loads(output.read_text())
    assert report['postprocessing']['conversion_performed'] is False
    assert report['postprocessing']['cache_identity_certified'] is False
    assert len(report['postprocessing']['results_sha256']) == 64
    assert len(report['postprocessing']['failure_classifier_sha256']) == 64
    assert report['coverage']['evaluated_case_fraction'] == .1
    original = output.read_bytes()
    with pytest.raises(ValueError, match='Preserve existing reporting'):
        main()
    assert output.read_bytes() == original


def test_controller_summarizes_snapshot_boundaries_and_resumes_durable_case_records(
    tmp_path, monkeypatch,
):
    from tools import benchmark_multires_corpus as runner
    from tools.evaluation_identity import digest

    # Controller protocol only: fake worker bytes are never counted as CAD data.
    stl, step = tmp_path / 'input.stl', tmp_path / 'truth.step'
    stl.write_bytes(b'protocol STL')
    step.write_bytes(b'protocol STEP')
    cases = [{**source(f'model{i // 10}')[i % 10], 'stl': str(stl),
              'source_step': str(step), 'input_sha256': digest(stl),
              'source_sha256': digest(step)} for i in range(205)]
    manifest, output = tmp_path / 'manifest.json', tmp_path / 'run.json'
    manifest.write_text(json.dumps({'cases': cases}))
    monkeypatch.setattr(runner, 'source_identity', lambda _: {'fit': 'stable'})
    monkeypatch.setattr(runner, 'runtime_identity', lambda: {})
    monkeypatch.setattr(runner.subprocess, 'check_output', lambda *_, **__: 'head')

    @contextmanager
    def resources():
        yield {}

    calls, snapshots = [], []
    original_summary = runner.summary

    def summarize(rows, expected):
        snapshots.append(len(rows))
        return original_summary(rows, expected)

    def worker(command, **_kwargs):
        identifier = command[command.index('--worker-id') + 1]
        calls.append(identifier)
        Path(command[command.index('--output') + 1]).write_text(json.dumps({
            'id': identifier, 'accepted': True, 'status': 'complete',
        }))
        return 0

    monkeypatch.setattr(runner, 'summary', summarize)
    monkeypatch.setattr(runner, 'run_bounded_process', worker)
    monkeypatch.setattr(runner, 'sampled_resources', resources)

    def run(limit):
        monkeypatch.setattr('sys.argv', ['runner', str(manifest), '--output', str(output),
                                        '--batch-cases', str(limit)])
        runner.main()
        return json.loads(output.read_text())

    first = run(150)
    assert snapshots == [1, 100, 150]
    assert len(calls) == 150 and not first['summary']['complete']
    assert len(list((tmp_path / 'run_cases').glob('model*-q??.json'))) == 150
    snapshots.clear()
    final = run(60)
    assert snapshots == [1, 100, 200, 205]
    assert len(calls) == 205 and final['summary']['complete']
    assert final['summary']['fresh_cases'] == 55 and final['summary']['reused_cases'] == 150
    assert final['summary']['coverage']['evaluated_cases'] == 205
    assert json.loads(output.with_suffix('.summary.json').read_text()) == final['summary']
