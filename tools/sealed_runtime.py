"""Live Windows read locks justify hash reuse within one verified session.

There is no serialized seal or skip-verification flag. A session owns only the
handles it opens, hashes every expected file while write/delete sharing is
denied, and rejects any subsequent catalog or directory-notification change.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import json
import os
import re
import stat
import sys
from ctypes import wintypes as W
from pathlib import Path, PurePosixPath
from types import MappingProxyType

CAD_SUFFIXES = {'.step', '.stp', '.stl', '.brep', '.3mf'}
REPARSE = 0x400
INVALID_HANDLE = C.c_void_p(-1).value
# A failed cancellation must not free an OVERLAPPED buffer still owned by the
# kernel. Handles are closed; rare timed-out buffers remain until process exit.
_cancelled_buffers = []


class SealError(RuntimeError):
    """A live, unchanged, write-blocked catalog could not be established."""


def module_digest() -> str:
    return _digest(Path(__file__))


def _digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _metadata(path: Path) -> tuple:
    info = path.lstat()
    attributes = getattr(info, 'st_file_attributes', 0)
    if stat.S_ISLNK(info.st_mode) or attributes & REPARSE:
        raise SealError('Reparse points, links and junctions are not permitted')
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise SealError('Only ordinary files and directories can be sealed')
    if not info.st_ino:
        raise SealError('The filesystem does not expose stable file identities')
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise SealError('Hard-linked runtime files are not an owned immutable catalog')
    return (info.st_dev, info.st_ino, info.st_mode, attributes,
            info.st_nlink, info.st_size if stat.S_ISREG(info.st_mode) else 0)


class _FileInfo(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME),
                ('accessed', W.FILETIME), ('written', W.FILETIME),
                ('volume', W.DWORD), ('size_high', W.DWORD), ('size_low', W.DWORD),
                ('links', W.DWORD), ('index_high', W.DWORD), ('index_low', W.DWORD)]


class _Overlapped(C.Structure):
    _fields_ = [('internal', C.c_size_t), ('internal_high', C.c_size_t),
                ('offset', W.DWORD), ('offset_high', W.DWORD), ('event', W.HANDLE)]


class _FileId(C.Structure):
    _fields_ = [('volume', C.c_ulonglong), ('identifier', W.BYTE * 16)]


class _StreamInfo(C.Structure):
    _fields_ = [('size', C.c_longlong), ('name', W.WCHAR * 296)]


class _Windows:
    def __init__(self):
        if sys.platform != 'win32':
            raise SealError('Live sealed runtime sessions require Windows')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateFileW': ([W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD,
                             W.DWORD, W.HANDLE], W.HANDLE),
            'CloseHandle': ([W.HANDLE], W.BOOL),
            'GetFileInformationByHandle': ([W.HANDLE, C.POINTER(_FileInfo)], W.BOOL),
            'GetFileInformationByHandleEx': ([W.HANDLE, C.c_int, W.LPVOID, W.DWORD], W.BOOL),
            'ReadFile': ([W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID], W.BOOL),
            'CreateEventW': ([W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            'ReadDirectoryChangesW': ([W.HANDLE, W.LPVOID, W.DWORD, W.BOOL, W.DWORD,
                                       C.POINTER(W.DWORD), C.POINTER(_Overlapped),
                                       W.LPVOID], W.BOOL),
            'GetOverlappedResult': ([W.HANDLE, C.POINTER(_Overlapped),
                                     C.POINTER(W.DWORD), W.BOOL], W.BOOL),
            'CancelIoEx': ([W.HANDLE, C.POINTER(_Overlapped)], W.BOOL),
            'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
            'FindFirstStreamW': ([W.LPCWSTR, W.DWORD, C.POINTER(_StreamInfo), W.DWORD],
                                 W.HANDLE),
            'FindNextStreamW': ([W.HANDLE, C.POINTER(_StreamInfo)], W.BOOL),
            'FindClose': ([W.HANDLE], W.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def open(self, path: Path, *, directory=False, overlapped=False):
        flags = 0x00200000 | (0x02000000 if directory else 0)
        if overlapped:
            flags |= 0x40000000
        handle = self.kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, flags, None)
        if handle == INVALID_HANDLE:
            raise SealError('Cannot obtain a write/delete-blocking read handle: '
                            f'{C.get_last_error()}')
        return handle

    def close(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise SealError(f'Cannot close owned seal handle: {C.get_last_error()}')

    def identity(self, handle) -> tuple[int, int]:
        info = _FileInfo()
        if not self.kernel.GetFileInformationByHandle(handle, C.byref(info)):
            raise SealError('Cannot inspect the locked file identity')
        if info.attributes & REPARSE:
            raise SealError('A locked path is a reparse point')
        identity = _FileId()
        if not self.kernel.GetFileInformationByHandleEx(
                handle, 18, C.byref(identity), C.sizeof(identity)):
            raise SealError('Cannot establish the complete volume/file identity')
        return identity.volume, int.from_bytes(bytes(identity.identifier), 'little')

    def digest(self, handle) -> str:
        result = hashlib.sha256()
        buffer, count = C.create_string_buffer(262144), W.DWORD()
        while True:
            if not self.kernel.ReadFile(handle, buffer, len(buffer), C.byref(count), None):
                raise SealError('Cannot hash the locked file')
            if not count.value:
                return result.hexdigest()
            result.update(buffer.raw[:count.value])

    def streams(self, path):
        info = _StreamInfo()
        handle = self.kernel.FindFirstStreamW(str(path), 0, C.byref(info), 0)
        if handle == INVALID_HANDLE:
            if C.get_last_error() == 38:  # No data stream, e.g. an ordinary directory.
                return
            raise SealError('Cannot establish the complete NTFS stream catalog')
        try:
            while True:
                if info.name != '::$DATA':
                    raise SealError('Named alternate data streams are not permitted')
                if not self.kernel.FindNextStreamW(handle, C.byref(info)):
                    if C.get_last_error() != 38:
                        raise SealError('Cannot finish inspecting file streams')
                    break
        finally:
            if not self.kernel.FindClose(handle):
                raise SealError('Cannot close the owned stream-catalog handle')

    def watch(self, root):
        return _Watch(self, root)


class _Watch:
    def __init__(self, native, root):
        self.native, self.handle, self.event = native, None, None
        self.buffer = C.create_string_buffer(65536)
        self.overlapped = _Overlapped()
        self.pending = False
        try:
            self.handle = native.open(root, directory=True, overlapped=True)
            self.event = native.kernel.CreateEventW(None, True, False, None)
            if not self.event:
                raise SealError('Cannot create the directory-notification event')
            self.overlapped.event = self.event
            # Main bytes are write-locked. Monitor namespaces (including ADS),
            # attributes and security, not deferred directory access/write times.
            flags = 1 | 2 | 4 | 256 | 512
            okay = native.kernel.ReadDirectoryChangesW(
                self.handle, self.buffer, len(self.buffer), True, flags, None,
                C.byref(self.overlapped), None)
            if not okay and C.get_last_error() != 997:
                raise SealError('Cannot monitor changes beneath the immutable root')
            self.pending = True
        except BaseException:
            self.close()
            raise

    def changed(self):
        count = W.DWORD()
        if self.native.kernel.GetOverlappedResult(
                self.handle, C.byref(self.overlapped), C.byref(count), False):
            self.pending = False
            return True  # Including a zero-byte notification overflow.
        if C.get_last_error() != 996:
            raise SealError('Directory monitor failed or was interrupted')
        return False

    def close(self):
        errors = []
        if self.handle is not None:
            if self.pending:
                self.native.kernel.CancelIoEx(self.handle, C.byref(self.overlapped))
                if self.event and self.native.kernel.WaitForSingleObject(self.event, 2000) != 0:
                    _cancelled_buffers.append(self)
                    errors.append(SealError('Directory notification cancellation did not settle'))
                self.pending = False
            try:
                self.native.close(self.handle)
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.handle = None
        if self.event is not None:
            try:
                self.native.close(self.event)
            except BaseException as exc:
                errors.append(exc)
            self.event = None
        if errors:
            raise SealError('Cannot release owned directory monitor resources') from errors[0]


class SealedRuntimeSession:
    """A non-transferable context owning live read locks over pinned local roots.

    Pass {absolute_owned_root: {POSIX_relative_filename: sha256}} and the pinned
    SHA-256 of this loaded module. Call check() before/after every case; context
    exit also checks before unlocking. No result may be certified after close.
    """
    def __init__(self, catalogs: dict, *, module_sha256: str):
        if not re.fullmatch('[0-9a-f]{64}', module_sha256):
            raise ValueError('A pinned seal-module SHA-256 is required')
        self._module_sha256 = module_sha256
        roots = {}
        for raw_root, raw_files in catalogs.items():
            root = Path(raw_root)
            if not root.is_absolute() or '..' in root.parts or root == Path(root.anchor):
                raise ValueError('Immutable roots must be explicitly absolute')
            root = root.absolute()
            files = {}
            folded = set()
            for name, expected in raw_files.items():
                path = PurePosixPath(name)
                if (not name or path.is_absolute() or path.as_posix() != name
                        or any(part in ('.', '..') for part in path.parts)
                        or '\\' in name or ':' in name
                        or any(suffix.lower() in CAD_SUFFIXES for suffix in path.suffixes)
                        or not re.fullmatch('[0-9a-f]{64}', expected)):
                    raise ValueError('Expected a relative non-CAD filename and pinned SHA-256')
                if name.casefold() in folded:
                    raise ValueError('Case-insensitive catalog aliases are not permitted')
                files[name] = expected
                folded.add(name.casefold())
            if not files:
                raise ValueError('Each immutable root needs a nonempty pinned file catalog')
            if any(root == other or root.is_relative_to(other) or other.is_relative_to(root)
                   for other in roots):
                raise ValueError('Immutable roots must be distinct and non-overlapping')
            roots[root] = MappingProxyType(files)
        if not roots:
            raise ValueError('At least one immutable owned root is required')
        self._catalogs = MappingProxyType(roots)
        self._handles, self._watches, self._metadata = [], [], {}
        self._native = None
        self._started = self._live = False

    def _code_check(self):
        if module_digest() != self._module_sha256:
            raise SealError('Seal module changed; live hash reuse is no longer valid')

    def __reduce_ex__(self, protocol):
        raise TypeError('A live sealed session cannot be serialized')

    def __copy__(self):
        raise TypeError('A live sealed session cannot be copied')

    def __deepcopy__(self, memo):
        raise TypeError('A live sealed session cannot be copied')

    def _scan(self, root):
        result = {'.': _metadata(root)}
        pending = [root]
        while pending:
            directory = pending.pop()
            self._native.streams(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(root).as_posix()
                    result[relative] = _metadata(path)
                    if stat.S_ISDIR(result[relative][2]):
                        pending.append(path)
                    else:
                        self._native.streams(path)
        files = {name for name, info in result.items() if stat.S_ISREG(info[2])}
        if files != set(self._catalogs[root]):
            raise SealError('Immutable root has missing or extra files')
        return result

    def __enter__(self):
        if self._started:
            raise SealError('A sealed session cannot be reopened or transferred')
        self._started = True
        try:
            self._code_check()
            self._native = _Windows()
            for root in self._catalogs:
                # Read ancestors only; never acquire locks outside the owned root.
                for ancestor in (root, *root.parents):
                    _metadata(ancestor)
                self._watches.append(self._native.watch(root))
                catalog = self._scan(root)
                for name, info in catalog.items():
                    path = root if name == '.' else root / name
                    directory = stat.S_ISDIR(info[2])
                    handle = self._native.open(path, directory=directory)
                    self._handles.append(handle)
                    if self._native.identity(handle) != info[:2]:
                        raise SealError('Path identity changed while obtaining its lock')
                    if not directory and self._native.digest(handle) != self._catalogs[root][name]:
                        raise SealError('Pinned file hash mismatch while read locks are held')
                self._metadata[root] = catalog
            self._live = True
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    def check(self):
        if not self._live:
            raise SealError('The sealed session is closed, invalid or not entered')
        try:
            self._code_check()
            if any(watch.changed() for watch in self._watches):
                raise SealError('Immutable directory changed during the live session')
            for root, before in self._metadata.items():
                if self._scan(root) != before:
                    raise SealError('Immutable catalog names, attributes or identities changed')
            if any(watch.changed() for watch in self._watches):
                raise SealError('Immutable directory changed during catalog verification')
        except BaseException:
            self.close()
            raise

    @property
    def identity(self):
        self.check()
        return self._identity()

    def require_catalogs(self, catalogs: dict) -> str:
        """Check live protection and exact required catalogs; return trace identity.

        Additional separately sealed roots are allowed. The returned digest is
        evidence only, never a substitute for passing this same live object.
        """
        self.check()
        try:
            if not catalogs:
                raise SealError('Required artifact catalogs cannot be empty')
            for root, expected in catalogs.items():
                root = Path(root)
                if not root.is_absolute() or self._catalogs.get(root) != expected:
                    raise SealError('Live session does not cover the exact required '
                                    'artifact catalog')
            return self._identity()
        except BaseException:
            self.close()
            raise

    def _identity(self):
        payload = {'module_sha256': self._module_sha256,
                   'catalogs': {str(root): dict(files) for root, files in self._catalogs.items()},
                   'lock_protocol': 'windows-read-deny-write-delete-with-change-notifications-v1'}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def close(self):
        self._live = False
        errors = []
        for watch in self._watches:
            try:
                watch.close()
            except BaseException as exc:
                errors.append(exc)
        self._watches.clear()
        while self._handles:
            try:
                self._native.close(self._handles.pop())
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise SealError('Could not cleanly release all owned seal resources') from errors[0]

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.check()
        finally:
            self.close()
