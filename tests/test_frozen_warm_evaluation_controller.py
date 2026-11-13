import ctypes
import json
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import frozen_warm_evaluation_controller as warm


class Fixture:
    """Protocol-only files/services; these tests never qualify a native worker."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / 'owned'
        self.root.mkdir()
        (self.root / 'tools').mkdir()
        self.helper = self.root / 'tools/sealed_grader_session.py'
        self.helper.write_text('protocol-only helper, not native qualification')
        self.evidence = self.root / 'native-proof.json'
        self.evidence.write_text('{"protocol_fixture":true}')
        self.worker = self.root / 'worker'
        self.worker.mkdir()
        self.qualification = self.root / 'qualification.json'
        self.configuration = warm.cold.Configuration(
            self.root / 'manifest.json', self.root / 'converter', 'converter-pin',
            self.root / 'grader', 'grader-pin', 'existing-profile', self.root,
            self.root / 'results.json',
            batch_cases=15,
        )
        self.receipt = {
            'schema': 1, 'kind': warm.QUALIFICATION_KIND, 'qualified': True,
            'worker_manifest_sha256': 'worker-pin', 'grader_manifest_sha256': 'grader-pin',
            'parent_helper_sha256': warm.cold._digest(self.helper),
            'entry_source_sha256': 'entry-pin',
            'runtime_inventory_sha256': warm.cold._inventory_hash({'python.exe': 'runtime-hash'}),
            'evidence': [{'path': str(self.evidence), 'sha256': warm.cold._digest(self.evidence)}],
            'maximum_cases_per_worker': 15,
        }
        monkeypatch.setattr(warm, 'ROOT', self.root)
        self.options = self.write_receipt()
        self.clock, self.events = {'now': 100.}, []
        self.sessions, self.seals = [], []
        self.fail_start = False
        self.fail_grade = False
        self.result = {'accepted': False, 'reference_maximum_mm': .25, 'original': 'unchanged'}
        fixture = self

        class Seal:
            def __init__(self, catalogs, **_kwargs):
                self.catalogs = catalogs
                self.closed = False
                fixture.seals.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True
                fixture.events.append('seal_close')

            def require_catalogs(self, catalogs):
                assert self.catalogs == catalogs and not self.closed
                return 'union-trace'

        class Session:
            def __init__(self, worker, worker_pin, grader, grader_pin, work, *, bootstrap_session):
                assert worker == fixture.options.worker and worker_pin == 'worker-pin'
                assert grader == fixture.configuration.grader and grader_pin == 'grader-pin'
                assert bootstrap_session is fixture.seals[-1]
                self.work, self.closed = work, False
                self.starts, self.requests = [], []
                fixture.sessions.append(self)

            def start(self, *, deadline):
                self.starts.append(deadline)
                fixture.clock['now'] += 20
                if fixture.fail_start:
                    raise TimeoutError('protocol startup deadline')

            def grade(self, case, retained, destination, *, deadline, artifacts,
                      reference_readiness_only):
                self.requests.append((
                    case, retained, deadline, artifacts, reference_readiness_only,
                ))
                if fixture.fail_grade:
                    raise TimeoutError('protocol request deadline')
                destination.write_text(json.dumps(fixture.result))
                return fixture.result, {'result_sha256': warm.cold._digest(destination)}

            def close(self):
                self.closed = True
                fixture.events.append('worker_close')

        def initialise(backend, configuration):
            backend.configuration = configuration
            backend._contexts = ExitStack()
            backend.verification_catalogs = {
                configuration.converter: {'converter.pyc': 'hash'},
                configuration.grader: {'grader.pyc': 'hash'},
                self.root / 'runtime': {'python.exe': 'runtime-hash'},
            }
            backend.grader_metadata = {
                'entry_source_sha256': 'entry-pin', 'runtime_files': {'python.exe': 'runtime-hash'},
            }
            backend.sealed = SimpleNamespace(__file__=str(self.helper), SealedRuntimeSession=Seal)

        def identity(_backend):
            return {'backend': 'cold', 'tools_sha256': {'cold-source': 'same'},
                    'verification_policy': {'child_grader_full_verification': True}}

        def close(backend):
            backend._contexts.close()

        module = SimpleNamespace(
            __file__=str(self.helper), ENTRY_SOURCE_SHA256='entry-pin',
            _worker_catalog=lambda *_args: (
                {'source_revision': 'worker-revision'}, {self.worker: {'worker.pyc': 'hash'}}),
            SealedGraderSession=Session,
            _WindowsJob=object,
        )
        monkeypatch.setattr(warm.cold._NativeBackend, 'initialise', initialise)
        monkeypatch.setattr(warm.cold._NativeBackend, 'identity', identity)
        monkeypatch.setattr(warm.cold._NativeBackend, 'close', close)
        monkeypatch.setattr(warm.cold, '_load_tool', lambda name: module)
        monkeypatch.setattr(warm.time, 'monotonic', lambda: self.clock['now'])
        monkeypatch.setattr(warm, '_handle_count', lambda: 123)
        self.backend = warm.WarmBackend(self.options)

    def write_receipt(self):
        self.qualification.write_text(json.dumps(self.receipt))
        return warm.WarmOptions(
            self.worker, 'worker-pin', self.qualification, warm.cold._digest(self.qualification),
        )


@pytest.mark.parametrize('field,value', [
    ('qualified', False), ('worker_manifest_sha256', 'other-worker'),
    ('grader_manifest_sha256', 'other-grader'), ('parent_helper_sha256', 'other-helper'),
    ('evidence', []),
])
def test_unqualified_or_foreign_native_receipt_is_refused_before_bootstrap(
    tmp_path, monkeypatch, field, value,
):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.receipt[field] = value
    options = fixture.write_receipt()
    with pytest.raises(warm.cold.ControllerError, match='qualification'):
        warm.WarmBackend(options).initialise(fixture.configuration)
    assert not fixture.seals and not fixture.sessions


def test_changed_native_evidence_cannot_authorize_warm_mode(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.evidence.write_text('changed proof')
    with pytest.raises(warm.cold.ControllerError, match='evidence changed'):
        fixture.backend.initialise(fixture.configuration)
    assert not fixture.seals and not fixture.sessions


def test_bootstrap_seals_complete_union_and_defers_worker_start(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.backend.initialise(fixture.configuration)
    assert set(fixture.seals[0].catalogs) == {
        fixture.configuration.converter, fixture.configuration.grader,
        fixture.root / 'runtime', fixture.worker,
    }
    assert not fixture.sessions
    assert fixture.backend.verification_session is fixture.seals[0]
    identity = fixture.backend.identity()
    assert identity['backend'] != 'cold'
    assert identity['warm_grader']['worker_manifest_sha256'] == 'worker-pin'
    assert identity['warm_grader']['qualification_sha256'] == fixture.options.qualification_sha256
    assert identity['warm_grader']['maximum_requests_per_worker'] == 15
    assert not identity['verification_policy']['child_grader_full_verification']
    assert identity['verification_policy']['child_grader_startup_full_hash']
    assert identity['process']['grader_transport'] == 'sealed-warm-grader-v1'
    assert 'tools/frozen_warm_evaluation_controller.py' in identity['tools_sha256']
    bootstrap = json.loads(next(fixture.root.glob('warm-bootstrap-*.json')).read_text())
    assert bootstrap['worker_started'] is False
    assert bootstrap['additional_parent_handles'] == 0  # Protocol fake seals own no native handles.
    fixture.backend.close()


def test_grade_and_readiness_share_actual_session_and_original_remaining_deadline(
    tmp_path, monkeypatch,
):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.backend.initialise(fixture.configuration)
    identity_before = fixture.backend.identity()
    destination = fixture.root / 'grade.json'
    result = fixture.backend.grade(
        fixture.root / 'case.json', fixture.root / 'retained', destination, None, timeout=60,
    )
    assert result is fixture.result and json.loads(destination.read_text()) == fixture.result
    session = fixture.sessions[0]
    assert session.starts == [160.] and session.requests[0][2] == 160.
    assert fixture.clock['now'] == 120.  # Startup consumed the same request's allowance.
    assert session.requests[0][-1] is False
    readiness = fixture.root / 'readiness.json'
    fixture.backend.readiness(fixture.root / 'case.json', readiness, timeout=30)
    assert len(fixture.sessions) == 1 and session.requests[1][2] == 150.
    assert session.requests[1][-1] is True
    assert destination.with_suffix('.warm-response.json').exists()
    assert fixture.backend.identity() == identity_before
    fixture.backend.close()
    assert fixture.events == ['worker_close', 'seal_close']


@pytest.mark.parametrize('stage', ['start', 'grade'])
def test_failed_request_closes_worker_and_next_request_can_start_a_fresh_one(
    tmp_path, monkeypatch, stage,
):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.backend.initialise(fixture.configuration)
    fixture.fail_start = stage == 'start'
    fixture.fail_grade = stage == 'grade'
    with pytest.raises(TimeoutError):
        fixture.backend.grade(
            fixture.root / 'case.json', fixture.root / 'retained',
            fixture.root / 'failed.json', None, timeout=60,
        )
    assert fixture.sessions[0].closed and not fixture.seals[0].closed
    fixture.fail_start = fixture.fail_grade = False
    fixture.backend.grade(
        fixture.root / 'case2.json', fixture.root / 'retained',
        fixture.root / 'success.json', None, timeout=60,
    )
    assert len(fixture.sessions) == 2
    fixture.backend.close()


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf'), True])
def test_invalid_or_expired_budget_never_starts_a_worker(tmp_path, monkeypatch, timeout):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.backend.initialise(fixture.configuration)
    with pytest.raises(warm.cold.CaseTimeout):
        fixture.backend.grade(
            fixture.root / 'case.json', fixture.root / 'retained',
            fixture.root / 'grade.json', None, timeout=timeout,
        )
    assert not fixture.sessions
    fixture.backend.close()


def test_facade_uses_original_controller_seam_without_copying_pipeline(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    seen = []

    def run(configuration, *, _backend):
        seen.append((configuration, _backend))
        return {'original_controller_result': True}

    monkeypatch.setattr(warm.cold, 'run_controller', run)
    assert warm.run_warm_controller(fixture.configuration, fixture.options) == {
        'original_controller_result': True,
    }
    assert seen[0][0] is fixture.configuration
    assert isinstance(seen[0][1], warm.WarmBackend)


def test_import_starts_no_worker_and_loads_no_application_modules():
    result = subprocess.check_output([
        sys.executable, '-I', '-B', '-c',
        'import importlib.util,sys; '
        's=importlib.util.spec_from_file_location("checked",sys.argv[1]); '
        'm=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m); '
        'print(any(n=="app" or n.startswith("app.") for n in sys.modules))',
        str(Path(warm.__file__).resolve()),
    ], text=True)
    assert result.strip() == 'False'


@pytest.mark.parametrize('field', ['entry_source_sha256', 'runtime_inventory_sha256'])
def test_native_receipt_must_cover_the_exact_entry_and_native_runtime(tmp_path, monkeypatch, field):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.receipt[field] = 'different-proof'
    backend = warm.WarmBackend(fixture.write_receipt())
    with pytest.raises(warm.cold.ControllerError, match='this grader and runtime'):
        backend.initialise(fixture.configuration)
    assert not fixture.sessions
    backend.close()


def test_request_budget_never_exceeds_original_maximum(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.backend.initialise(fixture.configuration)
    fixture.backend.grade(
        fixture.root / 'case.json', fixture.root / 'retained',
        fixture.root / 'grade.json', None, timeout=1000,
    )
    assert fixture.sessions[0].starts == [460.]
    assert fixture.sessions[0].requests[0][2] == 460.
    fixture.backend.close()


def test_inherited_converter_bridge_receives_the_same_live_union_guard(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.backend.initialise(fixture.configuration)
    seen = []
    fixture.backend.adapter = SimpleNamespace(
        convert_isolated=lambda *a, **kw: seen.append((a, kw)),
    )
    fixture.backend.convert(
        input_stl=fixture.root / 'input.stl', input_sha256='input-pin', units='mm',
        output_dir=fixture.root / 'retained', case_deadline=200.,
    )
    assert seen[0][1]['verification_session'] is fixture.backend.verification_session
    assert seen[0][1]['case_deadline'] == 200.
    assert seen[0][0] == (fixture.configuration.converter, 'converter-pin')
    fixture.backend.close()


@pytest.mark.parametrize('batch', [None, 0, -1, True, 15.0, float('inf'), float('nan')])
def test_unbounded_or_noninteger_batch_is_refused_before_bootstrap(tmp_path, monkeypatch, batch):
    fixture = Fixture(tmp_path, monkeypatch)
    configuration = replace(fixture.configuration, batch_cases=batch)
    with pytest.raises(warm.cold.ControllerError, match='batch-cases'):
        fixture.backend.initialise(configuration)
    assert not fixture.seals and not fixture.sessions


@pytest.mark.parametrize('limit', [None, 0, -1, True, 15.0, float('inf')])
def test_absent_or_invalid_native_worker_limit_cannot_qualify_use(tmp_path, monkeypatch, limit):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.receipt['maximum_cases_per_worker'] = limit
    backend = warm.WarmBackend(fixture.write_receipt())
    with pytest.raises(warm.cold.ControllerError, match='integer worker limit'):
        backend.initialise(fixture.configuration)
    assert not fixture.seals and not fixture.sessions


def test_request_limit_counts_readiness_and_recycles_with_same_remaining_deadline(
    tmp_path, monkeypatch,
):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.receipt['maximum_cases_per_worker'] = 2
    fixture.configuration = replace(fixture.configuration, batch_cases=2)
    backend = warm.WarmBackend(fixture.write_receipt())
    backend.initialise(fixture.configuration)
    backend.grade(fixture.root / 'case.json', fixture.root / 'retained',
                  fixture.root / 'first.json', None, timeout=60)
    backend.readiness(fixture.root / 'case.json', fixture.root / 'second.json', timeout=60)
    assert len(fixture.sessions) == 1 and len(fixture.sessions[0].requests) == 2
    original_deadline = fixture.clock['now'] + 60
    backend.grade(fixture.root / 'case.json', fixture.root / 'retained',
                  fixture.root / 'third.json', None, timeout=60)
    assert fixture.sessions[0].closed and len(fixture.sessions[0].requests) == 2
    assert len(fixture.sessions) == 2 and len(fixture.sessions[1].requests) == 1
    assert fixture.sessions[1].starts == [original_deadline]
    assert fixture.sessions[1].requests[0][2] == original_deadline
    response = json.loads((fixture.root / 'third.warm-response.json').read_text())
    assert response['worker_request_number'] == 1 and response['maximum_requests_per_worker'] == 2
    backend.close()


def test_batch_above_fifteen_recycles_each_worker_at_qualified_limit(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.configuration = replace(fixture.configuration, batch_cases=30)
    fixture.backend.initialise(fixture.configuration)
    for number in range(16):
        fixture.backend.grade(
            fixture.root / 'case.json', fixture.root / 'retained',
            fixture.root / f'grade-{number}.json', None, timeout=60,
        )
    assert [len(session.requests) for session in fixture.sessions] == [15, 1]
    assert fixture.sessions[0].closed and not fixture.sessions[1].closed
    identity = fixture.backend.identity()
    assert identity['warm_grader']['maximum_requests_per_worker'] == 15
    assert 'maximum_batch_cases' not in identity['warm_grader']
    fixture.backend.close()


class NativeLimitsFixture:
    """Actual ctypes layouts with fake kernel operations, never a native process."""

    def __init__(self, *, previous_limit=0, corrupt=False):
        from tools import windows_evaluation_sandbox as host

        self.limits = host._ExtendedLimits()
        self.limits.Basic.LimitFlags = 0x2000 | 0x10 | 0x20 | 0x8 | 0x200
        if previous_limit:
            self.limits.Basic.LimitFlags |= warm.PROCESS_MEMORY_FLAG
        self.limits.Basic.Affinity = 4
        self.limits.Basic.PriorityClass = 0x4000
        self.limits.Basic.ActiveProcessLimit = 1
        self.limits.Basic.MinimumWorkingSetSize = 123
        self.limits.ProcessMemory = previous_limit
        self.limits.JobMemory = 987654321
        self.before = bytes(self.limits)
        self.events, self.actions = [], []

        def query(_job, kind, buffer, size, _required):
            assert kind == 9 and size == ctypes.sizeof(self.limits)
            ctypes.memmove(buffer, ctypes.byref(self.limits), size)
            return True

        def update(_job, kind, buffer, size):
            assert kind == 9
            ctypes.memmove(ctypes.byref(self.limits), buffer, size)
            if corrupt:
                self.limits.Basic.Affinity = 2
            return True

        self.native = SimpleNamespace(
            job='owned-job', kernel=SimpleNamespace(
                QueryInformationJobObject=query, SetInformationJobObject=update,
            ), _check=lambda value: value,
            next_event=lambda _timeout: self.events.pop(0) if self.events else None,
            close=lambda: self.actions.append('native_close'),
            terminate=lambda: self.actions.append('terminate_owned_job'),
        )
        self.job = SimpleNamespace(native=self.native, host=host)


@pytest.mark.parametrize('previous_limit', [0, 2 * 1024**3])
def test_native_memory_limit_preserves_all_existing_job_policies(previous_limit):
    fixture = NativeLimitsFixture(previous_limit=previous_limit)
    guard = warm._JobMemoryGuard(fixture.job)
    expected = bytearray(fixture.before)
    original = type(fixture.limits).from_buffer_copy(expected)
    original.Basic.LimitFlags |= warm.PROCESS_MEMORY_FLAG
    original.ProcessMemory = previous_limit or warm.GRADER_PROCESS_MEMORY_BYTES
    assert bytes(fixture.limits) == bytes(original)
    assert guard.limit == (previous_limit or 8 * 1024**3)


def test_job_policy_readback_mismatch_is_refused():
    fixture = NativeLimitsFixture(corrupt=True)
    with pytest.raises(warm.cold.GraderError, match='another job limit'):
        warm._JobMemoryGuard(fixture.job)


def test_memory_events_are_latched_during_request_and_final_native_drain():
    fixture = NativeLimitsFixture()
    guard = warm._JobMemoryGuard(fixture.job)
    fixture.events.append((9, 100))
    assert fixture.native.next_event(0) == (9, 100)
    fixture.events.append((10, 100))
    fixture.native.close()
    assert guard.evidence()['memory_limit_events'] == [
        {'message': 9, 'pid': 100}, {'message': 10, 'pid': 100},
    ]
    assert fixture.actions == ['terminate_owned_job', 'terminate_owned_job', 'native_close']


@pytest.mark.parametrize('result', [
    {'accepted': True, 'reference_topology': {
        'diagnostic_error': 'MemoryError: allocation refused',
    }},
    {'accepted': False, 'fixture_readiness': {
        'fixture_ready': False, 'preparation_error': 'Standard_OutOfMemory: BRepMesh failed',
    }},
])
def test_caught_memory_errors_never_become_success_or_source_invalidity(
    tmp_path, monkeypatch, result,
):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.result = result
    fixture.backend.initialise(fixture.configuration)
    destination = fixture.root / 'grade.json'
    with pytest.raises(warm.GraderResourceLimit) as error:
        fixture.backend.grade(fixture.root / 'case.json', fixture.root / 'retained',
                             destination, None, timeout=60)
    assert error.value.classification == 'grader_resource_limit'
    assert error.value.resource_evidence['structured_memory_errors']
    assert fixture.sessions[0].closed
    assert json.loads(destination.read_text()) == result  # Actual child evidence is preserved.
    assert destination.with_suffix('.grader-resource.json').exists()
    fixture.backend.close()


def test_memory_event_discovered_only_during_drain_overrides_generic_worker_failure(
    tmp_path, monkeypatch,
):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.backend.initialise(fixture.configuration)
    session = fixture.backend._session(160.)
    events = []
    session.job = SimpleNamespace(memory_guard=SimpleNamespace(evidence=lambda: {
        'process_memory_limit_bytes': warm.GRADER_PROCESS_MEMORY_BYTES,
        'memory_limit_events': list(events),
    }), process=SimpleNamespace(returncode=None))

    def failed_grade(*_args, **_kwargs):
        raise RuntimeError('worker exited before a response')

    def drain():
        session.closed = True
        session.job.process.returncode = 1
        events.append({'message': 9, 'pid': 123})

    session.grade, session.close = failed_grade, drain
    with pytest.raises(warm.GraderResourceLimit) as error:
        fixture.backend.grade(fixture.root / 'case.json', fixture.root / 'retained',
                             fixture.root / 'grade.json', None, timeout=60)
    assert error.value.resource_evidence['memory_limit_events']
    assert error.value.resource_evidence['worker_drained'] is True
    fixture.backend.close()


def test_resource_logging_failure_cannot_skip_worker_drain(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.result = {
        'accepted': True, 'diagnostic_error': 'MemoryError: native allocation refused',
    }
    fixture.backend.initialise(fixture.configuration)

    def refused_log(*_args, **_kwargs):
        raise OSError('synthetic evidence volume full')

    monkeypatch.setattr(warm.cold, '_atomic', refused_log)
    with pytest.raises(warm.GraderResourceLimit) as error:
        fixture.backend.grade(fixture.root / 'case.json', fixture.root / 'retained',
                             fixture.root / 'grade.json', None, timeout=60)
    assert fixture.sessions[0].closed
    assert 'volume full' in error.value.resource_evidence['evidence_write_error']
    fixture.backend.close()
