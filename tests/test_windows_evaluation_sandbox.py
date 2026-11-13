from contextlib import contextmanager
from pathlib import Path

import pytest

from tools import windows_evaluation_sandbox as sandbox


class ProtocolBackend:
    """Adversarial process events, not a substitute for native qualification."""

    affinity_mask = 1

    def __init__(self, events=(), *, rejected_pid=None, total=2, exit_code=0):
        self.events = list(events)
        self.rejected_pid = rejected_pid
        self.total = total
        self.code = exit_code
        self.calls = []
        self.active = 2

    @contextmanager
    def lock_input(self, path):
        self.calls.append(("lock", path.name))
        try:
            yield
        finally:
            self.calls.append(("unlock", path.name))

    def launch_suspended(self, argv, cwd, env):
        self.calls.append(("launch", argv, cwd, env))
        return 10

    def audit(self, pid):
        self.calls.append(("audit", pid))
        if pid == self.rejected_pid:
            raise sandbox.IsolationError("Host observed wrong SID/job/capabilities")
        return sandbox.ProcessAudit(pid, "S-1-15-2-task", True, 0)

    def resume(self):
        self.calls.append(("resume",))

    def next_event(self, timeout):
        self.calls.append(("wait",))
        if not self.events:
            return None
        event = self.events.pop(0)
        if event[0] == sandbox.ACTIVE_PROCESS_ZERO:
            self.active = 0
        return event

    def active_processes(self):
        return self.active

    def total_processes(self):
        return self.total

    def exit_code(self):
        return self.code

    def wait_processes(self, timeout):
        self.calls.append(("wait_signals",))

    def terminate(self):
        self.calls.append(("terminate",))
        self.events = [(sandbox.ACTIVE_PROCESS_ZERO, 0)]

    def close(self):
        self.calls.append(("close", self.active))


def invoke(tmp_path, backend):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    interpreter = runtime / "python.exe"
    interpreter.write_bytes(b"fake interpreter; no native conversion in protocol test")
    entry = runtime / "converter.pyc"
    entry.write_bytes(b"fake bytecode")
    job = tmp_path / "job"
    job.mkdir()
    (job / "input.stl").write_bytes(b"input remains private")
    return sandbox.run_converter(
        interpreter,
        entry,
        job,
        "mm",
        owned_root=tmp_path,
        profile="existing-task-profile",
        _backend=backend,
    )


def test_audits_before_resume_and_waits_for_entire_job_with_input_locked(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "never inherit")
    backend = ProtocolBackend(
        [
            (sandbox.NEW_PROCESS, 10),
            (sandbox.NEW_PROCESS, 11),
            (sandbox.EXIT_PROCESS, 11),
            (sandbox.ACTIVE_PROCESS_ZERO, 0),
        ]
    )
    result = invoke(tmp_path, backend)
    assert result.job_drained and [audit.pid for audit in result.process_audits] == [10, 11]
    assert backend.calls.index(("audit", 10)) < backend.calls.index(("resume",))
    assert backend.calls[-2:] == [("close", 0), ("unlock", "input.stl")]
    assert ("wait_signals",) in backend.calls
    launch = next(call for call in backend.calls if call[0] == "launch")
    assert launch[1][1:3] == ["-I", "-B"]
    assert not any("SECRET_TOKEN" in key for key in launch[3])
    assert all(
        launch[3][key] == "1"
        for key in (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMBA_NUM_THREADS",
        )
    )


@pytest.mark.parametrize("pid", [10, 11])
def test_wrong_root_or_child_token_fails_and_drains_before_unlock(tmp_path, pid):
    backend = ProtocolBackend([(sandbox.NEW_PROCESS, 11)], rejected_pid=pid)
    with pytest.raises(sandbox.IsolationError, match="wrong SID"):
        invoke(tmp_path, backend)
    assert ("terminate",) in backend.calls
    assert backend.calls[-2:] == [("close", 0), ("unlock", "input.stl")]
    if pid == 10:
        assert ("resume",) not in backend.calls


def test_root_exit_waits_for_children_and_job_zero_before_publishing(tmp_path):
    backend = ProtocolBackend(
        [
            (sandbox.NEW_PROCESS, 11),
            (sandbox.EXIT_PROCESS, 10),
            (sandbox.EXIT_PROCESS, 11),
            (sandbox.ACTIVE_PROCESS_ZERO, 0),
        ]
    )
    assert invoke(tmp_path, backend).job_drained
    assert ("terminate",) not in backend.calls
    assert backend.calls[-2:] == [("close", 0), ("unlock", "input.stl")]


def test_orphan_child_still_running_at_deadline_is_killed_and_drained(tmp_path):
    backend = ProtocolBackend([(sandbox.NEW_PROCESS, 11), (sandbox.EXIT_PROCESS, 10)])
    with pytest.raises(TimeoutError):
        invoke(tmp_path, backend)
    assert ("terminate",) in backend.calls
    assert backend.calls[-2:] == [("close", 0), ("unlock", "input.stl")]


def test_timeout_kills_and_drains_entire_job_before_unlock(tmp_path):
    backend = ProtocolBackend()
    with pytest.raises(TimeoutError):
        invoke(tmp_path, backend)
    assert ("terminate",) in backend.calls
    assert backend.calls[-2:] == [("close", 0), ("unlock", "input.stl")]


def test_missing_new_process_notification_is_not_silently_qualified(tmp_path):
    backend = ProtocolBackend([(sandbox.ACTIVE_PROCESS_ZERO, 0)])
    with pytest.raises(sandbox.IsolationError, match="every job process"):
        invoke(tmp_path, backend)


def test_failure_to_drain_is_an_isolation_failure_and_never_a_result(tmp_path, monkeypatch):
    backend = ProtocolBackend()
    backend.terminate = lambda: backend.calls.append(("terminate",))
    times = iter([0, 2, 3, 20])
    monkeypatch.setattr(sandbox.time, "monotonic", lambda: next(times))
    with pytest.raises(sandbox.IsolationError, match="did not drain"):
        invoke(tmp_path, backend)
    assert backend.calls[-2:] == [("close", 2), ("unlock", "input.stl")]


def test_preserves_nonzero_converter_exit_after_qualified_cleanup(tmp_path):
    backend = ProtocolBackend(
        [(sandbox.NEW_PROCESS, 11), (sandbox.ACTIVE_PROCESS_ZERO, 0)], exit_code=17
    )
    assert invoke(tmp_path, backend).exit_code == 17


def test_job_accounting_zero_does_not_replace_waiting_for_process_handles(tmp_path):
    backend = ProtocolBackend([(sandbox.NEW_PROCESS, 11), (sandbox.ACTIVE_PROCESS_ZERO, 0)])

    def unsignaled(timeout):
        backend.calls.append(("wait_signals",))
        raise sandbox.IsolationError("Known job process did not finish termination")

    backend.wait_processes = unsignaled
    with pytest.raises(sandbox.IsolationError, match="did not finish termination"):
        invoke(tmp_path, backend)
    assert ("terminate",) in backend.calls
    assert backend.calls[-2:] == [("close", 0), ("unlock", "input.stl")]


@pytest.mark.parametrize("bad_job", ["contains_reference", "inside_runtime"])
def test_refuses_reference_leak_or_writable_runtime_before_launch(tmp_path, bad_job):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    interpreter = runtime / "python.exe"
    interpreter.write_bytes(b"not executed")
    entry = runtime / "converter.pyc"
    entry.write_bytes(b"not executed")
    job = (runtime if bad_job == "inside_runtime" else tmp_path) / "job"
    job.mkdir()
    (job / "input.stl").write_bytes(b"neutral STL")
    if bad_job == "contains_reference":
        (job / "original.step").write_bytes(b"ground truth must stay outside converter access")
    backend = ProtocolBackend()
    with pytest.raises(ValueError, match="separate|fresh job"):
        sandbox.run_converter(
            interpreter,
            entry,
            job,
            "mm",
            owned_root=tmp_path,
            profile="existing-task-profile",
            _backend=backend,
        )
    assert backend.calls == []
    assert (job / "input.stl").is_file()  # Refusal preserves input and recovery evidence.


def test_owned_paths_refuse_root_outside_and_symlink(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    outside = tmp_path / "private.step"
    outside.write_bytes(b"private reference, not granted")
    with pytest.raises(ValueError):
        sandbox.owned_path(outside, owned)
    with pytest.raises(ValueError):
        sandbox.owned_path(owned, owned)
    link = owned / "link.step"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable without additional Windows authority")
    with pytest.raises(ValueError):
        sandbox.owned_path(link, owned)


@pytest.mark.parametrize("timeout", [0, -1, 361, float("inf"), float("nan")])
def test_bad_timeout_refused_without_native_launch(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeouts"):
        sandbox.run_converter(
            Path("missing"),
            Path("missing"),
            Path("missing"),
            "mm",
            owned_root=tmp_path,
            profile="existing",
            timeout=timeout,
        )
