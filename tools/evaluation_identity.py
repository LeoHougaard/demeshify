"""Content identities for resumable, progressively expanded reconstruction runs."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

EVALUATION_TOOLS = (
    'benchmark_multires_corpus.py', 'benchmark_surface_corpus.py',
    'analyze_surface_failures.py', 'process_resources.py', 'evaluation_identity.py',
    'multires_summary.py',
    'geometry_dimensions.py',
    'brep_topology.py',
)


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_identity(root: Path) -> dict[str, str]:
    paths = [*sorted((root / 'app').glob('*.py')),
             *(root / 'tools' / name for name in EVALUATION_TOOLS),
             root / 'pyproject.toml', root / 'uv.lock']
    return {str(path.relative_to(root)): digest(path) for path in paths}


def runtime_identity() -> dict:
    executables = {Path(sys.executable).resolve()}
    base = Path(sys.base_prefix)
    if sys.platform == 'win32':
        executables.add(base / 'python.exe')
        executables.update(base.glob('python*.dll'))
    return {
        'python': sys.version, 'platform': sys.platform,
        'executables': {str(p): digest(p) for p in sorted(executables)},
        'distributions': sorted([d.metadata['Name'], d.version]
                                for d in importlib.metadata.distributions()),
        'thread_pools': {name: os.environ.get(name) for name in (
            'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
            'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS',
        )},
    }


def case_identity(case: dict, sources: dict, runtime: dict) -> str:
    # The controller verifies both geometry files against these declared hashes
    # before looking up a result. The manifest's unrelated rows and its filename
    # do not affect this case's conversion or grading.
    payload = {'schema': 2, 'case': case, 'sources': sources, 'runtime': runtime,
               'conversion_timeout': 240, 'case_timeout': 360}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def cached_result(path: Path, fingerprint: str) -> dict | None:
    if not path.exists():
        return None
    result = json.loads(path.read_text())
    if result.get('evaluation_fingerprint') != fingerprint:
        raise ValueError('Cached result has a different identity; preserve it and use a new run')
    return result
