import importlib.util
import json
import sys

import pytest

from tools import freeze_evaluation_converter as converter
from tools import freeze_evaluation_grader as grader
from tools import locked_artifact_identity as locked
from tools import sealed_runtime as seal

WINDOWS = pytest.mark.skipif(sys.platform != 'win32', reason='Requires live native read locks')


def artifact(tmp_path, kind='converter', runtime=None):
    runtime = runtime or tmp_path / 'runtime'
    runtime.mkdir(exist_ok=True)
    if not (runtime / 'python.exe').exists():
        (runtime / 'python.exe').write_bytes(b'protocol runtime, never executed')
    root = tmp_path / kind
    (root / 'application').mkdir(parents=True)
    (root / 'application' / (kind + '.pyc')).write_bytes(b'protocol compiled entrypoint')
    (root / 'LICENSE').write_bytes(b'actual pinned protocol license')
    metadata = {'schema': 1, 'source_revision': 'a' * 40,
                'python_magic': importlib.util.MAGIC_NUMBER.hex(),
                'python_runtime': str(runtime), 'runtime_files': converter.inventory(runtime),
                'application_files': converter.inventory(root / 'application'),
                'license_sha256': converter.digest(root / 'LICENSE')}
    if kind == 'grader':
        dependencies = root / 'dependencies'
        dependencies.mkdir()
        (dependencies / 'pyproject.toml').write_bytes(b'[project]\nname="protocol"\n')
        (dependencies / 'uv.lock').write_bytes(b'version=1\n')
        metadata.update(artifact_kind='grader', entrypoint='application/grader.pyc',
                        dependency_files=converter.inventory(dependencies))
    (root / 'manifest.json').write_text(json.dumps(metadata))
    pin = converter.digest(root / 'manifest.json')
    return root, runtime, pin, metadata


def session(catalogs):
    return seal.SealedRuntimeSession(catalogs, module_sha256=seal.module_digest())


@WINDOWS
def test_native_whole_artifacts_match_full_verifiers_and_block_all_required_writes(
    tmp_path, monkeypatch,
):
    conversion, runtime, converter_pin, conversion_metadata = artifact(tmp_path)
    grading, _, grader_pin, grading_metadata = artifact(tmp_path, 'grader', runtime)
    assert converter.verify(conversion, converter_pin) == conversion_metadata
    assert grader.verify(grading, grader_pin) == grading_metadata
    _, catalogs = locked.artifact_catalogs(conversion, converter_pin, expected_kind='converter')
    _, grading_catalogs = locked.artifact_catalogs(grading, grader_pin, expected_kind='grader')
    catalogs.update(grading_catalogs)
    with session(catalogs) as active:
        # Live validation must not fall back to repeatedly hashing whole trees.
        monkeypatch.setattr(converter, 'inventory',
                            lambda *_: pytest.fail('Full inventory repeated'))
        first, identity = locked.verify_locked_converter(conversion, converter_pin, active)
        second, same_identity = locked.verify_locked_artifact(
            grading, grader_pin, active, expected_kind='grader')
        assert first == conversion_metadata and second == grading_metadata
        assert identity == same_identity == active.identity
        for path in (conversion / 'manifest.json', conversion / 'LICENSE',
                     conversion / 'application/converter.pyc', runtime / 'python.exe',
                     grading / 'manifest.json', grading / 'LICENSE',
                     grading / 'dependencies/uv.lock', grading / 'dependencies/pyproject.toml',
                     grading / 'application/grader.pyc'):
            with pytest.raises(OSError):
                path.write_bytes(b'cannot change a required pinned file')
        active.check()


@WINDOWS
@pytest.mark.parametrize('omission', ['metadata_root', 'runtime'])
def test_native_incomplete_catalog_coverage_is_never_a_verification_reuse(tmp_path, omission):
    root, runtime, pin, metadata = artifact(tmp_path)
    _, catalogs = locked.converter_catalogs(root, pin)
    if omission == 'metadata_root':
        catalogs = {root / 'application': metadata['application_files'],
                    runtime: metadata['runtime_files']}
    else:
        catalogs.pop(runtime)
    active = session(catalogs)
    with pytest.raises(seal.SealError, match='exact required artifact catalog'):
        with active:
            locked.verify_locked_converter(root, pin, active)
    with pytest.raises(seal.SealError, match='closed'):
        active.check()


@WINDOWS
def test_native_closed_session_cannot_authorize_reuse(tmp_path):
    root, _, pin, _ = artifact(tmp_path)
    _, catalogs = locked.converter_catalogs(root, pin)
    active = session(catalogs)
    with active:
        locked.verify_locked_converter(root, pin, active)
    with pytest.raises(seal.SealError, match='closed'):
        locked.verify_locked_converter(root, pin, active)


@WINDOWS
@pytest.mark.parametrize('target', ['manifest.json', 'LICENSE', 'application/grader.pyc',
                                   'dependencies/uv.lock', 'runtime'])
def test_native_preseal_tamper_cannot_enter_a_pinned_artifact_session(tmp_path, target):
    root, runtime, pin, _ = artifact(tmp_path, 'grader')
    _, catalogs = locked.artifact_catalogs(root, pin, expected_kind='grader')
    path = runtime / 'python.exe' if target == 'runtime' else root / target
    path.write_bytes(b'changed before locks')
    with pytest.raises(seal.SealError, match='hash mismatch'):
        with session(catalogs):
            pytest.fail('Tampered catalogs must fail before reuse')


@pytest.mark.parametrize('fake', [True, False, 'serialized-session-identity', {}, object()])
def test_trace_strings_flags_and_duck_types_cannot_authorize_reuse(tmp_path, fake):
    with pytest.raises(TypeError, match='actual live'):
        locked.verify_locked_converter(tmp_path, '0' * 64, fake)


def test_subclass_with_no_op_check_is_not_a_session_authority(tmp_path):
    class FakeSession(seal.SealedRuntimeSession):
        def check(self):
            pass

    with pytest.raises(TypeError, match='actual live'):
        locked.verify_locked_converter(tmp_path, '0' * 64, object.__new__(FakeSession))


@pytest.mark.parametrize('mutation', ['pin', 'magic', 'schema', 'missing_manifest',
                                     'missing_declaration', 'wrong_kind'])
def test_manifest_headers_and_declarations_are_checked_before_catalog_reuse(tmp_path, mutation):
    root, _, pin, metadata = artifact(tmp_path, 'grader')
    if mutation == 'pin':
        pin = '0' * 64
    elif mutation == 'missing_manifest':
        (root / 'manifest.json').unlink()
    else:
        if mutation == 'magic':
            metadata['python_magic'] = 'bad'
        elif mutation == 'schema':
            metadata['schema'] = 2
        elif mutation == 'missing_declaration':
            metadata['dependency_files'].pop('uv.lock')
        else:
            metadata['artifact_kind'] = 'converter'
        (root / 'manifest.json').write_text(json.dumps(metadata))
        pin = converter.digest(root / 'manifest.json')
    with pytest.raises((ValueError, FileNotFoundError)):
        locked.artifact_catalogs(root, pin, expected_kind='grader')


def test_no_catalog_function_claims_that_unlocked_bytes_are_verified(tmp_path):
    root, runtime, pin, metadata = artifact(tmp_path)
    actual, catalogs = locked.converter_catalogs(root, pin)
    assert actual == metadata
    assert catalogs[root]['manifest.json'] == pin
    assert catalogs[root]['LICENSE'] == metadata['license_sha256']
    assert catalogs[root]['application/converter.pyc'] == (
        metadata['application_files']['converter.pyc'])
    assert catalogs[runtime] == metadata['runtime_files']
