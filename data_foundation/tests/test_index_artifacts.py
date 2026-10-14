"""Single-file publication integrity; synthetic bytes do not prove E indexing."""
from datetime import timedelta
from io import BytesIO
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from data_foundation import Conflict, InvalidArgument, JobLease, LeaseLost, NotFound
from data_foundation.artifacts import verify_artifact
from data_foundation.db.models import KnowledgeIndex
from data_foundation.repositories import KnowledgeRepository

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from worker_lifecycle_fixture import make_fixture


@pytest.fixture
def fx(store, tmp_path):
    store.data_root = tmp_path / 'managed'
    return store, make_fixture(store)


def activate(uow, f, **overrides):
    options = dict(document_id=f['document']['id'], version='test',
        artifact_path=f['artifact']['relative_path'], artifact_sha256=f['artifact']['sha256'],
        expected_artifact_byte_size=f['artifact']['byte_size'], expected_chunk_count=1,
        lease=f['knowledge_lease'])
    options.update(overrides)
    return uow.knowledge.activate_index(**options)


def assert_unpublished(store, f):
    with store.read() as uow:
        doc = uow.knowledge.get_document(f['document']['id'])
        assert doc['active_index_version'] is None
        assert uow.session.get(KnowledgeIndex, f['index']['id']).status == 'building'
        assert uow.jobs.get_job(f['knowledge_lease'].job_id)['status'] == 'running'


def test_publish_and_complete_same_transaction_read_rechecks(fx):
    store, f = fx
    with store.write() as uow:
        activate(uow, f)
        uow.jobs.complete_job(f['knowledge_lease'], {'index_id': f['index']['id']}, f['start'])
    with store.read() as uow:
        ref = uow.knowledge.get_index_artifact_ref(f['document']['id'])
        assert ref['status'] == 'ready'
        assert ref['sha256'] == f['artifact']['sha256']
        assert ref['byte_size'] == f['artifact']['byte_size']
        assert ref['version'] == 'test' and ref['index_id'] == f['index']['id']
        assert ref['parser_version'] == 'parser-test'
        assert ref['embedding_model_version'] == 'model-test'
        assert not Path(ref['relative_path']).is_absolute()
        assert uow.jobs.get_job(f['knowledge_lease'].job_id)['status'] == 'succeeded'


def test_legacy_signature_real_file_no_optional_size(fx):
    store, f = fx
    with store.write() as uow:
        activate(uow, f, expected_artifact_byte_size=None)
    with store.read() as uow:
        assert uow.knowledge.get_index_artifact_ref(f['document']['id'])['status'] == 'ready'


@pytest.mark.parametrize('sha', ['', ' ', None, True, 42, 'a'*63, 'g'*64, 'A'*64])
def test_invalid_digest_does_not_publish(fx, sha):
    store, f = fx
    with pytest.raises(InvalidArgument):
        with store.write() as uow:
            activate(uow, f, artifact_sha256=sha)
    assert_unpublished(store, f)


@pytest.mark.parametrize('size', [-1, True, False, 1.0, '1'])
def test_invalid_size_does_not_publish(fx, size):
    store, f = fx
    with pytest.raises(InvalidArgument):
        with store.write() as uow:
            activate(uow, f, expected_artifact_byte_size=size)
    assert_unpublished(store, f)


@pytest.mark.parametrize('path', ['', ' ', None, True, '../outside', '/absolute', 'raw/../escape',
    'raw//file', './raw/file', 'raw/./file', 'raw/file/', 'raw\\file', 'C:/file', 'raw/\0file', 'x'*501])
def test_invalid_path_does_not_publish(fx, path):
    store, f = fx
    with pytest.raises(InvalidArgument):
        with store.write() as uow:
            activate(uow, f, artifact_path=path)
    assert_unpublished(store, f)


@pytest.mark.parametrize('failure', ['missing', 'directory', 'digest', 'size', 'fifo', 'link_inside', 'link_outside', 'parent_link'])
def test_file_failure_does_not_publish(fx, tmp_path, failure):
    store, f = fx
    path = store.data_root / f['artifact']['relative_path']
    options = {}
    expected = Conflict
    if failure == 'missing':
        path.unlink(); expected = NotFound
    elif failure == 'directory':
        path.unlink(); path.mkdir(); expected = InvalidArgument
    elif failure == 'digest':
        path.write_bytes(b'x' * f['artifact']['byte_size'])
    elif failure == 'size':
        options['expected_artifact_byte_size'] = f['artifact']['byte_size'] + 1
    elif failure == 'fifo':
        path.unlink(); os.mkfifo(path); expected = InvalidArgument
    elif failure in {'link_inside', 'link_outside'}:
        target = (store.data_root / 'another.bin') if failure == 'link_inside' else tmp_path / 'outside.bin'
        target.write_bytes(path.read_bytes()); path.unlink(); path.symlink_to(target)
        expected = InvalidArgument
    else:
        link = store.data_root / 'alias'
        link.symlink_to(path.parent, target_is_directory=True)
        options['artifact_path'] = 'alias/' + path.name
        expected = InvalidArgument
    with pytest.raises(expected):
        with store.write() as uow:
            activate(uow, f, **options)
    assert_unpublished(store, f)


def test_root_is_required(fx):
    store, f = fx
    store.data_root = None
    with pytest.raises(InvalidArgument):
        with store.write() as uow:
            activate(uow, f)
    assert_unpublished(store, f)


def test_change_while_hashing_is_rejected(fx, monkeypatch):
    store, f = fx
    path = store.data_root / f['artifact']['relative_path']
    import data_foundation.artifacts as module
    original = module.os.fstat
    calls = 0
    def fstat(fd):
        nonlocal calls
        result = original(fd)
        calls += 1
        if calls == 1:
            path.write_bytes(b'changed-during-hash')
        return result
    monkeypatch.setattr(module.os, 'fstat', fstat)
    with pytest.raises(Conflict):
        with store.write() as uow:
            activate(uow, f, expected_artifact_byte_size=None)
    assert_unpublished(store, f)


@pytest.mark.parametrize('change', ['overwrite', 'replace_same_content', 'delete', 'symlink'])
def test_change_after_hash_before_lock_is_rejected(fx, monkeypatch, tmp_path, change):
    store, f = fx
    path = store.data_root / f['artifact']['relative_path']
    original = KnowledgeRepository._owned_index
    def mutate_then_lock(self, index_id, lease):
        data = path.read_bytes()
        if change == 'overwrite': path.write_bytes(b'x' * len(data))
        if change == 'replace_same_content':
            replacement = path.parent / 'replacement'
            replacement.write_bytes(data); replacement.replace(path)
        if change == 'delete': path.unlink()
        if change == 'symlink':
            target = tmp_path / 'outside'
            target.write_bytes(data); path.unlink(); path.symlink_to(target)
        return original(self, index_id, lease)
    monkeypatch.setattr(KnowledgeRepository, '_owned_index', mutate_then_lock)
    with pytest.raises((Conflict, NotFound, InvalidArgument)):
        with store.write() as uow:
            activate(uow, f)
    assert_unpublished(store, f)


@pytest.mark.parametrize('when', ['after_hash', 'before_update'])
def test_lease_expiration_during_publication_is_rejected(fx, monkeypatch, when):
    store, f = fx
    import data_foundation.repositories as module
    import data_foundation.artifacts as artifacts
    expired = (f['start'] + timedelta(seconds=3600)).replace(tzinfo=None)
    if when == 'after_hash':
        original = module.verify_artifact
        def expire(*args):
            value = original(*args)
            monkeypatch.setattr(module, 'utcnow_naive', lambda: expired)
            return value
        monkeypatch.setattr(module, 'verify_artifact', expire)
    else:
        original = artifacts.VerifiedArtifact.ensure_unchanged
        calls = 0
        def expire(self, root):
            nonlocal calls
            original(self, root); calls += 1
            if calls == 2:
                monkeypatch.setattr(module, 'utcnow_naive', lambda: expired)
        monkeypatch.setattr(artifacts.VerifiedArtifact, 'ensure_unchanged', expire)
    with pytest.raises(LeaseLost):
        with store.write() as uow:
            activate(uow, f)
    assert_unpublished(store, f)


def test_previous_active_version_survives_bad_new_artifact(fx):
    store, f = fx
    with store.write() as uow:
        activate(uow, f)
        next_index = uow.knowledge.begin_index(document_id=f['document']['id'], version='next',
            job_id=f['foreign_lease'].job_id, parser_version='p2', embedding_model_version='m2', lease=f['foreign_lease'])
        uow.knowledge.write_chunks(next_index['id'], [f['chunk']], f['foreign_lease'])
    with pytest.raises(Conflict):
        with store.write() as uow:
            activate(uow, f, version='next', lease=f['foreign_lease'], artifact_sha256='a'*64)
    with store.read() as uow:
        assert uow.knowledge.get_document(f['document']['id'])['active_index_version'] == 'test'
        assert uow.knowledge.get_index_artifact_ref(f['document']['id'])['version'] == 'test'
        assert uow.session.get(KnowledgeIndex, next_index['id']).status == 'building'


def test_completion_error_rolls_back_index_activation(fx):
    store, f = fx
    with pytest.raises(LeaseLost):
        with store.write() as uow:
            activate(uow, f)
            bad = JobLease(f['knowledge_lease'].job_id, f['knowledge_lease'].attempt_no, 'invalid-token')
            uow.jobs.complete_job(bad, {}, f['start'])
    assert_unpublished(store, f)


def test_outer_transaction_rolls_back_publication_and_completion(fx):
    store, f = fx
    with pytest.raises(RuntimeError):
        with store.write() as uow:
            activate(uow, f)
            uow.jobs.complete_job(f['knowledge_lease'], {}, f['start'])
            raise RuntimeError('caller failure')
    assert_unpublished(store, f)


@pytest.mark.parametrize('change', ['overwrite', 'delete', 'symlink'])
def test_read_rechecks_published_file(fx, tmp_path, change):
    store, f = fx
    with store.write() as uow: activate(uow, f)
    path = store.data_root / f['artifact']['relative_path']
    if change == 'overwrite': path.write_bytes(b'changed-after-publication')
    if change == 'delete': path.unlink()
    if change == 'symlink':
        target = tmp_path / 'outside'
        target.write_bytes(path.read_bytes()); path.unlink(); path.symlink_to(target)
    with pytest.raises((Conflict, NotFound, InvalidArgument)):
        with store.read() as uow:
            uow.knowledge.get_index_artifact_ref(f['document']['id'])
    # Reading does not silently unpublish a committed index or delete chunks.
    with store.read() as uow:
        assert uow.knowledge.get_chunks(f['document']['id'])['status'] == 'ready'


def test_unready_explicit_version_and_missing_document(fx):
    store, f = fx
    with store.read() as uow:
        assert uow.knowledge.get_index_artifact_ref(f['document']['id']) == {'status':'index_not_ready'}
        assert uow.knowledge.get_index_artifact_ref(f['document']['id'], 'unknown') == {'status':'index_not_ready'}
        with pytest.raises(NotFound): uow.knowledge.get_index_artifact_ref(str(uuid4()))
    with store.write() as uow: activate(uow, f)
    with store.read() as uow:
        assert uow.knowledge.get_index_artifact_ref(f['document']['id'], 'test')['status'] == 'ready'
        assert uow.knowledge.get_index_artifact_ref(f['document']['id'], 'unknown') == {'status':'index_not_ready'}


def test_readonly_cannot_activate(fx):
    store, f = fx
    with store.read() as uow:
        with pytest.raises(InvalidArgument): activate(uow, f)
    assert_unpublished(store, f)
