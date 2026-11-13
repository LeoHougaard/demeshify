"""Unintegrated serial grader candidate: exact frozen ENTRY, live native proof.

Each worker owns its own read-locked catalogs. A parent Windows Job bounds and
drains every request; identities sent over IPC never substitute for live locks.
No reconstruction acceptance formula is implemented here.
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import importlib.util
import json
import marshal
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from pathlib import Path

MAX_REQUEST_SECONDS = 360.0
CLEANUP_SECONDS = 10.0
MAX_FRAME_BYTES = 1024 * 1024
PARENT_JOB_HOST_SHA256 = 'f5d5fc02427af7e0d8a5df1a5b14372e930809dac96f257cd141a7b0da43db36'
ENTRY_SOURCE_SHA256 = 'c21c621b92571217c54c7cc4a88f6249d424846e95c4e493ca1dcbc77f9407e0'
WORKER_SOURCES = ('tools/sealed_grader_session.py', 'tools/sealed_runtime.py',
                  'tools/locked_artifact_identity.py')


class GraderSessionError(RuntimeError):
    pass


class GraderRequestTimeout(GraderSessionError, TimeoutError):
    pass


def _digest(path: Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _remaining(deadline: float) -> float:
    if (isinstance(deadline, bool) or not isinstance(deadline, int | float)
            or not math.isfinite(deadline)):
        raise ValueError('An explicit finite monotonic deadline is required')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GraderRequestTimeout('The original request deadline is exhausted')
    if remaining > MAX_REQUEST_SECONDS:
        raise ValueError('A grader request cannot exceed 360 seconds')
    return remaining


def _committed_sources(project: Path, revision: str):
    commit = subprocess.check_output([
        'git', 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}',
    ], cwd=project, text=True).strip()
    return commit, {name: subprocess.check_output(['git', 'show', commit + ':' + name],
                                                  cwd=project)
                    for name in (*WORKER_SOURCES, 'LICENSE')}


def freeze_worker(project: Path, revision: str, output: Path) -> dict:
    """Build only small committed worker bytecode; never copy a runtime or grader."""
    from tools import freeze_evaluation_converter as compiler

    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('Preserve existing artifacts and select a new worker directory')
    builder_pin, compiler_pin = _digest(Path(__file__)), _digest(Path(compiler.__file__))
    commit, sources = _committed_sources(Path(project), revision)
    output.mkdir(parents=True, exist_ok=False)
    for name, data in sources.items():
        if name == 'LICENSE':
            (output / name).write_bytes(data)
        else:
            target = ('sealed_grader_session.pyc' if name == WORKER_SOURCES[0]
                      else str(Path(name).with_suffix('.pyc')))
            compiler.compile_module(data, name, output / 'application' / target)
    manifest = {'schema': 1, 'artifact_kind': 'sealed_grader_worker', 'source_revision': commit,
                'python_magic': importlib.util.MAGIC_NUMBER.hex(),
                'source_sha256': {name: hashlib.sha256(data).hexdigest()
                                  for name, data in sources.items()},
                'application_files': compiler.inventory(output / 'application'),
                'license_sha256': _digest(output / 'LICENSE'),
                'builder_sha256': builder_pin,
                'builder_dependencies_sha256': {'freeze_evaluation_converter': compiler_pin},
                'parent_job_host_sha256': PARENT_JOB_HOST_SHA256, 'workflow_qualified': False}
    if (_digest(Path(__file__)) != builder_pin
            or _digest(Path(compiler.__file__)) != compiler_pin):
        raise GraderSessionError('Worker builder changed; partial artifact preserved')
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def _worker_catalog(artifact: Path, pin: str):
    from tools.locked_artifact_identity import _catalog, _plain_directory

    artifact = _plain_directory(artifact)
    if _digest(artifact / 'manifest.json') != pin:
        raise GraderSessionError('Worker manifest differs from its pinned identity')
    metadata = json.loads((artifact / 'manifest.json').read_text())
    if (metadata.get('schema') != 1 or metadata.get('artifact_kind') != 'sealed_grader_worker'
            or metadata.get('python_magic') != importlib.util.MAGIC_NUMBER.hex()):
        raise GraderSessionError('Unsupported frozen worker artifact')
    files = {'manifest.json': pin, 'LICENSE': metadata['license_sha256'],
             **_catalog(metadata['application_files'], 'worker', 'application/')}
    return metadata, {artifact: files}


def _merge_catalogs(*catalogs):
    result = {}
    for mapping in catalogs:
        for root, files in mapping.items():
            if root in result and result[root] != files:
                raise GraderSessionError('A shared runtime has conflicting pinned catalogs')
            result[root] = files
    return result


class _FrozenEntryExecutor:
    """Execute the unchanged sealed pyc with fresh globals, not a rewritten grader."""
    def __init__(self, grader: Path, pin: str, worker: Path, session, boot: Path):
        from tools import freeze_evaluation_converter as verifier
        from tools.locked_artifact_identity import verify_locked_artifact

        self.grader, self.pin, self.worker = Path(grader), pin, Path(worker)
        self.session, self.boot = session, Path(boot)
        self.metadata, _ = verify_locked_artifact(
            self.grader, pin, session, expected_kind='grader')
        if self.metadata.get('entry_source_sha256') != ENTRY_SOURCE_SHA256:
            raise GraderSessionError('This frozen ENTRY version has not qualified for reuse')
        self.verify_locked = verify_locked_artifact
        self.verifier = verifier
        self.original_verify = verifier.verify
        self.entry = self.grader / 'application/grader.pyc'
        code = self.entry.read_bytes()
        if code[:4] != importlib.util.MAGIC_NUMBER:
            raise GraderSessionError('Frozen ENTRY has incompatible bytecode')
        self.code = marshal.loads(code[16:])
        if not isinstance(self.code, types.CodeType):
            raise GraderSessionError('Frozen ENTRY is not executable Python bytecode')
        # Warm only imports, never case JSON or geometry. Route import side effects
        # outside every sealed root before app.storage is loaded.
        self.boot.mkdir(parents=True, exist_ok=True)
        self._environment(self.boot)
        self.allowed = {}
        worker_metadata = json.loads((self.worker / 'manifest.json').read_text())
        for root, meta in ((self.grader, self.metadata), (self.worker, worker_metadata)):
            for name in meta['application_files']:
                self.allowed[(root / 'application' / name).resolve()] = True
        self._origins()
        app_spec = importlib.util.find_spec('app')
        if (app_spec is None or app_spec.origin is None
                or Path(app_spec.origin).resolve() not in self.allowed):
            raise GraderSessionError('Application startup origin escaped the frozen grader')
        import app.storage as storage
        from tools import benchmark_multires_corpus as evaluator
        from tools import benchmark_surface_corpus as benchmark

        self.storage, self.evaluator, self.benchmark = storage, evaluator, benchmark
        self.baseline = (evaluator.reconstruct_in_worker, benchmark.reconstruct_surfaces,
                         benchmark.step_surface_metrics)
        self._origins()

    @staticmethod
    def _environment(work):
        os.environ.update(_environment(work))
        tempfile.tempdir = str(work / 'tmp')

    def _origins(self):
        for name, module in tuple(sys.modules.items()):
            if not (name == 'app' or name.startswith('app.')
                    or name == 'tools' or name.startswith('tools.')):
                continue
            path = getattr(module, '__file__', None)
            if path is None:
                allowed = {(self.grader / 'application' / name.replace('.', '/')).resolve(),
                           (self.worker / 'application' / name.replace('.', '/')).resolve()}
                if not set(map(lambda p: Path(p).resolve(), module.__path__)) <= allowed:
                    raise GraderSessionError('A grading namespace escaped the frozen applications')
            elif Path(path).resolve() not in self.allowed:
                raise GraderSessionError(f'Unexpected grading module origin: {name}')

    def _proof(self, artifact, pin):
        if Path(artifact).resolve() != self.grader.resolve() or pin != self.pin:
            raise GraderSessionError('Frozen ENTRY requested another verification identity')
        metadata, _ = self.verify_locked(
            self.grader, self.pin, self.session, expected_kind='grader')
        return metadata

    def run(self, arguments: list[str], *, deadline: float, evidence: Path) -> dict:
        """All acceptance/readiness/hash logic stays inside the original ENTRY."""
        _remaining(deadline)
        if len(arguments) not in (3, 4):
            raise ValueError('Expected the normal frozen ENTRY arguments')
        configuration_path = Path(arguments[1] if arguments[0] == '--reference-readiness'
                                  else arguments[0])
        configuration = json.loads(configuration_path.read_text(encoding='utf-8'))
        outputs = [Path(arguments[2])]
        if len(arguments) == 4:
            outputs.append(Path(arguments[3]))
        if 'scratch_root' in configuration:
            outputs.append(Path(configuration['scratch_root']))
        sealed_roots = (self.grader, self.worker, Path(self.metadata['python_runtime']))
        if any(path.resolve().is_relative_to(root.resolve())
               for path in outputs for root in sealed_roots):
            raise GraderSessionError('Request writes must stay outside all sealed applications')
        self._origins()
        if (self.evaluator.reconstruct_in_worker, self.benchmark.reconstruct_surfaces,
                self.benchmark.step_surface_metrics) != self.baseline:
            raise GraderSessionError('A previous evaluator patch leaked across requests')
        if self.verifier.verify is not self.original_verify:
            raise GraderSessionError('The frozen verifier was already patched')
        evidence = Path(evidence)
        evidence.mkdir(parents=True, exist_ok=False)
        previous_env, previous_argv = dict(os.environ), sys.argv
        previous_temp, previous_paths = tempfile.tempdir, list(sys.path)
        previous_runs = self.storage.RUNS
        original_temp, original_register = tempfile.TemporaryDirectory, atexit.register
        temporaries, registrations = [], []
        namespace = {'__name__': '__main__', '__file__': str(self.entry),
                     '__builtins__': __builtins__}

        def temporary(*args, **kwargs):
            value = original_temp(*args, **kwargs)
            temporaries.append(value)
            if kwargs.get('prefix') == 'grader-work-':
                self.storage.RUNS = Path(value.name) / 'runs'
                os.environ.update(_owned_home(Path(value.name)))
            return value

        def register(function, *args, **kwargs):
            if getattr(function, '__self__', None) in temporaries:
                registrations.append(function)
            return original_register(function, *args, **kwargs)

        failure = None
        installed_proof = self._proof
        try:
            sys.argv = [str(self.entry), *map(str, arguments)]
            tempfile.TemporaryDirectory, atexit.register = temporary, register
            self.verifier.verify = installed_proof
            with (evidence / 'entry.stdout.log').open('w') as stdout, (
                evidence / 'entry.stderr.log').open('w') as stderr:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exec(self.code, namespace)
            _remaining(deadline)
        except BaseException as exc:
            failure = exc
            (evidence / 'failure.json').write_text(json.dumps({
                'qualified': False, 'error': f'{type(exc).__name__}: {exc}',
                'traceback': traceback.format_exc()}, indent=2) + '\n')
        finally:
            leaked = (self.evaluator.reconstruct_in_worker, self.benchmark.reconstruct_surfaces,
                      self.benchmark.step_surface_metrics) != self.baseline
            leaked = leaked or self.verifier.verify is not installed_proof
            if leaked and failure is None:
                failure = GraderSessionError('Frozen evaluator did not restore its global patches')
            self.verifier.verify = self.original_verify
            tempfile.TemporaryDirectory, atexit.register = original_temp, original_register
            self.evaluator.reconstruct_in_worker = self.baseline[0]
            self.benchmark.reconstruct_surfaces = self.baseline[1]
            self.benchmark.step_surface_metrics = self.baseline[2]
            cleanup_error = None
            for function in registrations:
                atexit.unregister(function)
            for value in reversed(temporaries):
                try:
                    value.cleanup()
                except Exception as exc:
                    cleanup_error = exc
            self.storage.RUNS, tempfile.tempdir = previous_runs, previous_temp
            sys.argv, sys.path[:] = previous_argv, previous_paths
            os.environ.clear()
            os.environ.update(previous_env)
            namespace.clear()
            if cleanup_error:
                raise GraderSessionError('Per-request temporary cleanup failed') from cleanup_error
        self.session.check()
        self._origins()
        if failure:
            if not (evidence / 'failure.json').exists():
                (evidence / 'failure.json').write_text(json.dumps({
                    'qualified': False, 'error': f'{type(failure).__name__}: {failure}'},
                    indent=2) + '\n')
            raise GraderSessionError('Frozen ENTRY failed; preserved request evidence') from failure
        destination = Path(arguments[2])
        result = json.loads(destination.read_text())
        if (result.get('grader_manifest_sha256') != self.pin
                or not result.get('second_conversion_not_run')):
            raise GraderSessionError('The unchanged ENTRY did not produce its expected evidence')
        return {'result_sha256': _digest(destination), 'result_path': str(destination.resolve()),
                'second_conversion_not_run': True, 'entry_sha256': _digest(self.entry),
                'temporaries_cleaned': len(temporaries),
                'temporary_atexit_registrations_removed': len(registrations)}


class _WindowsJob:
    """One trusted, gated worker; OS limit forbids any child process creation."""
    def __init__(self):
        import ctypes as C
        from ctypes import wintypes as W

        from tools import windows_evaluation_sandbox as host

        if _digest(Path(host.__file__)) != PARENT_JOB_HOST_SHA256:
            raise GraderSessionError('The parent job supervisor differs from its qualified source')
        self.host, self.native, self.process = host, host._Win32(''), None
        self.closed = False
        try:
            native = self.native
            native.job = native._check(native.kernel.CreateJobObjectW(None, None))
            native.port = native._check(native.kernel.CreateIoCompletionPort(
                W.HANDLE(-1).value, None, 0, 1))
            association = host._CompletionPort(1, native.port)
            native._check(native.kernel.SetInformationJobObject(
                native.job, 7, C.byref(association), C.sizeof(association)))
            mask, system = C.c_size_t(), C.c_size_t()
            native._check(native.kernel.GetProcessAffinityMask(
                native.kernel.GetCurrentProcess(), C.byref(mask), C.byref(system)))
            if not mask.value:
                raise GraderSessionError('No available CPU affinity')
            limits = host._ExtendedLimits()
            limits.Basic.Affinity = mask.value & -mask.value
            limits.Basic.PriorityClass = 0x4000
            limits.Basic.ActiveProcessLimit = 1
            limits.Basic.LimitFlags = 0x2000 | 0x10 | 0x20 | 0x8
            native._check(native.kernel.SetInformationJobObject(
                native.job, 9, C.byref(limits), C.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def launch(self, argv, *, cwd, env, stderr):
        # Popen closes its primary thread handle. Reopen the sole initial thread
        # only after assigning the still-suspended process to the bounded job.
        self.process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=stderr, bufsize=0, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.BELOW_NORMAL_PRIORITY_CLASS | 4)
        native = self.native
        try:
            native.pi.hProcess = native._check(native.kernel.OpenProcess(
                0x100000 | 0x1000 | 0x100 | 1, False, self.process.pid))
            native.pi.dwProcessId = self.process.pid
            native._check(native.kernel.AssignProcessToJobObject(native.job, native.pi.hProcess))
            native.assigned = True
            self.assert_single_worker()
            self._resume_initial_thread()
        except BaseException:
            self.close()
            raise
        return self.process

    def _resume_initial_thread(self):
        import ctypes as C
        from ctypes import wintypes as W

        class Entry(C.Structure):
            _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('tid', W.DWORD),
                        ('pid', W.DWORD), ('base', W.LONG), ('delta', W.LONG),
                        ('flags', W.DWORD)]

        kernel = self.native.kernel
        signatures = {'CreateToolhelp32Snapshot': (W.HANDLE, [W.DWORD, W.DWORD]),
                      'Thread32First': (W.BOOL, [W.HANDLE, C.POINTER(Entry)]),
                      'Thread32Next': (W.BOOL, [W.HANDLE, C.POINTER(Entry)]),
                      'OpenThread': (W.HANDLE, [W.DWORD, W.BOOL, W.DWORD])}
        for name, (result, arguments) in signatures.items():
            self.native._signature(kernel, name, result, arguments)
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == W.HANDLE(-1).value:
            raise C.WinError(C.get_last_error())
        threads = []
        try:
            entry = Entry()
            entry.size = C.sizeof(entry)
            more = kernel.Thread32First(snapshot, C.byref(entry))
            while more:
                if entry.pid == self.process.pid:
                    threads.append(entry.tid)
                more = kernel.Thread32Next(snapshot, C.byref(entry))
            if C.get_last_error() != 18:
                raise C.WinError(C.get_last_error())
        finally:
            kernel.CloseHandle(snapshot)
        if len(threads) != 1:
            raise GraderSessionError('Suspended worker does not have one initial thread')
        handle = self.native._check(kernel.OpenThread(2, False, threads[0]))
        try:
            if kernel.ResumeThread(handle) != 1:
                raise GraderSessionError('Owned initial thread had an unexpected suspend count')
        finally:
            kernel.CloseHandle(handle)

    def assert_single_worker(self):
        if (self.closed or self.native.active_processes() != 1
                or self.native.total_processes() != 1):
            raise GraderSessionError('The serial grader job is not exactly its original worker')

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.process is not None:
                if self.native.assigned:
                    self.native.terminate()
                    self.host._drain(self.native, CLEANUP_SECONDS)
                elif self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=CLEANUP_SECONDS)
        finally:
            if self.process is not None:
                for stream in (self.process.stdin, self.process.stdout):
                    if stream is not None:
                        stream.close()
                handle = getattr(self.process, '_handle', None)
                if handle is not None:
                    handle.Close()
            self.native.close()


def _read_responses(stream, responses):
    try:
        while line := stream.readline(MAX_FRAME_BYTES + 1):
            if len(line) > MAX_FRAME_BYTES:
                raise GraderSessionError('Worker protocol frame exceeds its bounded size')
            responses.put(json.loads(line))
    except BaseException as exc:
        responses.put(exc)
    finally:
        responses.put(GraderSessionError('The sealed grader worker exited'))


def _owned_home(work: Path) -> dict[str, str]:
    home = Path(work).absolute() / 'home'
    roaming, local = home / 'AppData/Roaming', home / 'AppData/Local'
    for path in (home, roaming, local):
        path.mkdir(parents=True, exist_ok=True)
    text = str(home)
    return {'USERPROFILE': text, 'HOME': text, 'HOMEDRIVE': home.drive,
            'HOMEPATH': text[len(home.drive):], 'APPDATA': str(roaming),
            'LOCALAPPDATA': str(local)}


def _environment(work: Path):
    system = os.environ.get('SystemRoot', 'C:/Windows')
    result = {'SystemRoot': system, 'WINDIR': system,
              'PATH': str(Path(system) / 'System32'), 'PYTHONDONTWRITEBYTECODE': '1'}
    result.update(_owned_home(work))
    for name, suffix in (('STL_TO_STEP_RUNS_DIR', 'runs'), ('MPLCONFIGDIR', 'mpl'),
                         ('TEMP', 'tmp'), ('TMP', 'tmp'), ('TMPDIR', 'tmp')):
        path = work / suffix
        path.mkdir(parents=True, exist_ok=True)
        result[name] = str(path)
    result.update({name: '1' for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                   'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS')})
    return result


class SealedGraderSession:
    """Explicitly bounded parent API; not integrated with the corpus controller.

    Construction does not start processes. start(deadline=...) and grade(...,
    deadline=...) both require original finite monotonic deadlines <=360s away.
    close() kills and drains the owned Windows Job; no worker children are allowed.
    """
    def __init__(self, worker: Path, worker_sha256: str, grader: Path, grader_sha256: str,
                 work: Path, *, bootstrap_session):
        self.worker, self.grader = Path(worker).absolute(), Path(grader).absolute()
        self.worker_pin, self.grader_pin = worker_sha256, grader_sha256
        self.work = Path(work).absolute()
        self.job = self.reader = self.stderr = None
        self.writer = None
        self.bootstrap_session = bootstrap_session
        self.responses = queue.Queue()
        self.started = self.closed = False
        self.busy = threading.Lock()
        self.code_identity = _digest(Path(__file__))
        self.helper_identities = {}

    def _code_check(self):
        if _digest(Path(__file__)) != self.code_identity:
            raise GraderSessionError('The parent sealed-grader code changed')
        if any(_digest(path) != expected for path, expected in self.helper_identities.items()):
            raise GraderSessionError('A parent proof/supervisor module changed')

    def _send(self, value, *, deadline):
        _remaining(deadline)
        data = (json.dumps(value, separators=(',', ':')) + '\n').encode()
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError('Request frame exceeds its bounded size')
        finished = queue.Queue()

        def write():
            try:
                view = memoryview(data)
                while view:
                    count = self.job.process.stdin.write(view)
                    if not count:
                        raise BrokenPipeError('Worker request pipe made no progress')
                    view = view[count:]
                self.job.process.stdin.flush()
                finished.put(None)
            except BaseException as exc:
                finished.put(exc)

        self.writer = threading.Thread(target=write, daemon=True)
        self.writer.start()
        try:
            error = finished.get(timeout=_remaining(deadline))
        except queue.Empty as exc:
            raise GraderRequestTimeout('The request deadline expired while sending') from exc
        if error:
            raise GraderSessionError('Cannot send the bounded worker request') from error

    def _response(self, deadline):
        try:
            value = self.responses.get(timeout=_remaining(deadline))
        except queue.Empty as exc:
            raise GraderRequestTimeout('The original grader request deadline expired') from exc
        if isinstance(value, BaseException):
            raise GraderSessionError('Worker protocol failed') from value
        self.job.assert_single_worker()
        _remaining(deadline)
        return value

    def start(self, *, deadline: float):
        from tools.locked_artifact_identity import artifact_catalogs
        from tools.sealed_runtime import SealedRuntimeSession as NativeSeal
        from tools.sealed_runtime import module_digest

        _remaining(deadline)
        if type(self.bootstrap_session) is not NativeSeal:
            raise TypeError('Startup requires the actual live bootstrap SealedRuntimeSession')
        if self.started or self.closed:
            raise GraderSessionError('A grader session cannot be restarted')
        self.started = True
        self._code_check()
        worker_metadata, worker_catalog = _worker_catalog(self.worker, self.worker_pin)
        metadata, grading_catalog = artifact_catalogs(
            self.grader, self.grader_pin, expected_kind='grader')
        if worker_metadata.get('parent_job_host_sha256') != PARENT_JOB_HOST_SHA256:
            raise GraderSessionError('Worker was frozen for another parent job supervisor')
        bootstrap_catalogs = _merge_catalogs(worker_catalog, grading_catalog)
        self.bootstrap_session.require_catalogs(bootstrap_catalogs)
        self.helper_identities = {Path(sys.modules[name].__file__): _digest(
            Path(sys.modules[name].__file__)) for name in (
                'tools.sealed_runtime', 'tools.locked_artifact_identity',
                'tools.freeze_evaluation_converter')}
        for root in (self.worker, self.grader, Path(metadata['python_runtime'])):
            if self.work.is_relative_to(root) or root.is_relative_to(self.work):
                raise ValueError('New session work must be separate from frozen artifacts/runtime')
        for ancestor in self.work.parents:
            if ancestor.exists() and getattr(ancestor.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('New session work cannot traverse a link or junction')
        self.work.mkdir(parents=True, exist_ok=False)
        configuration = self.work / 'configuration'
        configuration.mkdir()
        config_path = configuration / 'worker.json'
        config_path.write_text(json.dumps({'worker': str(self.worker),
            'worker_manifest_sha256': self.worker_pin, 'grader': str(self.grader),
            'grader_manifest_sha256': self.grader_pin,
            'runtime': metadata['python_runtime'], 'work': str(self.work.resolve())}) + '\n')
        config_pin = _digest(config_path)
        try:
            # Parent locks protect the interpreter/bootstrap before the worker's
            # own live seal is established. No serialized skip token is used.
            with NativeSeal({configuration: {'worker.json': config_pin}},
                            module_sha256=module_digest()):
                _remaining(deadline)
                self.stderr = (self.work / 'worker.stderr.log').open('wb')
                self.job = _WindowsJob()
                self.helper_identities[Path(self.job.host.__file__)] = PARENT_JOB_HOST_SHA256
                process = self.job.launch([
                    str(Path(metadata['python_runtime']) / 'python.exe'), '-I', '-B',
                    str(self.worker / 'application/sealed_grader_session.pyc'),
                    '--worker', str(config_path), config_pin,
                ], cwd=self.work, env=_environment(self.work / 'bootstrap'), stderr=self.stderr)
                self.reader = threading.Thread(target=_read_responses,
                                               args=(process.stdout, self.responses), daemon=True)
                self.reader.start()
                self._send({'operation': 'start'}, deadline=deadline)
                ready = self._response(deadline)
                if (ready.get('status') != 'ready' or ready.get('worker_manifest_sha256') != (
                        self.worker_pin) or ready.get('grader_manifest_sha256') != self.grader_pin):
                    raise GraderSessionError('Worker did not establish the pinned native seal')
                self.bootstrap_session.require_catalogs(bootstrap_catalogs)
            self._code_check()
            return self
        except BaseException:
            self.close()
            raise

    def grade(self, case_json: Path, retained: Path, destination: Path, *, deadline: float,
              artifacts: Path | None = None, reference_readiness_only=False):
        _remaining(deadline)
        if not self.started or self.closed or self.job is None:
            raise GraderSessionError('The sealed grader session is not live')
        if not self.busy.acquire(blocking=False):
            raise GraderSessionError('Only one serial grading request is allowed')
        request = uuid.uuid4().hex
        evidence = self.work / ('request-' + request)
        try:
            self._code_check()
            arguments = ([str(Path(case_json).absolute()), str(Path(retained).absolute()),
                          str(Path(destination).absolute())]
                         if not reference_readiness_only
                         else ['--reference-readiness', str(Path(case_json).absolute()),
                               str(Path(destination).absolute())])
            if artifacts is not None:
                if reference_readiness_only:
                    raise ValueError('Reference readiness has no failure artifact argument')
                arguments.append(str(Path(artifacts).absolute()))
            self._send({'operation': 'grade', 'request': request, 'arguments': arguments,
                        'deadline': deadline, 'evidence': str(evidence)}, deadline=deadline)
            response = self._response(deadline)
            if response.get('request') != request or response.get('status') != 'complete':
                raise GraderSessionError('Frozen grading failed; request evidence is preserved')
            if (_digest(destination) != response.get('result_sha256')
                    or response.get('entry_sha256') != json.loads(
                        (self.grader / 'manifest.json').read_text())[
                            'application_files']['grader.pyc']):
                raise GraderSessionError('The grader result or ENTRY identity differs')
            result = json.loads(Path(destination).read_text())
            if (result.get('grader_manifest_sha256') != self.grader_pin
                    or result.get('second_conversion_not_run') is not True):
                raise GraderSessionError('The original grading evidence is incomplete')
            self._code_check()
            _remaining(deadline)
            return result, response
        except BaseException as exc:
            try:
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / 'parent-failure.json').write_text(json.dumps({
                    'qualified': False, 'error': f'{type(exc).__name__}: {exc}'}, indent=2) + '\n')
            except BaseException:
                # Disk or allocation failure must neither hide the original
                # request failure nor prevent the owned worker being drained.
                pass
            finally:
                self.close()
            raise
        finally:
            self.busy.release()

    def close(self):
        if self.closed:
            return
        self.closed = True
        cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        try:
            if self.job:
                self.job.close()
        finally:
            if self.reader:
                self.reader.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
            if self.writer:
                self.writer.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
            if self.stderr:
                self.stderr.close()

    def __enter__(self):
        if not self.started or self.closed:
            raise TypeError('Call start(deadline=...) explicitly before using the session')
        return self

    def __exit__(self, *unused):
        self.close()


def _worker_main(configuration: Path, configuration_pin: str):
    # The native job was assigned before interpreter resume. This gate precedes
    # artifact-specific imports and receipt of any case path or geometry request.
    if json.loads(sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)) != {'operation': 'start'}:
        raise GraderSessionError('The parent did not assign and start the trusted worker')
    if _digest(configuration) != configuration_pin:
        raise GraderSessionError('Worker configuration differs from its pinned identity')
    config = json.loads(configuration.read_text())
    worker, grader, runtime, work = map(Path, (
        config['worker'], config['grader'], config['runtime'], config['work']))
    if Path(__file__).resolve() != worker / 'application/sealed_grader_session.pyc':
        raise GraderSessionError('Worker bootstrap did not load its frozen entrypoint')
    sys.path[:0] = [str(worker / 'application'), str(grader / 'application'),
                    str(runtime / 'Lib/site-packages')]
    from tools.locked_artifact_identity import artifact_catalogs
    from tools.sealed_runtime import SealedRuntimeSession as NativeSeal

    worker_metadata, worker_catalog = _worker_catalog(worker, config['worker_manifest_sha256'])
    metadata, grader_catalog = artifact_catalogs(
        grader, config['grader_manifest_sha256'], expected_kind='grader')
    if Path(metadata['python_runtime']) != runtime:
        raise GraderSessionError('The grader runtime identity differs from the launcher')
    catalogs = _merge_catalogs(worker_catalog, grader_catalog,
                               {configuration.parent: {configuration.name: configuration_pin}})
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8', buffering=1)
    native_log = (work / 'worker.stdout.log').open('ab', buffering=0)
    os.dup2(native_log.fileno(), sys.stdout.fileno())
    if sys.platform == 'win32':
        import ctypes as C
        import msvcrt

        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.SetStdHandle.argtypes = [C.c_ulong, C.c_void_p]
        kernel.SetStdHandle(-11 & 0xffffffff, msvcrt.get_osfhandle(sys.stdout.fileno()))
    seal_pin = worker_metadata['application_files']['tools/sealed_runtime.pyc']
    try:
        with NativeSeal(catalogs, module_sha256=seal_pin) as active:
            executor = _FrozenEntryExecutor(
                grader, config['grader_manifest_sha256'], worker, active, work / 'bootstrap')
            protocol.write(json.dumps({'status': 'ready',
                'worker_manifest_sha256': config['worker_manifest_sha256'],
                'grader_manifest_sha256': config['grader_manifest_sha256']}) + '\n')
            while line := sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1):
                if len(line) > MAX_FRAME_BYTES:
                    raise GraderSessionError('Request frame exceeds bounded size')
                request = json.loads(line)
                if request.get('operation') != 'grade':
                    raise GraderSessionError('Unknown worker operation')
                try:
                    evidence = Path(request['evidence'])
                    if evidence.parent.resolve() != work.resolve() or not evidence.name.startswith(
                            'request-'):
                        raise GraderSessionError('Request evidence escaped its owned session')
                    result = executor.run(request['arguments'], deadline=request['deadline'],
                                          evidence=evidence)
                    protocol.write(json.dumps({'status': 'complete',
                                               'request': request['request'], **result}) + '\n')
                except BaseException as exc:
                    protocol.write(json.dumps({'status': 'error', 'request': request.get('request'),
                                               'error': f'{type(exc).__name__}: {exc}'}) + '\n')
                    raise
    finally:
        protocol.close()
        native_log.close()


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--worker':
        raise ValueError('Use the explicitly bounded SealedGraderSession API')
    _worker_main(Path(sys.argv[2]), sys.argv[3])

