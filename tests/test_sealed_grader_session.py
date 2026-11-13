import importlib.util
import json
import queue
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import freeze_evaluation_converter as frozen
from tools import freeze_evaluation_grader
from tools import sealed_grader_session as candidate

WINDOWS = pytest.mark.skipif(sys.platform != 'win32', reason='Requires native Windows locks/jobs')

FAKE_GRADER = b'''import os
from pathlib import Path
import tempfile
from app import storage
from tools import benchmark_surface_corpus as benchmark
COUNT = 0
def reconstruct_in_worker(*args, **kwargs):
    raise AssertionError('A second conversion cannot be run')
def evaluate(case, manifest, artifacts):
    global COUNT
    COUNT += 1
    with tempfile.TemporaryDirectory(prefix='protocol-case-') as value:
        stl = Path(value) / 'input.stl'
        stl.write_bytes((manifest.parent / case['stl']).read_bytes())
        report = reconstruct_in_worker('case', stl, 'input.stl', 'mm', engine='surface_brep')
        assert storage.RUNS == Path(os.environ['STL_TO_STEP_RUNS_DIR'])
        assert storage.RUNS.is_dir()
        home = Path.home()
        assert home.is_relative_to(storage.RUNS.parent)
        assert Path('~').expanduser() == home
        assert Path(os.environ['HOME']) == home
        assert Path(os.environ['APPDATA']).is_relative_to(home)
        assert Path(os.environ['LOCALAPPDATA']).is_relative_to(home)
        assert Path(os.environ['HOMEDRIVE'] + os.environ['HOMEPATH']) == home
        if case.get('leak'):
            benchmark.reconstruct_surfaces = lambda: 'leaked'
        if case.get('mutate'):
            Path(case['mutate']).write_bytes(b'changed after copying the actual output')
        if case.get('raise'):
            raise RuntimeError('real protocol grading failure')
        return {'accepted': report.accepted, 'status': report.status,
                'retained_report_value': report.value, 'module_count': COUNT,
                'case_home': str(home), 'case_appdata': os.environ['APPDATA'],
                'case_runs': str(storage.RUNS), 'conversion_wall_seconds': .01,
                'verification_wall_seconds': .02}
'''
SCHEMAS = b'''import json
from types import SimpleNamespace
class ReconstructionReport:
    @classmethod
    def model_validate_json(cls, value):
        return SimpleNamespace(**json.loads(value))
'''
STEP = b'''from types import SimpleNamespace
def load_step_shape(path):
    return SimpleNamespace(ready=not path.read_bytes().startswith(b'invalid'),
                           BoundingBox=lambda: SimpleNamespace(xlen=1.,ylen=1.,zlen=1.))
'''
REFERENCE = b'''from types import SimpleNamespace
class ReferenceFixtureError(ValueError):
    def __init__(self): self.diagnostics={'fixture_ready':False,'reason':'protocol_bad_fixture'}
def validated_reference_mesh(shape, *args, **kwargs):
    if not shape.ready: raise ReferenceFixtureError()
    return SimpleNamespace(diagnostics={'fixture_ready': True})
'''


def artifacts(tmp_path, monkeypatch):
    repo = Path(candidate.__file__).resolve().parents[1]
    sources = {name: (repo / name).read_bytes() for name in candidate.WORKER_SOURCES}
    sources['LICENSE'] = b'protocol worker license'
    monkeypatch.setattr(candidate, '_committed_sources', lambda *_: ('protocol-commit', sources))
    worker = tmp_path / 'worker'
    worker_metadata = candidate.freeze_worker(repo, 'commit', worker)
    worker_pin = frozen.digest(worker / 'manifest.json')
    runtime = tmp_path / 'tiny-runtime-catalog'
    runtime.mkdir()
    (runtime / 'protocol.bin').write_bytes(b'only a native locking fixture; never an interpreter')
    grader = tmp_path / 'grader'
    app = grader / 'application'
    source = {
        'app/__init__.py': b'', 'app/schemas.py': SCHEMAS, 'app/step.py': STEP,
        'app/storage.py': (b'import os\nfrom pathlib import Path\n'
                           b'RUNS=Path(os.environ["STL_TO_STEP_RUNS_DIR"])\n'
                           b'RUNS.mkdir(parents=True,exist_ok=True)\n'),
        'tools/benchmark_multires_corpus.py': FAKE_GRADER,
        'tools/benchmark_surface_corpus.py': (
            b'def reconstruct_surfaces(): pass\ndef step_surface_metrics(): pass\n'),
        'tools/reference_mesh.py': REFERENCE,
        'tools/freeze_evaluation_converter.py': Path(frozen.__file__).read_bytes(),
    }
    for name, data in source.items():
        frozen.compile_module(data, name, app / Path(name).with_suffix('.pyc'))
    frozen.compile_module(freeze_evaluation_grader.ENTRY.encode(), 'grader.py', app / 'grader.pyc')
    (grader / 'LICENSE').write_bytes(b'protocol grader license')
    dependencies = grader / 'dependencies'
    dependencies.mkdir()
    (dependencies / 'pyproject.toml').write_bytes(b'[project]\nname="protocol"\n')
    (dependencies / 'uv.lock').write_bytes(b'version=1\n')
    metadata = {'schema': 1, 'artifact_kind': 'grader', 'source_revision': 'protocol-grader',
                'entrypoint': 'application/grader.pyc',
                'entry_source_sha256': candidate.ENTRY_SOURCE_SHA256,
                'python_magic': importlib.util.MAGIC_NUMBER.hex(),
                'python_runtime': str(runtime), 'runtime_files': frozen.inventory(runtime),
                'application_files': frozen.inventory(app),
                'dependency_files': frozen.inventory(dependencies),
                'license_sha256': frozen.digest(grader / 'LICENSE')}
    (grader / 'manifest.json').write_text(json.dumps(metadata))
    pin = frozen.digest(grader / 'manifest.json')
    fixtures = tmp_path / 'fixtures'
    fixtures.mkdir()
    (fixtures / 'input.stl').write_bytes(b'protocol STL')
    (fixtures / 'original.step').write_bytes(b'protocol reference, never actual CAD')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    configurations = []
    for index, accepted in enumerate((True, False)):
        retained = tmp_path / f'retained-{index}'
        retained.mkdir()
        (retained / 'reconstruction.step').write_bytes(b'actual retained protocol STEP')
        (retained / 'report.json').write_text(json.dumps({
            'status': 'complete' if accepted else 'best_effort', 'accepted': accepted,
            'value': 73 + index}))
        case = {'id': f'source-q0{index}', 'stl': 'input.stl', 'source_step': 'original.step',
                'source_model_id': 'source', 'split': 'development', 'preset': f'q0{index}',
                'input_sha256': frozen.digest(fixtures / 'input.stl'),
                'source_sha256': frozen.digest(fixtures / 'original.step')}
        config = {'case': case, 'manifest_parent': str(fixtures), 'scratch_root': str(scratch),
                  'grader_manifest_sha256': pin,
                  'retained_sha256': {p.name: frozen.digest(p) for p in retained.iterdir()}}
        path = tmp_path / f'case-{index}.json'
        path.write_text(json.dumps(config))
        configurations.append({'case': str(path), 'retained': str(retained),
                               'result': str(tmp_path / f'result-{index}.json'),
                               'evidence': str(tmp_path / f'evidence-{index}')})
    return {'worker': str(worker), 'worker_pin': worker_pin, 'grader': str(grader),
            'grader_pin': pin, 'worker_metadata': worker_metadata, 'cases': configurations,
            'scratch': str(scratch), 'output': str(tmp_path / 'observations.json')}


CHILD = r'''import json,os,sys,tempfile,time,types
from pathlib import Path
config=json.loads(Path(sys.argv[1]).read_text())
worker,grader=Path(config['worker']),Path(config['grader'])
sys.path[:0]=[str(worker/'application'),str(grader/'application')]
import sealed_grader_session as candidate
from tools.locked_artifact_identity import artifact_catalogs
from tools.sealed_runtime import SealedRuntimeSession
metadata,grade_catalog=artifact_catalogs(grader,config['grader_pin'],expected_kind='grader')
wm,worker_catalog=candidate._worker_catalog(worker,config['worker_pin'])
catalogs=candidate._merge_catalogs(grade_catalog,worker_catalog)
rows=[]
seal=SealedRuntimeSession(catalogs,module_sha256=wm['application_files']['tools/sealed_runtime.pyc'])
with seal as active:
 executor=candidate._FrozenEntryExecutor(grader,config['grader_pin'],worker,active,Path(config['output']).parent/'boot')
 original_env=dict(os.environ);original_temp=tempfile.tempdir;original_runs=executor.storage.RUNS
 for index,item in enumerate(config['cases']):
  casepath=Path(item['case'])
  mode=config['mode']
  if index==0 and mode!='normal':
   data=json.loads(casepath.read_text())
   if mode=='leak': data['case']['leak']=True
   elif mode=='tamper': data['case']['mutate']=str(Path(item['retained'])/'reconstruction.step')
   elif mode=='fixture_error':
    bad=Path(data['manifest_parent'])/'invalid.step';bad.write_bytes(b'invalid reference fixture')
    from tools.freeze_evaluation_converter import digest
    data['case']['source_step']=str(bad);data['case']['source_sha256']=digest(bad)
   elif mode=='readiness':
    data['case']['stl']='missing-input.stl';data['case']['input_sha256']='0'*64
   casepath.write_text(json.dumps(data))
  try:
   if index==0 and mode=='origin':
    external=types.ModuleType('app.external');external.__file__=str(casepath.parent/'outside.py')
    sys.modules['app.external']=external
   args=[item['case'],item['retained'],item['result']]
   if index==0 and mode=='readiness':args=['--reference-readiness',item['case'],item['result']]
   trace=executor.run(args,deadline=time.monotonic()+30,evidence=Path(item['evidence']))
   rows.append({'trace':trace,'result':json.loads(Path(item['result']).read_text())})
  except candidate.GraderSessionError as exc:
   rows.append({'error':str(exc),'failure_preserved':(Path(item['evidence'])/'failure.json').exists()})
  finally:
   sys.modules.pop('app.external',None)
  assert dict(os.environ)==original_env
  assert tempfile.tempdir==original_temp
  assert executor.storage.RUNS==original_runs
 after=(executor.evaluator.reconstruct_in_worker,executor.benchmark.reconstruct_surfaces,
        executor.benchmark.step_surface_metrics)
 assert after==executor.baseline
 assert not list(Path(config['scratch']).glob('grader-work-*'))
 active.check()
Path(config['output']).write_text(json.dumps(rows))
'''


@WINDOWS
@pytest.mark.parametrize('mode', ['normal', 'leak', 'tamper', 'fixture_error',
                                  'readiness', 'origin'])
def test_exact_frozen_entry_reused_with_native_seal_and_independent_case_state(
    tmp_path, monkeypatch, mode,
):
    config = artifacts(tmp_path, monkeypatch)
    config['mode'] = mode
    path = tmp_path / 'protocol.json'
    path.write_text(json.dumps(config))
    result = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', CHILD, str(path)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    rows = json.loads(Path(config['output']).read_text())
    if mode in ('leak', 'tamper'):
        assert rows[0]['error'] and rows[0]['failure_preserved']
        if mode == 'tamper':
            assert not Path(config['cases'][0]['result']).exists()
    elif mode == 'origin':
        assert 'Unexpected grading module origin' in rows[0]['error']
        assert not Path(config['cases'][0]['result']).exists()
    elif mode == 'fixture_error':
        assert rows[0]['result']['status'] == 'fixture_error'
        assert rows[0]['result']['accepted'] is False
        assert rows[0]['result']['evaluation_failure_kind'] == 'fixture'
    elif mode == 'readiness':
        assert rows[0]['result']['status'] == 'reference_ready'
        assert rows[0]['result']['accepted'] is False
        assert rows[0]['result']['reference_readiness_only'] is True
    else:
        assert rows[0]['result']['accepted'] is True
        assert rows[0]['result']['retained_report_value'] == 73
        assert rows[0]['result']['module_count'] == 1
        assert rows[0]['result']['case_home'] != rows[1]['result']['case_home']
        assert rows[0]['trace']['temporary_atexit_registrations_removed'] == 1
    assert rows[1]['result']['accepted'] is False
    assert rows[1]['result']['retained_report_value'] == 74
    assert rows[1]['result']['module_count'] == (
        1 if mode in ('fixture_error', 'readiness', 'origin') else 2)
    assert rows[1]['trace']['second_conversion_not_run']
    assert 'conversion_wall_seconds' not in rows[1]['result']
    assert rows[1]['result']['artifact_copy_wall_seconds'] == .01


def test_worker_freeze_preserves_existing_output_and_pins_only_small_code(tmp_path, monkeypatch):
    config = artifacts(tmp_path, monkeypatch)
    worker = Path(config['worker'])
    assert len(config['worker_metadata']['application_files']) == 3
    assert not list(worker.rglob('*.py'))
    assert not list(worker.rglob('python.exe'))
    assert config['worker_metadata']['workflow_qualified'] is False
    before = (worker / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='Preserve existing'):
        candidate.freeze_worker(tmp_path, 'commit', worker)
    assert (worker / 'manifest.json').read_bytes() == before


@WINDOWS
def test_unqualified_entry_version_is_refused_before_any_request(tmp_path, monkeypatch):
    config = artifacts(tmp_path, monkeypatch)
    path = Path(config['grader']) / 'manifest.json'
    metadata = json.loads(path.read_text())
    metadata['entry_source_sha256'] = '0' * 64
    path.write_text(json.dumps(metadata))
    config.update(grader_pin=frozen.digest(path), mode='normal')
    configuration = tmp_path / 'protocol.json'
    configuration.write_text(json.dumps(config))
    result = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', CHILD, str(configuration)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0 and 'ENTRY version has not qualified' in result.stderr
    assert not any(Path(item['result']).exists() for item in config['cases'])


@pytest.mark.parametrize('fake', [None, False, True, 'serialized-seal', {}])
def test_startup_requires_an_actual_live_bootstrap_object(tmp_path, fake):
    session = candidate.SealedGraderSession(
        tmp_path, 'w', tmp_path, 'g', tmp_path / 'work', bootstrap_session=fake)
    with pytest.raises(TypeError, match='actual live bootstrap'):
        session.start(deadline=time.monotonic() + 1)
    assert not (tmp_path / 'work').exists()


@WINDOWS
def test_native_job_kills_and_drains_only_the_gated_worker(tmp_path):
    import ctypes as C
    from ctypes import wintypes as W

    class JobPids(C.Structure):
        _fields_ = [('assigned', W.DWORD), ('listed', W.DWORD), ('pids', C.c_size_t * 4)]

    job = candidate._WindowsJob()
    reader = None
    with (tmp_path / 'stderr.log').open('wb') as stderr:
        try:
            process = job.launch([
                str(Path(sys.base_prefix) / 'python.exe'), '-I', '-B', '-S', '-c',
                'import sys,time,json;value=sys.stdin.readline().strip();'
                'print(json.dumps({"handshake":value}),flush=True);time.sleep(30)',
            ], cwd=tmp_path, env=candidate._environment(tmp_path / 'work'), stderr=stderr)
            responses = queue.Queue()
            reader = __import__('threading').Thread(
                target=candidate._read_responses, args=(process.stdout, responses), daemon=True)
            reader.start()
            process.stdin.write(b'start\n')
            process.stdin.flush()
            assert responses.get(timeout=3) == {'handshake': 'start'}
            # Console helpers can appear after launch; inspect the live job
            # after interpreter startup, not only while its thread is suspended.
            for _ in range(4):
                time.sleep(.05)
                job.assert_single_worker()
                members = JobPids()
                job.native._check(job.native.kernel.QueryInformationJobObject(
                    job.native.job, 3, C.byref(members), C.sizeof(members), None))
                assert members.assigned == members.listed == 1
                assert members.pids[0] == process.pid
        finally:
            job.close()
            if reader:
                reader.join(timeout=2)
        assert process.poll() is not None


@WINDOWS
def test_native_job_forbids_even_a_trusted_worker_from_spawning_children(tmp_path):
    job = candidate._WindowsJob()
    marker = tmp_path / 'child-executed.txt'
    code = '''import sys,subprocess,json,time
sys.stdin.readline()
try:
 subprocess.Popen([sys.executable,'-I','-B','-S','-c',
                   'from pathlib import Path;import sys;Path(sys.argv[1]).write_text("executed")',
                   sys.argv[1]])
 print(json.dumps({'child_forbidden':False}),flush=True)
except OSError as error:
 print(json.dumps({'child_forbidden':True,'winerror':error.winerror}),flush=True)
time.sleep(30)
'''
    with (tmp_path / 'stderr.log').open('wb') as stderr:
        try:
            process = job.launch([str(Path(sys.base_prefix) / 'python.exe'), '-I', '-B', '-S',
                                  '-c', code, str(marker)], cwd=tmp_path,
                                 env=candidate._environment(tmp_path / 'work'), stderr=stderr)
            responses = queue.Queue()
            reader = __import__('threading').Thread(
                target=candidate._read_responses, args=(process.stdout, responses), daemon=True)
            reader.start()
            process.stdin.write(b'start\n')
            process.stdin.flush()
            assert responses.get(timeout=3) == {'child_forbidden': True, 'winerror': 1816}
            assert not marker.exists()
            for _ in range(4):
                time.sleep(.05)
                job.assert_single_worker()
        finally:
            job.close()
        assert process.poll() is not None


@pytest.mark.parametrize('closed,active,total', [
    (True, 1, 1), (False, 0, 1), (False, 1, 2), (False, 2, 2),
])
def test_serial_job_guard_rejects_closed_or_mismatched_accounting(closed, active, total):
    job = candidate._WindowsJob.__new__(candidate._WindowsJob)
    job.closed = closed
    job.native = SimpleNamespace(active_processes=lambda: active, total_processes=lambda: total)
    with pytest.raises(candidate.GraderSessionError, match='not exactly its original worker'):
        job.assert_single_worker()


@WINDOWS
def test_public_deadline_timeout_kills_and_drains_native_job(tmp_path):
    session = candidate.SealedGraderSession(
        tmp_path, 'w', tmp_path, 'g', tmp_path / 'work', bootstrap_session=None)
    session.work.mkdir()
    session.started, session.job = True, candidate._WindowsJob()
    with (tmp_path / 'stderr.log').open('wb') as stderr:
        process = session.job.launch([
            str(Path(sys.base_prefix) / 'python.exe'), '-I', '-B', '-S', '-c',
            'import sys,time;sys.stdin.readline();time.sleep(30)',
        ], cwd=tmp_path, env=candidate._environment(tmp_path / 'bootstrap'), stderr=stderr)
        with pytest.raises(candidate.GraderRequestTimeout):
            session.grade(tmp_path / 'case.json', tmp_path / 'retained', tmp_path / 'result.json',
                          deadline=time.monotonic() + .03)
        assert session.closed and process.poll() is not None
        assert list(session.work.glob('request-*/parent-failure.json'))


def test_public_request_timeout_closes_its_job_and_preserves_failure(tmp_path):
    class Job:
        closed = False
        process = SimpleNamespace(stdin=SimpleNamespace(write=lambda x: len(x), flush=lambda: None))

        def close(self):
            self.closed = True

    session = candidate.SealedGraderSession(
        tmp_path, 'w', tmp_path, 'g', tmp_path / 'work', bootstrap_session=None)
    session.started, session.job = True, Job()
    with pytest.raises(candidate.GraderRequestTimeout):
        session.grade(tmp_path / 'case.json', tmp_path / 'retained', tmp_path / 'result.json',
                      deadline=time.monotonic() + .02)
    assert session.closed and session.job.closed
    assert list((tmp_path / 'work').glob('request-*/parent-failure.json'))


@pytest.mark.parametrize('operation', ['mkdir', 'write_text'])
@pytest.mark.parametrize('failure', [MemoryError('synthetic evidence allocation failure'),
                                   OSError(28, 'synthetic full evidence disk'),
                                   PermissionError('synthetic denied evidence write')])
@pytest.mark.parametrize('original', [candidate.GraderRequestTimeout('synthetic request deadline'),
                                    MemoryError('synthetic original request allocation failure')])
def test_failed_failure_evidence_still_drains_and_preserves_original(
    tmp_path, monkeypatch, operation, failure, original,
):
    class Job:
        closed = False

        def close(self):
            self.closed = True

    session = candidate.SealedGraderSession(
        tmp_path, 'w', tmp_path, 'g', tmp_path / 'work', bootstrap_session=None)
    session.started, session.job = True, Job()

    def send(*args, **kwargs):
        raise original

    def evidence_failure(*args, **kwargs):
        raise failure

    monkeypatch.setattr(session, '_send', send)
    monkeypatch.setattr(Path, operation, evidence_failure)
    with pytest.raises(type(original)) as caught:
        session.grade(tmp_path / 'case.json', tmp_path / 'retained', tmp_path / 'result.json',
                      deadline=time.monotonic() + 1)
    assert caught.value is original
    assert session.closed and session.job.closed
    assert session.busy.acquire(blocking=False)
    session.busy.release()


@pytest.mark.parametrize('deadline', [True, float('inf'), float('nan'), 'later'])
def test_public_deadlines_cannot_be_omitted_or_unbounded(deadline):
    with pytest.raises(ValueError):
        candidate._remaining(deadline)


def test_deadlines_never_exceed_original_budget_or_revive_after_expiry():
    with pytest.raises(ValueError, match='360'):
        candidate._remaining(time.monotonic() + 361)
    with pytest.raises(candidate.GraderRequestTimeout):
        candidate._remaining(time.monotonic() - 1)


def test_response_reader_refuses_oversized_or_invalid_frames():
    import io

    for raw in (b'x' * (candidate.MAX_FRAME_BYTES + 1), b'not-json\n'):
        responses = queue.Queue()
        candidate._read_responses(io.BytesIO(raw), responses)
        assert isinstance(responses.get_nowait(), BaseException)


def test_process_environment_uses_only_owned_home_and_restores_parent(tmp_path, monkeypatch):
    import os

    actual_private = tmp_path / 'must-not-read-private-home'
    monkeypatch.setenv('USERPROFILE', str(actual_private))
    monkeypatch.setenv('HOME', str(actual_private))
    before = dict(os.environ)
    work = tmp_path / 'owned-environment'
    environment = candidate._environment(work)
    home = work / 'home'
    assert environment['USERPROFILE'] == environment['HOME'] == str(home)
    assert Path(environment['HOMEDRIVE'] + environment['HOMEPATH']) == home
    assert Path(environment['APPDATA']).is_relative_to(home)
    assert Path(environment['LOCALAPPDATA']).is_relative_to(home)
    assert all(Path(environment[name]).is_dir() for name in (
        'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'))
    assert dict(os.environ) == before
    assert not actual_private.exists()


@WINDOWS
def test_ezdxf_import_resolves_neutral_home_without_actual_user_configuration(tmp_path):
    environment = candidate._environment(tmp_path / 'owned')
    command = [sys.executable, '-I', '-B', '-c',
               'import ezdxf;from pathlib import Path;print(Path("~").expanduser())']
    result = subprocess.run(command, env=environment, capture_output=True, text=True,
                            timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == tmp_path / 'owned/home'
