"""Sample an owned process tree on Windows without another dependency."""
from __future__ import annotations

import ctypes
import os
import threading
from contextlib import contextmanager
from ctypes import wintypes as w


def windows_tree_rss(root_pid: int) -> int:
    class Entry(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                    ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                    ('name', w.WCHAR * 260)]

    class Memory(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('faults', w.DWORD),
                    *[(name, ctypes.c_size_t) for name in (
                        'peak', 'rss', 'peak_paged', 'paged', 'peak_nonpaged',
                        'nonpaged', 'pagefile', 'peak_pagefile')]]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(Memory), w.DWORD]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[entry.pid] = entry.parent
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    owned = {root_pid}
    while children := {pid for pid, parent in parents.items() if parent in owned} - owned:
        owned.update(children)
    total = 0
    for pid in owned:
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle:
            continue  # A process may exit between the snapshot and this query.
        try:
            memory = Memory()
            memory.size = ctypes.sizeof(memory)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.size):
                total += memory.rss
        finally:
            kernel.CloseHandle(handle)
    return total


@contextmanager
def sampled_resources():
    record = {'sampled_peak_tree_rss_bytes': None, 'memory_sampling_interval_seconds': 1,
              'memory_sample_count': 0}
    stop = threading.Event()

    def sample():
        if os.name != 'nt':
            record['memory_sampling_error'] = 'Windows tree sampler unavailable'
            return
        while not stop.is_set():
            try:
                value = windows_tree_rss(os.getpid())
                record['sampled_peak_tree_rss_bytes'] = max(
                    record['sampled_peak_tree_rss_bytes'] or 0, value)
                record['memory_sample_count'] += 1
            except OSError as exc:
                record['memory_sampling_error'] = str(exc)
                return
            stop.wait(1)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        yield record
    finally:
        stop.set()
        thread.join(timeout=2)
