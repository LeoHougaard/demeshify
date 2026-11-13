import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import compare_multires_runs as compare


def fixture(tmp_path, *, ten=False):
    source_ids = ['a' * 20] * 10 if ten else ['a' * 20, 'b' * 20]
    cases = []
    for index, source in enumerate(source_ids):
        preset = f'q{index:02d}' if ten else 'q00'
        cases.append({
            'id': source + '-' + preset, 'source_model_id': source,
            'geometry_family': source, 'split': 'development', 'preset': preset,
            'stl': f'{source}/{preset}.stl', 'source_step': source + '.step',
            'input_sha256': hashlib.sha256(f'input{index}'.encode()).hexdigest(),
            'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'mesh_geometry_sha256': hashlib.sha256(f'mesh{index}'.encode()).hexdigest(),
            'input_units': 'mm', 'tessellation_tolerance_mm': .05,
            'angular_tolerance_rad': .22, 'relative': False, 'parallel': False,
            'exporter': {'cadquery': '2.8.0'},
        })
    manifest = {'cases': cases, 'complete': ten}
    identity = {
        'backend': 'isolated-converter-frozen-grader-controller-v1',
        'converter_manifest_sha256': '1' * 64, 'converter_source_revision': '3' * 40,
        'grader_manifest_sha256': '5' * 64, 'grader_source_revision': '6' * 40,
        'converter_runtime_inventory_sha256': '7' * 64,
        'grader_runtime_inventory_sha256': '7' * 64,
        'grader_dependencies_sha256': '8' * 64,
        'tools_sha256': {'tools/frozen_evaluation_controller.py': '9' * 64},
        'platform': {'machine': 'AMD64'}, 'process': {'cpu_count': 1},
        'profile': 'private-profile-name', 'deadlines': {'whole_case': 360},
        'retain_failure_artifacts': True,
        'verification_policy': {'kind': 'live-locked', 'catalog_sha256': 'a' * 64},
    }
    resolved = compare._cases(manifest, tmp_path, 'development')

    def run(candidate):
        context = copy.deepcopy(identity)
        if candidate:
            context.update(converter_manifest_sha256='2' * 64,
                           converter_source_revision='4' * 40)
            context['verification_policy']['catalog_sha256'] = 'b' * 64
        rows = []
        for index, case in enumerate(resolved):
            rows.append({
                **{field: case[field] for field in ('id', 'source_model_id', 'preset', 'split')},
                'accepted': candidate or index > 0, 'certified': True,
                'execution_certified': True, 'solver_outcome_scored': True,
                'fixture_readiness': {'fixture_ready': True},
                'fixture_sha256': {'stl': case['input_sha256'],
                                   'source_step': case['source_sha256']},
                'frozen_converter_manifest_sha256': context['converter_manifest_sha256'],
                'frozen_grader_manifest_sha256': context['grader_manifest_sha256'],
                'grader_manifest_sha256': context['grader_manifest_sha256'],
                'grader_source_revision': context['grader_source_revision'],
                'grader_runtime_files_sha256': context['grader_runtime_inventory_sha256'],
                'input_units': 'mm', 'reused': False, 'timing_provenance': 'fresh',
                'status': 'complete', 'conversion_wall_seconds': 2. if candidate else 4.,
                'sampled_peak_tree_rss_bytes': 200 if candidate else 300,
                'reference_dimension_max_error_mm': .02, 'valid_solid': True,
                'faceted_face_count': 0, 'faceted_patch_count': 0,
                'evaluation_fingerprint': compare._hash(
                    {'schema': compare.CACHE_SCHEMA, 'case': case, 'identity': context}),
            })
        return {'identity': context, 'results': rows, 'halt_reason': None}

    before, after = run(False), run(True)

    def contract(document):
        return {'schema': 1, 'identity': copy.deepcopy(document['identity']),
                'manifest_sha256': 'f' * 64, 'expected_cases': len(cases),
                'split': 'development'}

    arguments = {'manifest_sha256': 'f' * 64, 'manifest_parent': tmp_path,
                 'baseline_contract': contract(before), 'candidate_contract': contract(after)}
    return before, after, manifest, arguments


def test_gain_is_evidence_without_automatic_promotion_or_full_source_claim(tmp_path):
    before, after, manifest, options = fixture(tmp_path)
    report = compare.compare_runs(before, after, manifest, **options)
    assert report['summary']['baseline_strict_passes'] == 1
    assert report['summary']['candidate_strict_passes'] == 2
    assert report['summary']['gains'] == ['a' * 20 + '-q00']
    assert report['summary']['lost_passes'] == []
    assert report['promotion_decision'] == 'not_made'
    assert all(row['candidate_all_ten_strict'] is None
               for row in report['per_source_model'].values())
    assert report['baseline_details']['source_model_rates'][
        'all_ten_strict_rate_full_expected_sources'] is None
    assert report['cases'][0]['candidate']['measurements'][
        'reference_dimension_max_error_mm'] == .02
    assert report['summary']['candidate_topology']['valid_solid']['true_cases'] == 2


def test_lost_success_control_is_reported_even_when_another_case_recovers(tmp_path):
    before, after, manifest, options = fixture(tmp_path)
    after['results'][1]['accepted'] = False
    report = compare.compare_runs(before, after, manifest, **options)
    assert report['summary']['gains'] == ['a' * 20 + '-q00']
    assert report['summary']['lost_passes'] == ['b' * 20 + '-q00']


@pytest.mark.parametrize('field,value', [
    ('grader_manifest_sha256', 'c' * 64), ('tools_sha256', {'changed.py': 'd' * 64}),
    ('grader_runtime_inventory_sha256', 'd' * 64), ('process', {'cpu_count': 2}),
    ('profile', 'different-profile'), ('deadlines', {'whole_case': 400}),
])
def test_mismatched_evaluation_contract_is_refused(tmp_path, field, value):
    before, after, manifest, options = fixture(tmp_path)
    after['identity'][field] = value
    options['candidate_contract']['identity'] = copy.deepcopy(after['identity'])
    with pytest.raises(compare.ComparisonError, match='differ'):
        compare.compare_runs(before, after, manifest, **options)


@pytest.mark.parametrize('problem', ['missing', 'duplicate', 'unscored', 'input',
                                   'settings', 'grade_identity', 'family_split'])
def test_incomplete_or_foreign_case_evidence_is_refused(tmp_path, problem):
    before, after, manifest, options = fixture(tmp_path)
    if problem == 'missing':
        after['results'].pop()
    elif problem == 'duplicate':
        after['results'].append(copy.deepcopy(after['results'][0]))
    elif problem == 'unscored':
        after['results'][0]['solver_outcome_scored'] = False
    elif problem == 'input':
        after['results'][0]['fixture_sha256']['stl'] = '0' * 64
    elif problem == 'settings':
        manifest['cases'][0]['angular_tolerance_rad'] = .5
    elif problem == 'grade_identity':
        after['results'][0]['grader_manifest_sha256'] = '0' * 64
    else:
        manifest['cases'][1]['geometry_family'] = manifest['cases'][0]['geometry_family']
        manifest['cases'][1]['split'] = 'holdout'
    with pytest.raises(compare.ComparisonError):
        compare.compare_runs(before, after, manifest, **options)


def test_cached_measurement_is_history_and_excluded_from_fresh_matched_performance(tmp_path):
    before, after, manifest, options = fixture(tmp_path)
    after['results'][0].update(reused=True, timing_provenance='historical_verified_case',
                               conversion_wall_seconds=1000.)
    report = compare.compare_runs(before, after, manifest, **options)
    assert report['summary']['fresh_performance_pairs'] == 1
    assert report['summary']['historical_or_mixed_performance_pairs'] == 1
    performance = report['summary']['fresh_matched_performance']['conversion_wall_seconds']
    assert performance['candidate']['mean'] == 2.
    historical = report['candidate_details']['outcomes']['performance']['reused']
    assert historical['conversion_wall_seconds']['mean'] == 1000.


def test_only_ten_distinct_scored_presets_establish_a_source_outcome(tmp_path):
    before, after, manifest, options = fixture(tmp_path, ten=True)
    report = compare.compare_runs(before, after, manifest, **options)
    source = report['per_source_model']['a' * 20]
    assert source['all_ten_evaluated'] and source['candidate_all_ten_strict'] is True
    assert source['baseline_all_ten_strict'] is False


def test_cli_preserves_inputs_and_reports_without_private_paths(tmp_path):
    before, after, manifest, options = fixture(tmp_path)
    for row in before['results'] + after['results']:
        row.update(error='C:/Users/Leo/private-fixture/location',
                   attempt='C:/Users/Leo/private-fixture/attempt')
    paths = {}
    paths['manifest'] = tmp_path / 'manifest.json'
    paths['manifest'].write_text(json.dumps(manifest))
    sha = hashlib.sha256(paths['manifest'].read_bytes()).hexdigest()
    for name, data in (('baseline', before), ('candidate', after)):
        path = paths[name] = tmp_path / (name + '.json')
        path.write_text(json.dumps(data))
        contract = copy.deepcopy(options[name + '_contract'])
        contract['manifest_sha256'] = sha
        path.with_suffix('.contract.json').write_text(json.dumps(contract))
    original = {name: path.read_bytes() for name, path in paths.items()}
    output = tmp_path / 'comparison.json'
    command = [sys.executable, str(Path(compare.__file__)), str(paths['baseline']),
               str(paths['candidate']), '--manifest', str(paths['manifest']),
               '--output', str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = output.read_text()
    assert 'C:/Users/Leo' not in report and 'private-profile-name' not in report
    assert json.loads(report)['postprocessing']['conversion_performed'] is False
    assert original == {name: path.read_bytes() for name, path in paths.items()}
    repeated = subprocess.run(command, capture_output=True, text=True, check=False)
    assert repeated.returncode != 0 and output.read_text() == report


def test_human_identifiers_are_anonymized_in_every_report_group(tmp_path):
    before, after, manifest, options = fixture(tmp_path)
    case = manifest['cases'][0]
    case.update(id='Private bracket.stl-q00', source_model_id='Private bracket',
                geometry_family='Private design family')
    resolved = compare._cases(manifest, tmp_path, 'development')[0]
    for run in (before, after):
        row = run['results'][0]
        row.update(id=case['id'], source_model_id=case['source_model_id'])
        row['evaluation_fingerprint'] = compare._hash(
            {'schema': compare.CACHE_SCHEMA, 'case': resolved, 'identity': run['identity']})
    report = compare.compare_runs(before, after, manifest, **options)
    text = json.dumps(report)
    assert 'Private' not in text
    assert report['cases'][0]['id'].startswith('case-')
    assert report['coverage_scope']['full_1000x10_matched'] is False


def test_scored_execution_failures_remain_failures_with_explicit_missing_geometry(tmp_path):
    before, after, manifest, options = fixture(tmp_path)
    before['results'][0].update(status='crash', failure_kind='converter_exit')
    row = after['results'][0]
    row.update(accepted=False, status='timeout', failure_kind='converter_timeout')
    for key in ('fixture_sha256', 'reference_dimension_max_error_mm', 'valid_solid'):
        row.pop(key)
    report = compare.compare_runs(before, after, manifest, **options)
    assert report['candidate_details']['outcomes']['timeout_cases'] == 1
    assert report['baseline_details']['outcomes']['crash_cases'] == 1
    observed = report['cases'][0]['candidate']
    assert observed['topology']['valid_solid'] is None
    assert observed['measurements']['reference_dimension_max_error_mm'] is None


@pytest.mark.parametrize('text', ['{"results":[],"results":[]}', '{"value":NaN}'])
def test_ambiguous_or_nonfinite_evidence_json_is_refused(tmp_path, text):
    path = tmp_path / 'ambiguous.json'
    path.write_text(text)
    with pytest.raises(compare.ComparisonError):
        compare._read(path)
