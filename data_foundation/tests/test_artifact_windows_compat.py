"""Portable regressions; Windows simulation is not Windows acceptance."""
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_foundation import Conflict, InvalidArgument, NotFound
from data_foundation.artifacts import verify_artifact


@pytest.fixture
def artifact(tmp_path):
    data = b'{"schema_version":1,"chunks":[]}'
    path = tmp_path / 'index.json'
    path.write_bytes(data)
    return tmp_path, path, sha256(data).hexdigest(), len(data)


def test_reopen_uses_fstat_not_path_ctime(artifact, monkeypatch):
    root, path, digest, size = artifact
    original = Path.stat
    def windows_stat(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self == path:
            # Simulate path.stat creation-time vs fstat change-time disagreement.
            return SimpleNamespace(st_mode=result.st_mode, st_ctime_ns=0)
        return result
    monkeypatch.setattr(Path, 'stat', windows_stat)
    verified = verify_artifact(root, path.name, digest, size)
    verified.ensure_unchanged(root)


@pytest.mark.parametrize('field', ['st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'])
def test_every_fingerprint_field_is_rechecked(artifact, monkeypatch, field):
    root, path, digest, size = artifact
    verified = verify_artifact(root, path.name, digest, size)
    original = os.fstat
    def changed(fd):
        result = original(fd)
        values = {name: getattr(result, name) for name in
                  ['st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns']}
        values[field] += 1
        return SimpleNamespace(**values)
    monkeypatch.setattr(os, 'fstat', changed)
    with pytest.raises(Conflict):
        verified.ensure_unchanged(root)


@pytest.mark.parametrize('failure', ['fstat', 'fdopen', 'read', 'success'])
def test_open_descriptors_always_close(artifact, monkeypatch, failure):
    root, path, digest, size = artifact
    opened, closed = [], []
    original_open, original_close = os.open, os.close
    def tracked_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def tracked_close(fd):
        closed.append(fd)
        return original_close(fd)
    def denied(*args, **kwargs):
        raise PermissionError('simulated read failure')
    monkeypatch.setattr(os, 'open', tracked_open)
    monkeypatch.setattr(os, 'close', tracked_close)
    if failure == 'fstat':
        monkeypatch.setattr(os, 'fstat', denied)
    elif failure == 'fdopen':
        monkeypatch.setattr(os, 'fdopen', denied)
    elif failure == 'read':
        original_fdopen = os.fdopen
        class Unreadable:
            def __init__(self, fd, *args, **kwargs):
                self.stream = original_fdopen(fd, *args, **kwargs)
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            read = denied
        monkeypatch.setattr(os, 'fdopen', Unreadable)
    if failure == 'success':
        verified = verify_artifact(root, path.name, digest, size)
        verified.ensure_unchanged(root)
        assert len(opened) == 3
    else:
        with pytest.raises(Conflict):
            verify_artifact(root, path.name, digest, size)
    assert opened == closed


def test_windows_directory_permission_error_is_invalid_argument(artifact, monkeypatch):
    root, path, digest, size = artifact
    path.unlink()
    path.mkdir()
    def windows_open(*args): raise PermissionError('Windows directory open')
    monkeypatch.setattr(os, 'open', windows_open)
    with pytest.raises(InvalidArgument):
        verify_artifact(root, path.name, digest, size)


def test_directory_replacement_during_open_is_invalid_argument(artifact, monkeypatch):
    root, path, digest, size = artifact
    def replaced(*args):
        path.unlink()
        path.mkdir()
        raise PermissionError('Windows directory open')
    monkeypatch.setattr(os, 'open', replaced)
    with pytest.raises(InvalidArgument):
        verify_artifact(root, path.name, digest, size)


@pytest.mark.parametrize('failure', ['permission', 'missing'])
def test_regular_file_open_error_classification(artifact, monkeypatch, failure):
    root, path, digest, size = artifact
    def denied(*args):
        raise PermissionError('regular file denied')
    if failure == 'missing':
        path.unlink()
        expected = NotFound
    else:
        monkeypatch.setattr(os, 'open', denied)
        expected = Conflict
    with pytest.raises(expected):
        verify_artifact(root, path.name, digest, size)


def test_native_persist_file_closes_before_atomic_rename(store, tmp_path, monkeypatch):
    store.data_root = tmp_path / 'managed'
    original = os.replace
    renamed = []
    def checked_replace(source, target):
        # This native rename fails on Windows if the NamedTemporaryFile is open.
        original(source, target)
        renamed.append(Path(target))
    monkeypatch.setattr(os, 'replace', checked_replace)
    payload = b'{"schema_version":1,"chunks":[]}'
    result = store.persist_file(source_id='windows-receiving', original_name='index.json', stream=BytesIO(payload))
    assert renamed == [store.data_root / result['relative_path']]
    assert renamed[0].read_bytes() == payload
    verify_artifact(store.data_root, result['relative_path'], result['sha256'], result['byte_size'])
