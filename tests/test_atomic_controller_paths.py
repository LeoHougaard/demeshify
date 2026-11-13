import errno
import json
from pathlib import Path

import pytest

from tools import frozen_evaluation_controller as controller


def test_atomic_cache_write_roundtrips_under_windows_path_limit(tmp_path, monkeypatch):
    name = "f" * 64 + ".json"
    padding = 245 - len(str(tmp_path)) - len(name) - 2
    assert padding > 0
    parent = tmp_path / ("p" * padding)
    destination = parent / name
    assert len(str(destination)) == 245
    original_open = Path.open
    observed = []

    def limited_open(path, *args, **kwargs):
        observed.append(str(path))
        if len(str(path)) >= 260:
            raise FileNotFoundError(errno.ENOENT, "Windows MAX_PATH", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", limited_open)
    payload = {"accepted": True, "evaluation_fingerprint": "a" * 64}
    controller._atomic(destination, payload, tmp_path, exclusive=True)
    assert json.loads(destination.read_text()) == payload
    assert all(len(path) < 260 for path in observed)
    assert list(parent.iterdir()) == [destination]

    with pytest.raises(FileExistsError):
        controller._atomic(destination, {"accepted": False}, tmp_path, exclusive=True)
    assert json.loads(destination.read_text()) == payload

    replacement = {"accepted": False, "status": "retained diagnostic"}
    controller._atomic(destination, replacement, tmp_path)
    assert json.loads(destination.read_text()) == replacement
