import copy
import hashlib
import mmap
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from tools import sealed_runtime as sealed
from tools.freeze_evaluation_converter import compile_module

WINDOWS = pytest.mark.skipif(sys.platform != 'win32',
                             reason='Requires native Windows sharing locks')


def prepare(tmp_path):
    root = tmp_path / 'owned-artifact'
    (root / 'nested').mkdir(parents=True)
    source = root / 'nested/file.txt'
    source.write_bytes(b'original bytes')
    expected = {'nested/file.txt': hashlib.sha256(source.read_bytes()).hexdigest()}
    return root, source, expected


def session(root, expected):
    return sealed.SealedRuntimeSession({root: expected}, module_sha256=sealed.module_digest())


@WINDOWS
def test_native_locks_block_write_truncate_delete_and_rename_then_release(tmp_path):
    root, source, expected = prepare(tmp_path)
    active = session(root, expected)
    with active:
        assert source.read_bytes() == b'original bytes'
        for operation in (
            lambda: source.write_bytes(b'wrong'), lambda: source.open('wb'), source.unlink,
            lambda: source.rename(source.with_name('other.txt')),
            lambda: source.parent.rename(root / 'renamed'),
            lambda: root.rename(root.with_name('renamed-root')),
            lambda: tmp_path.rename(tmp_path.with_name(tmp_path.name + '-renamed-parent')),
        ):
            with pytest.raises(OSError):
                operation()
        active.check()
        assert len(active.identity) == 64
    with pytest.raises(sealed.SealError, match='closed'):
        active.check()
    with pytest.raises(sealed.SealError, match='reopened'):
        active.__enter__()
    source.write_bytes(b'changed after close')
    root.rename(root.with_name('released-root'))


@WINDOWS
def test_native_generated_bytecode_can_be_imported_while_locked(tmp_path):
    root = tmp_path / 'python-artifact'
    root.mkdir()
    code = root / 'toy.pyc'
    compile_module(b'VALUE = 73\n', 'toy.py', code)
    with session(root, {'toy.pyc': sealed._digest(code)}):
        result = subprocess.check_output([
            sys.executable, '-I', '-B', '-c',
            'import sys;sys.path.insert(0,sys.argv[1]);import toy;print(toy.VALUE)', str(root),
        ], text=True)
        assert result.strip() == '73'


@WINDOWS
def test_native_preexisting_writer_or_writable_mapping_prevents_seal(tmp_path):
    root, source, expected = prepare(tmp_path)
    with source.open('r+b'):
        with pytest.raises(sealed.SealError, match='read handle'):
            with session(root, expected):
                pytest.fail('A preexisting writer must prevent sealing')
    with source.open('r+b') as stream:
        mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_WRITE)
    try:
        with pytest.raises(sealed.SealError, match='read handle'):
            with session(root, expected):
                pytest.fail('A preexisting writable mapping must prevent sealing')
    finally:
        mapping.close()
    source.write_bytes(b'locks released after failed acquisition')


@WINDOWS
def test_native_added_then_removed_file_latches_invalidation(tmp_path):
    root, source, expected = prepare(tmp_path)
    with pytest.raises(sealed.SealError, match='changed'):
        with session(root, expected) as active:
            transient = root / 'unexpected-private.step'
            transient.write_bytes(b'must not be silently accepted')
            transient.unlink()
            active.check()
    source.write_bytes(b'locks released after catalog violation')
    with pytest.raises(sealed.SealError, match='closed'):
        active.check()


@WINDOWS
def test_native_named_streams_and_transient_stream_changes_fail_closed(tmp_path):
    root, source, expected = prepare(tmp_path)
    stream = Path(str(source) + ':hidden-data')
    stream.write_bytes(b'not in the pinned catalog')
    with pytest.raises(sealed.SealError, match='streams'):
        with session(root, expected):
            pytest.fail('Named streams must not enter a sealed catalog')
    stream.unlink()
    with pytest.raises(sealed.SealError, match='changed'):
        with session(root, expected) as active:
            stream.write_bytes(b'created while main data stream is locked')
            stream.unlink()
            active.check()
    source.write_bytes(b'released')


@WINDOWS
def test_native_exceptions_and_hash_mismatch_release_all_owned_locks(tmp_path):
    root, source, expected = prepare(tmp_path)
    with pytest.raises(RuntimeError, match='caller failed'):
        with session(root, expected):
            raise RuntimeError('caller failed')
    source.write_bytes(b'bytes changed')
    with pytest.raises(sealed.SealError, match='hash mismatch'):
        with session(root, expected):
            pytest.fail('Changed bytes must not be sealed')
    source.write_bytes(b'locks released after hash mismatch')
    root.rename(root.with_name('released'))


@WINDOWS
def test_native_extra_files_and_hardlinks_fail_closed(tmp_path):
    root, source, expected = prepare(tmp_path)
    extra = root / 'extra.txt'
    extra.write_bytes(b'extra')
    with pytest.raises(sealed.SealError, match='extra files'):
        with session(root, expected):
            pytest.fail('Extra catalog files must be rejected')
    extra.unlink()
    alias = tmp_path / 'outside-hardlink.txt'
    alias.hardlink_to(source)
    with pytest.raises(sealed.SealError, match='Hard-linked'):
        with session(root, expected):
            pytest.fail('Hard-linked roots have unowned aliases')
    alias.unlink()
    source.write_bytes(b'released after rejected alias')


@WINDOWS
def test_native_module_identity_changes_invalidate_and_unlock(tmp_path, monkeypatch):
    root, source, expected = prepare(tmp_path)
    with pytest.raises(sealed.SealError, match='module changed'):
        with session(root, expected) as active:
            monkeypatch.setattr(sealed, 'module_digest', lambda: '0' * 64)
            active.check()
    source.write_bytes(b'released after module identity mismatch')


@pytest.mark.parametrize('name', ['../escaped.py', '/absolute.py', 'nested\\..\\escaped.py',
                                 'source.step.gz', 'code.py:extra', 'double//name.py'])
def test_catalog_rejects_escaped_alias_and_geometry_names(tmp_path, name):
    with pytest.raises(ValueError):
        session(tmp_path, {name: '0' * 64})


def test_catalog_copies_pins_and_rejects_case_aliases_or_overlapping_roots(tmp_path):
    with pytest.raises(ValueError, match='Case-insensitive'):
        session(tmp_path, {'Code.py': '0' * 64, 'code.py': '0' * 64})
    with pytest.raises(ValueError, match='overlapping'):
        sealed.SealedRuntimeSession({tmp_path: {'a.py': '0' * 64},
                                     tmp_path / 'child': {'b.py': '0' * 64}},
                                    module_sha256=sealed.module_digest())
    pin = {'code.py': '0' * 64}
    guard = session(tmp_path, pin)
    pin['code.py'] = '1' * 64
    assert guard._catalogs[tmp_path]['code.py'] == '0' * 64
    with pytest.raises(sealed.SealError, match='not entered'):
        guard.check()
    for operation in (lambda: pickle.dumps(guard), lambda: copy.copy(guard),
                      lambda: copy.deepcopy(guard)):
        with pytest.raises(TypeError, match='cannot be'):
            operation()


@WINDOWS
def test_native_junction_cannot_escape_owned_root(tmp_path):
    root, source, expected = prepare(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    junction = root / 'alias'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        pytest.skip('The local filesystem does not permit a task-owned junction fixture')
    try:
        with pytest.raises(sealed.SealError, match='Reparse'):
            with session(root, expected):
                pytest.fail('A junction must not enter the sealed catalog')
    finally:
        junction.rmdir()
    source.write_bytes(b'released after rejected junction')


def test_non_windows_default_fails_closed(monkeypatch, tmp_path):
    root, _, expected = prepare(tmp_path)
    monkeypatch.setattr(sealed.sys, 'platform', 'unsupported')
    with pytest.raises(sealed.SealError, match='require Windows'):
        with session(root, expected):
            pytest.fail('No pretend non-Windows seal is permitted')
