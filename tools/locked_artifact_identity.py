"""Bind artifact verification to exact catalogs protected by a live OS seal.

Catalog dictionaries and trace digests are not authority. Reuse requires the
actual, non-transferable SealedRuntimeSession, including manifest and license.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path, PurePosixPath

from tools.freeze_evaluation_converter import digest
from tools.sealed_runtime import CAD_SUFFIXES, SealedRuntimeSession


def _hash(value) -> str:
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Expected a pinned lowercase SHA-256')
    return value


def _plain_directory(raw: Path) -> Path:
    raw = Path(raw)
    if not raw.is_absolute() or '..' in raw.parts:
        raise ValueError('Artifact and runtime roots must be explicitly absolute')
    for path in (raw, *raw.parents):
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Pinned artifact paths cannot traverse links or junctions')
    if not raw.is_dir():
        raise ValueError('Expected an existing artifact or runtime directory')
    return raw.resolve(strict=True)


def _catalog(value, label: str, prefix: str = '') -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f'Expected a nonempty pinned {label} catalog')
    result, folded = {}, set()
    for name, expected in value.items():
        if not isinstance(name, str):
            raise ValueError('Catalog filenames must be strings')
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or path.as_posix() != name
                or '..' in path.parts or '\\' in name or ':' in name
                or any(suffix.lower() in CAD_SUFFIXES for suffix in path.suffixes)
                or name.casefold() in folded):
            raise ValueError('Catalog contains an escaped, aliased or CAD filename')
        result[prefix + name] = _hash(expected)
        folded.add(name.casefold())
    return result


def artifact_catalogs(
    artifact: Path, manifest_sha256: str, *, expected_kind: str,
) -> tuple[dict, dict[Path, dict[str, str]]]:
    """Read pinned metadata and derive full-root catalogs, without claiming a seal.

    The returned catalogs still need a fresh live session's full locked hash
    verification. This function alone never verifies runtime/application bytes.
    """
    if expected_kind not in ('converter', 'grader'):
        raise ValueError('Expected a converter or grader artifact kind')
    artifact = _plain_directory(artifact)
    manifest_path = artifact / 'manifest.json'
    info = manifest_path.lstat()
    if (manifest_path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
            or info.st_nlink != 1 or not manifest_path.is_file()):
        raise ValueError('Pinned manifest must be a plain owned file')
    if digest(manifest_path) != _hash(manifest_sha256):
        raise ValueError('Frozen manifest differs from the pinned identity')
    metadata = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (metadata.get('schema') != 1
            or metadata.get('python_magic') != importlib.util.MAGIC_NUMBER.hex()):
        raise ValueError('Unsupported snapshot or interpreter bytecode version')
    kind = metadata.get('artifact_kind', 'converter')
    if kind != expected_kind:
        raise ValueError('Frozen artifact kind differs from the required role')
    runtime = _plain_directory(Path(metadata['python_runtime']))
    if artifact == runtime or artifact.is_relative_to(runtime) or runtime.is_relative_to(artifact):
        raise ValueError('Application artifact and runtime roots must be separate')
    application = _catalog(metadata.get('application_files'), 'application', 'application/')
    entrypoint = ('application/converter.pyc' if kind == 'converter'
                  else 'application/grader.pyc')
    if entrypoint not in application or (kind == 'grader'
                                       and metadata.get('entrypoint') != entrypoint):
        raise ValueError('Frozen artifact lacks its declared neutral entrypoint')
    files = {'manifest.json': manifest_sha256, 'LICENSE': _hash(metadata['license_sha256']),
             **application}
    if kind == 'grader' or 'dependency_files' in metadata:
        declarations = _catalog(metadata.get('dependency_files'), 'dependency', 'dependencies/')
        if kind == 'grader' and not {'dependencies/pyproject.toml', 'dependencies/uv.lock'} <= (
                declarations.keys()):
            raise ValueError('Frozen grader lacks dependency declarations')
        files.update(declarations)
    if not isinstance(metadata.get('source_revision'), str) or not metadata['source_revision']:
        raise ValueError('Frozen artifact lacks its committed source revision')
    return metadata, {artifact: files, runtime: _catalog(metadata.get('runtime_files'), 'runtime')}


def converter_catalogs(artifact: Path, manifest_sha256: str) -> tuple[dict, dict]:
    return artifact_catalogs(artifact, manifest_sha256, expected_kind='converter')


def verify_locked_artifact(
    artifact: Path, manifest_sha256: str, session: SealedRuntimeSession, *, expected_kind: str,
) -> tuple[dict, str]:
    """Recheck a live seal covering every byte required by the original verifiers."""
    if type(session) is not SealedRuntimeSession:
        raise TypeError('Verification reuse requires the actual live SealedRuntimeSession')
    metadata, catalogs = artifact_catalogs(artifact, manifest_sha256, expected_kind=expected_kind)
    identity = session.require_catalogs(catalogs)
    return metadata, identity


def verify_locked_converter(
    artifact: Path, manifest_sha256: str, session: SealedRuntimeSession,
) -> tuple[dict, str]:
    return verify_locked_artifact(artifact, manifest_sha256, session, expected_kind='converter')
