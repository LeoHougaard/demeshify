"""Opt-in warm grader backend for the unchanged frozen evaluation controller.

No acceptance formula or evaluator is duplicated here. Production requires a
pinned native qualification receipt; importing this module starts no worker.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import frozen_evaluation_controller as cold  # noqa: E402

QUALIFICATION_KIND = 'sealed-grader-native-qualification-v1'
GRADER_PROCESS_MEMORY_BYTES = 8 * 1024**3
PROCESS_MEMORY_FLAG = 0x100
MEMORY_MESSAGES = {9, 10}  # JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT / JOB_MEMORY_LIMIT.


class GraderResourceLimit(cold.GraderError):
    classification = 'grader_resource_limit'

    def __init__(self, evidence: dict):
        self.resource_evidence = evidence
        super().__init__('Grader memory resource limit; source validity and solve remain unscored')


def _memory_errors(value) -> list[str]:
    """Do not let caught native/Python OOM become a source-invalid or passing row."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and str(key).endswith('error') and any(
                token in item.lower() for token in (
                    'memoryerror', 'standard_outofmemory', 'std::bad_alloc', 'out of memory',
                    'not enough memory', 'insufficient memory',
                )
            ):
                found.append(item[:2000])
            elif isinstance(item, (dict, list)):
                found.extend(_memory_errors(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_memory_errors(item))
    return found


class _JobMemoryGuard:
    """Apply a native committed-memory limit and latch its job notifications."""

    def __init__(self, job):
        self.job, self.native = job, job.native
        self.events, self.observer_error = [], None
        self.stop = threading.Event()
        self.thread = None
        self._next_event, self._native_close = self.native.next_event, self.native.close
        limits = job.host._ExtendedLimits()
        self.native._check(self.native.kernel.QueryInformationJobObject(
            self.native.job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None,
        ))
        before = {name: getattr(limits.Basic, name) for name, _kind in limits.Basic._fields_}
        previous_limit = limits.ProcessMemory
        effective = min(GRADER_PROCESS_MEMORY_BYTES, previous_limit) if (
            limits.Basic.LimitFlags & PROCESS_MEMORY_FLAG and previous_limit
        ) else GRADER_PROCESS_MEMORY_BYTES
        limits.Basic.LimitFlags |= PROCESS_MEMORY_FLAG
        limits.ProcessMemory = effective
        self.native._check(self.native.kernel.SetInformationJobObject(
            self.native.job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ))
        actual = job.host._ExtendedLimits()
        self.native._check(self.native.kernel.QueryInformationJobObject(
            self.native.job, 9, ctypes.byref(actual), ctypes.sizeof(actual), None,
        ))
        expected = {**before, 'LimitFlags': before['LimitFlags'] | PROCESS_MEMORY_FLAG}
        if (any(getattr(actual.Basic, name) != value for name, value in expected.items())
                or actual.ProcessMemory != effective
                or actual.JobMemory != limits.JobMemory):
            raise cold.GraderError('Native grader memory guard changed another job limit')
        self.limit = effective
        self.native.next_event = self.next_event
        self.native.close = self.close_native

    def next_event(self, timeout):
        event = self._next_event(timeout)
        if event is not None and event[0] in MEMORY_MESSAGES:
            self.events.append({'message': event[0], 'pid': event[1]})
            self.stop.set()
            self.native.terminate()  # Only this owned, bounded worker job.
        return event

    def _observe(self):
        try:
            while not self.stop.is_set():
                self.next_event(.05)
        except Exception as exc:
            self.observer_error = f'{type(exc).__name__}: {exc}'
            self.stop.set()
            try:
                self.native.terminate()
            except Exception:
                pass

    def start(self):
        self.stop.clear()
        self.thread = threading.Thread(target=self._observe, daemon=True)
        self.thread.start()

    def pending(self):
        for _ in range(64):
            if self.next_event(0) is None:
                return
        if not self.events:
            self.observer_error = 'Job notification queue did not drain within the bounded scan'

    def finish_observing(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=.2)
            if self.thread.is_alive():
                raise cold.GraderError('Grader memory notification observer did not stop')

    def close_native(self):
        try:
            self.pending()  # Also capture events arriving during native termination/drain.
        finally:
            self._native_close()

    def evidence(self):
        return {'process_memory_limit_bytes': self.limit, 'memory_limit_events': list(self.events),
                'observer_error': self.observer_error}


def _guarded_job_class(original):
    class GuardedJob(original):
        def __init__(self):
            self.memory_guard = None
            super().__init__()
            try:
                self.memory_guard = _JobMemoryGuard(self)
                self.memory_guard.start()
            except BaseException:
                self.close()
                raise

        def close(self):
            guard = getattr(self, 'memory_guard', None)
            try:
                if guard is not None:
                    guard.finish_observing()
            finally:
                super().close()

    return GuardedJob


@dataclass(frozen=True)
class WarmOptions:
    worker: Path
    worker_sha256: str
    qualification: Path
    qualification_sha256: str


def _qualification(options: WarmOptions, configuration: cold.Configuration) -> dict:
    """Require explicitly pinned, identity-bound native evidence before use."""
    cold._plain(options.qualification, configuration.owned_root)
    if cold._digest(options.qualification) != options.qualification_sha256:
        raise cold.ControllerError('Warm native qualification differs from its pinned identity')
    value = json.loads(options.qualification.read_text(encoding='utf-8'))
    if (value.get('schema') != 1 or value.get('kind') != QUALIFICATION_KIND
            or value.get('qualified') is not True
            or value.get('worker_manifest_sha256') != options.worker_sha256
            or value.get('grader_manifest_sha256') != configuration.grader_sha256
            or value.get('parent_helper_sha256') != cold._digest(
                ROOT / 'tools/sealed_grader_session.py')):
        raise cold.ControllerError('Warm qualification is absent or targets another implementation')
    evidence = value.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise cold.ControllerError('Warm qualification requires preserved native evidence files')
    for item in evidence:
        path = Path(item['path'])
        path = path if path.is_absolute() else options.qualification.parent / path
        cold._plain(path, configuration.owned_root)
        if cold._digest(path) != item['sha256']:
            raise cold.ControllerError('Warm native qualification evidence changed')
    return value


def _deadline(timeout: float) -> float:
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise cold.CaseTimeout('Warm grading requires the remaining original case budget')
    return time.monotonic() + min(timeout, cold.GRADING_TIMEOUT)


def _handle_count() -> int | None:
    """Observe this process only; unavailable diagnostics never become zero."""
    if sys.platform != 'win32':
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.GetProcessHandleCount.restype = ctypes.c_int
    count = ctypes.c_ulong()
    if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
        return None
    return count.value


class WarmBackend(cold._NativeBackend):
    """Change only grading transport; conversion, scoring and cache stay upstream."""

    def __init__(self, options: WarmOptions):
        self.options = options
        self.warm_session = None
        self.worker_requests = 0

    def initialise(self, configuration: cold.Configuration):
        qualification = _qualification(self.options, configuration)
        limit = qualification.get('maximum_cases_per_worker')
        if type(limit) is not int or limit <= 0:
            raise cold.ControllerError('Qualification requires a positive integer worker limit')
        if type(configuration.batch_cases) is not int or configuration.batch_cases <= 0:
            raise cold.ControllerError('Warm --batch-cases must be an explicit positive integer')
        self.maximum_worker_requests = limit
        super().initialise(configuration)
        self.warm = cold._load_tool('sealed_grader_session')
        self.worker_metadata, worker_catalogs = self.warm._worker_catalog(
            self.options.worker, self.options.worker_sha256,
        )
        if (qualification.get('entry_source_sha256') != self.grader_metadata['entry_source_sha256']
                or self.grader_metadata['entry_source_sha256'] != self.warm.ENTRY_SOURCE_SHA256
                or qualification.get('runtime_inventory_sha256') != cold._inventory_hash(
                    self.grader_metadata['runtime_files'])):
            raise cold.ControllerError('Warm qualification does not cover this grader and runtime')
        catalogs = dict(self.verification_catalogs)
        for root, files in worker_catalogs.items():
            cold._owned(root, configuration.owned_root)
            if root in catalogs and catalogs[root] != files:
                raise cold.ControllerError('Warm worker conflicts with a pinned artifact catalog')
            catalogs[root] = files
        # Keep the original context's ownership intact. The new actual live
        # union protects converter, grader, worker and runtime together.
        before_handles, started = _handle_count(), time.perf_counter()
        self.verification_session = self._contexts.enter_context(
            self.sealed.SealedRuntimeSession(
                catalogs, module_sha256=cold._digest(Path(self.sealed.__file__)),
            )
        )
        self.verification_catalogs = catalogs
        self.session_trace = self.verification_session.require_catalogs(catalogs)
        after_handles = _handle_count()
        bootstrap = configuration.output.parent / ('warm-bootstrap-' + uuid.uuid4().hex + '.json')
        cold._atomic(bootstrap, {
            'schema': 1, 'worker_manifest_sha256': self.options.worker_sha256,
            'qualification_sha256': self.options.qualification_sha256,
            'union_seal_wall_seconds': time.perf_counter() - started,
            'old_parent_seal_still_owned': True,
            'parent_handles_before_union': before_handles,
            'parent_handles_after_union': after_handles,
            'additional_parent_handles': (after_handles - before_handles
                                          if None not in (before_handles, after_handles) else None),
            'worker_started': False,
        }, configuration.owned_root, exclusive=True)

    def verify(self):
        super().verify()
        _qualification(self.options, self.configuration)

    def identity(self) -> dict:
        value = super().identity()
        value['backend'] = 'isolated-converter-sealed-warm-grader-controller-v1'
        value['tools_sha256'] = {
            **value['tools_sha256'],
            'tools/frozen_warm_evaluation_controller.py': cold._digest(Path(__file__)),
            'tools/sealed_grader_session.py': cold._digest(Path(self.warm.__file__)),
        }
        value['warm_grader'] = {
            'worker_manifest_sha256': self.options.worker_sha256,
            'worker_source_revision': self.worker_metadata['source_revision'],
            'qualification_sha256': self.options.qualification_sha256,
            'worker_startup': 'deferred until a bounded grading/readiness request',
            'entry_execution': 'unchanged pinned ENTRY with fresh request globals',
            'maximum_requests_per_worker': self.maximum_worker_requests,
            'batch_policy': 'explicit positive integer; independent of worker request limit',
            'limit_policy': 'count grading and readiness; close and replace before excess request',
            'process_memory_limit_bytes': GRADER_PROCESS_MEMORY_BYTES,
            'memory_limit_policy': 'unscored grader_resource_limit; never fixture invalidity',
        }
        # The original readiness-cache contract includes process metadata.
        # Bind its entries to this transport as well as binding case caches.
        value['process'] = {
            **value.get('process', {}),
            'grader_transport': 'sealed-warm-grader-v1',
            'worker_manifest_sha256': self.options.worker_sha256,
            'qualification_sha256': self.options.qualification_sha256,
            'maximum_requests_per_worker': self.maximum_worker_requests,
            'grader_process_memory_limit_bytes': GRADER_PROCESS_MEMORY_BYTES,
        }
        value['verification_policy'] = {
            **value['verification_policy'],
            'child_grader_full_verification': False,
            'child_grader_startup_full_hash': True,
            'child_grader_per_request': 'own OS seal, exact catalogs and original ENTRY checks',
        }
        return value

    def _session(self, deadline: float):
        cold._remaining(deadline)
        if (self.warm_session is not None and not self.warm_session.closed
                and self.worker_requests >= self.maximum_worker_requests):
            self.warm_session.close()
            cold._remaining(deadline)
        if self.warm_session is None or self.warm_session.closed:
            work = self.configuration.owned_root / ('warm-grader-' + uuid.uuid4().hex)
            cold._owned(work, self.configuration.owned_root)
            self.warm_session = self.warm.SealedGraderSession(
                self.options.worker, self.options.worker_sha256,
                self.configuration.grader, self.configuration.grader_sha256, work,
                bootstrap_session=self.verification_session,
            )
            self.worker_requests = 0
            original_job = self.warm._WindowsJob
            try:
                self.warm._WindowsJob = _guarded_job_class(original_job)
                self.warm_session.start(deadline=deadline)
            except BaseException:
                self.warm_session.close()
                raise
            finally:
                self.warm._WindowsJob = original_job
        return self.warm_session

    def _resource_evidence(self, session, result=None, error=None, *, poll=False):
        job = getattr(session, 'job', None)
        guard = getattr(job, 'memory_guard', None)
        if guard is not None and poll:
            guard.finish_observing()
            guard.pending()
        evidence = (guard.evidence() if guard is not None else {
            'process_memory_limit_bytes': GRADER_PROCESS_MEMORY_BYTES, 'memory_limit_events': [],
        })
        errors = _memory_errors(result)
        if isinstance(error, MemoryError):
            errors.append(f'MemoryError: {error}')
        evidence['structured_memory_errors'] = errors
        process = getattr(job, 'process', None)
        evidence['worker_drained'] = (getattr(process, 'returncode', None) is not None
                                      if process is not None else None)
        return evidence

    def _raise_resource_limit(self, destination, evidence, error=None):
        try:
            cold._atomic(destination.with_suffix('.grader-resource.json'), evidence,
                         self.configuration.owned_root, exclusive=True)
        except Exception as exc:
            evidence['evidence_write_error'] = f'{type(exc).__name__}: {exc}'
        raise GraderResourceLimit(evidence) from error

    def _request(self, case_configuration, retained, destination, artifacts, timeout, *, readiness):
        deadline = _deadline(timeout)
        started = time.perf_counter()
        session = self._session(deadline)
        result = None
        try:
            self.worker_requests += 1  # Readiness and failed requests consume the same limit.
            result, response = session.grade(
                case_configuration, retained, destination, deadline=deadline,
                artifacts=artifacts, reference_readiness_only=readiness,
            )
            resource = self._resource_evidence(session, result, poll=True)
            if resource['memory_limit_events'] or resource['structured_memory_errors']:
                raise GraderResourceLimit(resource)
            if resource.get('observer_error'):
                raise cold.GraderError('Grader memory notification observer failed')
            guard = getattr(getattr(session, 'job', None), 'memory_guard', None)
            if guard is not None:
                guard.start()
            # Preserve the actual original ENTRY result, including all geometry
            # and acceptance measurements; transport evidence stays alongside it.
            cold._atomic(destination.with_suffix('.warm-response.json'), {
                'schema': 1, 'worker_manifest_sha256': self.options.worker_sha256,
                'qualification_sha256': self.options.qualification_sha256,
                'session_work': str(session.work), 'request': response,
                'request_including_startup_wall_seconds': time.perf_counter() - started,
                'reference_readiness_only': readiness,
                'worker_request_number': self.worker_requests,
                'maximum_requests_per_worker': self.maximum_worker_requests,
            }, self.configuration.owned_root, exclusive=True)
            cold._remaining(deadline)
            return result
        except BaseException as exc:
            try:
                session.close()  # Drain before logging or classifying failed geometry grading.
            finally:
                evidence = self._resource_evidence(session, result, exc)
                if evidence['memory_limit_events'] or evidence['structured_memory_errors']:
                    self._raise_resource_limit(destination, evidence, exc)
            raise

    def grade(self, case_configuration, retained, destination, artifacts, *, timeout):
        return self._request(
            case_configuration, retained, destination, artifacts, timeout, readiness=False,
        )

    def readiness(self, case_configuration, destination, *, timeout):
        return self._request(
            case_configuration, destination.parent, destination, None, timeout, readiness=True,
        )

    def close(self):
        try:
            if self.warm_session is not None:
                self.warm_session.close()
        finally:
            super().close()


def run_warm_controller(configuration: cold.Configuration, options: WarmOptions) -> dict:
    return cold.run_controller(configuration, _backend=WarmBackend(options))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    for name in ('converter', 'grader', 'worker', 'qualification', 'owned-root', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    for name in ('converter-sha256', 'grader-sha256', 'worker-sha256', 'qualification-sha256',
                 'profile'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--batch-cases', type=int)
    parser.add_argument('--split', choices=('development', 'validation', 'holdout'))
    args = parser.parse_args()
    configuration = cold.Configuration(
        args.manifest.resolve(), args.converter.resolve(), args.converter_sha256,
        args.grader.resolve(), args.grader_sha256, args.profile, args.owned_root.resolve(),
        args.output.resolve(), args.artifacts.resolve() if args.artifacts else None,
        args.batch_cases, args.split,
    )
    options = WarmOptions(args.worker.resolve(), args.worker_sha256,
                          args.qualification.resolve(), args.qualification_sha256)
    result = run_warm_controller(configuration, options)
    if result['halt_reason']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
