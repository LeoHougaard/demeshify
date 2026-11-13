"""Run a converter in an existing Windows AppContainer and drain its entire job.

This host creates no profiles or permission grants. The controller must verify
sealed converter/runtime identities, provision a fresh task-owned job, and revoke
its scoped grant after this function returns and before grading its outputs.
"""

from __future__ import annotations

import ctypes as C
import math
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from ctypes import wintypes as W
from dataclasses import dataclass
from pathlib import Path

NEW_PROCESS = 6
EXIT_PROCESS = 7
ABNORMAL_EXIT_PROCESS = 8
ACTIVE_PROCESS_ZERO = 4


class IsolationError(RuntimeError):
    """The host could not establish or retain the required process boundary."""


@dataclass(frozen=True)
class ProcessAudit:
    pid: int
    appcontainer_sid: str
    in_job: bool
    capability_count: int
    image_path: str = ""


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    root_pid: int
    process_audits: tuple[ProcessAudit, ...]
    job_drained: bool
    affinity_mask: int


def owned_path(path: Path, root: Path) -> Path:
    """Refuse root aliases, escaped paths and reparse-point descendants."""
    root = Path(root)
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError("Owned root cannot be a link or junction")
    root = root.resolve(strict=True)
    raw = Path(path).absolute()
    resolved = raw.resolve(strict=True)
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("Expected a descendant of the task-owned root")
    for item in (raw, *raw.parents):
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("Owned artifact cannot traverse a link or junction")
    return resolved


def environment(job: Path) -> dict[str, str]:
    """A neutral environment, excluding inherited credentials and source paths."""
    system = os.environ.get("SystemRoot", "C:/Windows")
    result = {
        "SystemRoot": system,
        "WINDIR": system,
        "PATH": str(Path(system) / "System32"),
        "TEMP": str(job / "tmp"),
        "TMP": str(job / "tmp"),
        "USERPROFILE": str(job),
        "LOCALAPPDATA": str(job),
        "APPDATA": str(job),
        "DEMESHIFY_EVALUATION_JOB": str(job),
        "PYTHONDONTWRITEBYTECODE": "1",
        "STL_TO_STEP_RUNS_DIR": str(job / "runs"),
        "MPLCONFIGDIR": str(job / "mpl"),
    }
    result.update(
        {
            name: "1"
            for name in (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
                "NUMBA_NUM_THREADS",
            )
        }
    )
    return result


def _drain(backend, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while backend.active_processes():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IsolationError(
                "Job termination did not drain every process; do not grade outputs"
            )
        backend.next_event(remaining)
    backend.wait_processes(max(0.0, deadline - time.monotonic()))


def _run(
    backend, argv: list[str], cwd: Path, env: dict[str, str], timeout: float, cleanup_timeout: float
) -> SandboxResult:
    audits: dict[int, ProcessAudit] = {}
    drained = False
    try:
        root_pid = backend.launch_suspended(argv, cwd, env)
        audits[root_pid] = backend.audit(root_pid)
        backend.resume()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Isolated conversion exceeded {timeout:g} seconds")
            event = backend.next_event(remaining)
            if event is None:
                raise TimeoutError(f"Isolated conversion exceeded {timeout:g} seconds")
            message, pid = event
            if message == NEW_PROCESS and pid not in audits:
                # These records come from host token/job queries, never files
                # writable by the converter. An exited/unauditable child fails.
                audits[pid] = backend.audit(pid)
            elif message == ACTIVE_PROCESS_ZERO:
                if backend.active_processes():
                    raise IsolationError(
                        "Job zero notification disagrees with active process count"
                    )
                if len(audits) != backend.total_processes():
                    raise IsolationError("Not every job process received a host token audit")
                # Job accounting can reach zero before process handles signal.
                # Retain every observed handle and wait for actual termination,
                # so inherited/open output handles cannot outlive this boundary.
                backend.wait_processes(max(0.0, deadline - time.monotonic()))
                drained = True
                return SandboxResult(
                    backend.exit_code(),
                    root_pid,
                    tuple(audits.values()),
                    True,
                    backend.affinity_mask,
                )
    finally:
        try:
            if not drained:
                backend.terminate()
                _drain(backend, cleanup_timeout)
        finally:
            backend.close()


def run_converter(
    executable: Path,
    entrypoint: Path,
    job: Path,
    units: str,
    *,
    owned_root: Path,
    profile: str,
    timeout: float = 240,
    cleanup_timeout: float = 10,
    _backend=None,
) -> SandboxResult:
    """Return only after the entire converter job has exited, with host audits.

    The selected profile must already exist. Runtime/application ACLs must be
    read/execute only and the job must have its scoped grant. Nothing in this
    API qualifies the grader, corpus, ACL setup or cache identity by itself.
    """
    if (
        not math.isfinite(timeout)
        or not 0 < timeout <= 360
        or not math.isfinite(cleanup_timeout)
        or not 0 < cleanup_timeout <= 60
    ):
        raise ValueError("Use positive bounded conversion and cleanup timeouts")
    if units not in ("mm", "cm", "m", "in") or not profile:
        raise ValueError("Expected supported units and an existing profile name")
    executable, entrypoint, job = (owned_path(p, owned_root) for p in (executable, entrypoint, job))
    if not executable.is_file() or not entrypoint.is_file() or not job.is_dir():
        raise ValueError("Expected existing interpreter, compiled entrypoint and job directory")
    if entrypoint.suffix != ".pyc":
        raise ValueError("Expected a frozen bytecode entrypoint")
    if (
        executable.is_relative_to(job)
        or entrypoint.is_relative_to(job)
        or job.is_relative_to(executable.parent)
        or job.is_relative_to(entrypoint.parent)
    ):
        raise ValueError("The writable job must be separate from runtime and application")
    if {path.name for path in job.iterdir()} != {"input.stl"}:
        raise ValueError("Use a fresh job containing only the copied input STL")
    input_path = owned_path(job / "input.stl", owned_root)
    if not input_path.is_file():
        raise ValueError("Expected a copied input STL")
    (job / "tmp").mkdir(exist_ok=True)
    backend = _backend if _backend is not None else _Win32(profile)
    with backend.lock_input(input_path):
        return _run(
            backend,
            [str(executable), "-I", "-B", str(entrypoint), str(job), units],
            entrypoint.parent,
            environment(job),
            timeout,
            cleanup_timeout,
        )


class _StartupInfo(C.Structure):
    _fields_ = [
        ("cb", W.DWORD),
        ("lpReserved", W.LPWSTR),
        ("lpDesktop", W.LPWSTR),
        ("lpTitle", W.LPWSTR),
        ("dwX", W.DWORD),
        ("dwY", W.DWORD),
        ("dwXSize", W.DWORD),
        ("dwYSize", W.DWORD),
        ("dwXCountChars", W.DWORD),
        ("dwYCountChars", W.DWORD),
        ("dwFillAttribute", W.DWORD),
        ("dwFlags", W.DWORD),
        ("wShowWindow", W.WORD),
        ("cbReserved2", W.WORD),
        ("lpReserved2", C.c_void_p),
        ("hStdInput", W.HANDLE),
        ("hStdOutput", W.HANDLE),
        ("hStdError", W.HANDLE),
    ]


class _StartupInfoEx(C.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", C.c_void_p)]


class _Caps(C.Structure):
    _fields_ = [
        ("AppContainerSid", C.c_void_p),
        ("Capabilities", C.c_void_p),
        ("CapabilityCount", W.DWORD),
        ("Reserved", W.DWORD),
    ]


class _ProcessInfo(C.Structure):
    _fields_ = [
        ("hProcess", W.HANDLE),
        ("hThread", W.HANDLE),
        ("dwProcessId", W.DWORD),
        ("dwThreadId", W.DWORD),
    ]


class _BasicLimits(C.Structure):
    _fields_ = [
        ("ProcessTime", C.c_longlong),
        ("JobTime", C.c_longlong),
        ("LimitFlags", W.DWORD),
        ("MinimumWorkingSet", C.c_size_t),
        ("MaximumWorkingSet", C.c_size_t),
        ("ActiveProcessLimit", W.DWORD),
        ("Affinity", C.c_size_t),
        ("PriorityClass", W.DWORD),
        ("SchedulingClass", W.DWORD),
    ]


class _IoCounters(C.Structure):
    _fields_ = [
        (name, C.c_ulonglong)
        for name in (
            "ReadOperations",
            "WriteOperations",
            "OtherOperations",
            "ReadBytes",
            "WriteBytes",
            "OtherBytes",
        )
    ]


class _ExtendedLimits(C.Structure):
    _fields_ = [
        ("Basic", _BasicLimits),
        ("Io", _IoCounters),
        ("ProcessMemory", C.c_size_t),
        ("JobMemory", C.c_size_t),
        ("PeakProcessMemory", C.c_size_t),
        ("PeakJobMemory", C.c_size_t),
    ]


class _Accounting(C.Structure):
    _fields_ = [
        (name, C.c_longlong)
        for name in ("UserTime", "KernelTime", "ThisPeriodUserTime", "ThisPeriodKernelTime")
    ] + [
        (name, W.DWORD)
        for name in ("PageFaults", "TotalProcesses", "ActiveProcesses", "TerminatedProcesses")
    ]


class _CompletionPort(C.Structure):
    _fields_ = [("Key", C.c_void_p), ("Port", W.HANDLE)]


class _Win32:
    """Native boundary; imported safely without loading DLLs on other platforms."""

    def __init__(self, profile: str):
        if sys.platform != "win32":
            raise OSError("The evaluation AppContainer host requires Windows")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.advapi = C.WinDLL("advapi32", use_last_error=True)
        self.userenv = C.WinDLL("userenv", use_last_error=True)
        self.job = self.port = None
        self.sid = C.c_void_p()
        self.pi = _ProcessInfo()
        self.assigned = False
        self.attributes_initialized = False
        self.profile = profile
        self.affinity_mask = 0
        self.process_handles = {}
        self._bind()

    def _bind(self):
        signatures = {
            "InitializeProcThreadAttributeList": (
                W.BOOL,
                [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)],
            ),
            "UpdateProcThreadAttribute": (
                W.BOOL,
                [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p],
            ),
            "DeleteProcThreadAttributeList": (None, [C.c_void_p]),
            "CreateProcessW": (
                W.BOOL,
                [
                    W.LPCWSTR,
                    W.LPWSTR,
                    C.c_void_p,
                    C.c_void_p,
                    W.BOOL,
                    W.DWORD,
                    C.c_void_p,
                    W.LPCWSTR,
                    C.POINTER(_StartupInfoEx),
                    C.POINTER(_ProcessInfo),
                ],
            ),
            "CreateJobObjectW": (W.HANDLE, [C.c_void_p, W.LPCWSTR]),
            "CreateIoCompletionPort": (W.HANDLE, [W.HANDLE, W.HANDLE, C.c_size_t, W.DWORD]),
            "GetQueuedCompletionStatus": (
                W.BOOL,
                [
                    W.HANDLE,
                    C.POINTER(W.DWORD),
                    C.POINTER(C.c_size_t),
                    C.POINTER(C.c_void_p),
                    W.DWORD,
                ],
            ),
            "SetInformationJobObject": (W.BOOL, [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]),
            "QueryInformationJobObject": (
                W.BOOL,
                [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p],
            ),
            "AssignProcessToJobObject": (W.BOOL, [W.HANDLE, W.HANDLE]),
            "IsProcessInJob": (W.BOOL, [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)]),
            "TerminateJobObject": (W.BOOL, [W.HANDLE, W.UINT]),
            "TerminateProcess": (W.BOOL, [W.HANDLE, W.UINT]),
            "ResumeThread": (W.DWORD, [W.HANDLE]),
            "WaitForSingleObject": (W.DWORD, [W.HANDLE, W.DWORD]),
            "GetExitCodeProcess": (W.BOOL, [W.HANDLE, C.POINTER(W.DWORD)]),
            "OpenProcess": (W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            "QueryFullProcessImageNameW": (
                W.BOOL,
                [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)],
            ),
            "GetCurrentProcess": (W.HANDLE, []),
            "GetProcessAffinityMask": (
                W.BOOL,
                [W.HANDLE, C.POINTER(C.c_size_t), C.POINTER(C.c_size_t)],
            ),
            "CloseHandle": (W.BOOL, [W.HANDLE]),
            "CreateFileW": (
                W.HANDLE,
                [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE],
            ),
            "LocalFree": (C.c_void_p, [C.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            self._signature(self.kernel, name, result, arguments)
        self._signature(
            self.userenv,
            "DeriveAppContainerSidFromAppContainerName",
            C.c_long,
            [W.LPCWSTR, C.POINTER(C.c_void_p)],
        )
        self._signature(self.advapi, "FreeSid", C.c_void_p, [C.c_void_p])
        self._signature(
            self.advapi, "OpenProcessToken", W.BOOL, [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
        )
        self._signature(
            self.advapi,
            "GetTokenInformation",
            W.BOOL,
            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)],
        )
        self._signature(self.advapi, "EqualSid", W.BOOL, [C.c_void_p, C.c_void_p])
        self._signature(
            self.advapi, "ConvertSidToStringSidW", W.BOOL, [C.c_void_p, C.POINTER(W.LPWSTR)]
        )

    @staticmethod
    def _signature(library, name, result, arguments):
        method = getattr(library, name)
        method.restype, method.argtypes = result, arguments

    @staticmethod
    def _check(value):
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    @contextmanager
    def lock_input(self, path):
        handle = self.kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
        if handle == W.HANDLE(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            yield
        finally:
            self.kernel.CloseHandle(handle)

    def launch_suspended(self, argv, cwd, env):
        hr = self.userenv.DeriveAppContainerSidFromAppContainerName(self.profile, C.byref(self.sid))
        if hr:
            raise OSError(f"Cannot derive AppContainer SID: {hr & 0xFFFFFFFF:08x}")
        self.job = self._check(self.kernel.CreateJobObjectW(None, None))
        self.port = self._check(self.kernel.CreateIoCompletionPort(W.HANDLE(-1).value, None, 0, 1))
        association = _CompletionPort(1, self.port)
        self._check(
            self.kernel.SetInformationJobObject(
                self.job, 7, C.byref(association), C.sizeof(association)
            )
        )
        mask, system = C.c_size_t(), C.c_size_t()
        self._check(
            self.kernel.GetProcessAffinityMask(
                self.kernel.GetCurrentProcess(), C.byref(mask), C.byref(system)
            )
        )
        if not mask.value:
            raise IsolationError("Host has no allowed CPU")
        self.affinity_mask = mask.value & -mask.value
        limits = _ExtendedLimits()
        limits.Basic.Affinity = self.affinity_mask
        limits.Basic.PriorityClass = 0x4000  # BELOW_NORMAL_PRIORITY_CLASS
        limits.Basic.LimitFlags = (
            0x2000 | 0x10 | 0x20
        )  # kill-on-close, affinity, priority; no breakaway
        self._check(
            self.kernel.SetInformationJobObject(self.job, 9, C.byref(limits), C.sizeof(limits))
        )
        size = C.c_size_t()
        self.kernel.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
        self.attributes = C.create_string_buffer(size.value)
        self._check(
            self.kernel.InitializeProcThreadAttributeList(self.attributes, 1, 0, C.byref(size))
        )
        self.attributes_initialized = True
        caps = _Caps(self.sid, None, 0, 0)
        self._check(
            self.kernel.UpdateProcThreadAttribute(
                self.attributes, 0, 0x20009, C.byref(caps), C.sizeof(caps), None, None
            )
        )
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = C.sizeof(startup)
        startup.StartupInfo.dwFlags = 1  # STARTF_USESHOWWINDOW, SW_HIDE=0
        startup.lpAttributeList = C.cast(self.attributes, C.c_void_p)
        command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
        block = C.create_unicode_buffer(
            "\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0"
        )
        self._check(
            self.kernel.CreateProcessW(
                argv[0],
                command,
                None,
                None,
                False,
                0x80000 | 0x8000000 | 0x4000 | 0x4 | 0x400,
                C.cast(block, C.c_void_p),
                str(cwd),
                C.byref(startup),
                C.byref(self.pi),
            )
        )
        self._check(self.kernel.AssignProcessToJobObject(self.job, self.pi.hProcess))
        self.assigned = True
        return self.pi.dwProcessId

    def _token_info(self, token, kind):
        required = W.DWORD()
        self.advapi.GetTokenInformation(token, kind, None, 0, C.byref(required))
        if not required.value:
            raise IsolationError("Cannot size token information")
        buffer = C.create_string_buffer(required.value)
        self._check(
            self.advapi.GetTokenInformation(token, kind, buffer, len(buffer), C.byref(required))
        )
        return buffer

    def audit(self, pid):
        process = (
            self.pi.hProcess
            if pid == self.pi.dwProcessId
            else self._check(self.kernel.OpenProcess(0x100000 | 0x1000, False, pid))
        )
        self.process_handles[pid] = process
        token = W.HANDLE()
        try:
            member = W.BOOL()
            self._check(self.kernel.IsProcessInJob(process, self.job, C.byref(member)))
            self._check(self.advapi.OpenProcessToken(process, 8, C.byref(token)))
            is_container = C.cast(self._token_info(token, 29), C.POINTER(W.DWORD)).contents.value
            sid_buffer = self._token_info(token, 31)
            sid = C.cast(sid_buffer, C.POINTER(C.c_void_p)).contents.value
            count = C.cast(self._token_info(token, 30), C.POINTER(W.DWORD)).contents.value
            if not member.value or is_container != 1 or not sid or count:
                raise IsolationError("Process token/job/capabilities do not satisfy the boundary")
            if not self.advapi.EqualSid(sid, self.sid):
                raise IsolationError("Process has the wrong AppContainer SID")
            text = W.LPWSTR()
            self._check(self.advapi.ConvertSidToStringSidW(sid, C.byref(text)))
            try:
                length = W.DWORD(32768)
                image = C.create_unicode_buffer(length.value)
                self._check(
                    self.kernel.QueryFullProcessImageNameW(process, 0, image, C.byref(length))
                )
                return ProcessAudit(pid, text.value, True, count, image.value)
            finally:
                self.kernel.LocalFree(text)
        finally:
            if token:
                self.kernel.CloseHandle(token)

    def resume(self):
        if self.kernel.ResumeThread(self.pi.hThread) == 0xFFFFFFFF:
            raise C.WinError(C.get_last_error())

    def next_event(self, timeout):
        message, key, pid = W.DWORD(), C.c_size_t(), C.c_void_p()
        ok = self.kernel.GetQueuedCompletionStatus(
            self.port,
            C.byref(message),
            C.byref(key),
            C.byref(pid),
            max(1, math.ceil(timeout * 1000)),
        )
        if not ok:
            if C.get_last_error() == 258:
                return None
            raise C.WinError(C.get_last_error())
        if key.value != 1:
            raise IsolationError("Unexpected completion-port owner")
        return message.value, pid.value or 0

    def active_processes(self):
        if not self.job:
            return 0
        accounting = _Accounting()
        self._check(
            self.kernel.QueryInformationJobObject(
                self.job, 1, C.byref(accounting), C.sizeof(accounting), None
            )
        )
        return accounting.ActiveProcesses

    def total_processes(self):
        accounting = _Accounting()
        self._check(
            self.kernel.QueryInformationJobObject(
                self.job, 1, C.byref(accounting), C.sizeof(accounting), None
            )
        )
        return accounting.TotalProcesses

    def exit_code(self):
        code = W.DWORD()
        self._check(self.kernel.GetExitCodeProcess(self.pi.hProcess, C.byref(code)))
        if code.value == 259:  # STILL_ACTIVE
            raise IsolationError("Root process remains active after job drained")
        return code.value

    def wait_processes(self, timeout):
        deadline = time.monotonic() + timeout
        handles = {
            handle for handle in (self.pi.hProcess, *self.process_handles.values()) if handle
        }
        for handle in handles:
            milliseconds = max(0, math.ceil((deadline - time.monotonic()) * 1000))
            status = self.kernel.WaitForSingleObject(handle, milliseconds)
            if status == 258:
                raise IsolationError("Known job process did not finish termination; do not grade")
            if status != 0:
                raise C.WinError(C.get_last_error())

    def terminate(self):
        if self.job:
            self._check(self.kernel.TerminateJobObject(self.job, 1))
        if self.pi.hProcess and not self.assigned:
            self._check(self.kernel.TerminateProcess(self.pi.hProcess, 1))

    def close(self):
        for handle in {
            self.job,
            self.pi.hProcess,
            self.pi.hThread,
            self.port,
            *self.process_handles.values(),
        }:
            if handle:
                self.kernel.CloseHandle(handle)
        if self.attributes_initialized:
            self.kernel.DeleteProcThreadAttributeList(self.attributes)
        if self.sid:
            self.advapi.FreeSid(self.sid)
