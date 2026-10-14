"""Independent MySQL single-file publication races; synthetic index bytes."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from data_foundation import Conflict, JobLease, LeaseLost, __version__, create_store
from data_foundation.db.models import Job, KnowledgeIndex
import data_foundation.repositories as repositories
from de_handoff_smoke import migrate
from worker_lifecycle_fixture import make_fixture


def verify(url, data_root):
    parsed = make_url(url)
    if parsed.drivername != 'mysql+pymysql' or not (parsed.database or '').endswith('_test'):
        raise ValueError('Only an exclusive empty MySQL schema ending in _test is allowed')
    engine = create_engine(url)
    try:
        if inspect(engine).get_table_names():
            raise ValueError('Test schema must be empty; existing data is never deleted')
    finally:
        engine.dispose()
    migrate(url)
    store = create_store(url, data_root=data_root)
    checks = []
    def fixture():
        f = make_fixture(store)
        with store.write() as uow:
            # Avoid unrelated synthetic tasks becoming recovery candidates.
            for name in ('video_lease', 'foreign_lease'):
                uow.jobs.complete_job(f[name], {'synthetic_unused': True}, f['start'])
        return f
    def connection(uow):
        uow.session.execute(text('SET SESSION innodb_lock_wait_timeout = 5'))
        return uow.session.scalar(text('SELECT CONNECTION_ID()'))
    def publish(uow, f, lease=None):
        return uow.knowledge.activate_index(document_id=f['document']['id'], version='test',
            artifact_path=f['artifact']['relative_path'], artifact_sha256=f['artifact']['sha256'],
            expected_artifact_byte_size=f['artifact']['byte_size'], expected_chunk_count=1,
            lease=lease or f['knowledge_lease'])
    def passed(name): checks.append({'name': name, 'status': 'passed'})
    def assert_state(f, index_status, job_status):
        with store.read() as uow:
            assert uow.session.get(KnowledgeIndex, f['index']['id']).status == index_status
            assert uow.jobs.get_job(f['knowledge_lease'].job_id)['status'] == job_status
    try:
        # The publisher has hashed the file before waiting on the held Job lock.
        # Mutating the file while it waits must abort publication after the wait.
        f = fixture()
        waiting, finished = Event(), Event()
        connection_ids = []
        def before_lock(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('SELECT jobs.') and 'FOR UPDATE' in statement:
                waiting.set()
        def blocked_publish():
            with store.write() as uow:
                connection_ids.append(connection(uow))
                event.listen(uow.session.connection(), 'before_cursor_execute', before_lock)
                try:
                    publish(uow, f)
                except Conflict:
                    return 'conflict'
                finally:
                    finished.set()
                return 'unexpected_success'
        with ThreadPoolExecutor(max_workers=1) as pool:
            with store.write() as holder:
                holder_id = connection(holder)
                holder.jobs.ensure_lease(f['knowledge_lease'], f['start'])
                future = pool.submit(blocked_publish)
                assert waiting.wait(5), 'publisher did not reach post-hash Job lock'
                assert not finished.wait(0.1), 'publisher did not wait for Job lock'
                path = store.data_root / f['artifact']['relative_path']
                path.write_bytes(b'x' * f['artifact']['byte_size'])
            assert future.result(timeout=7) == 'conflict'
        assert connection_ids[0] != holder_id
        assert_state(f, 'building', 'running')
        # Finish this task so later artificial recovery cutoffs do not select it.
        with store.write() as uow: uow.jobs.fail_job(f['knowledge_lease'], 'test artifact changed', f['start'])
        passed('file_change_while_waiting_for_job_lock_aborts_publication')

        # Hashing does not take Job/Attempt locks; recovery can commit meanwhile.
        f = fixture()
        hashed, proceed = Event(), Event()
        original_verify = repositories.verify_artifact
        def pause_after_hash(*args):
            artifact = original_verify(*args)
            hashed.set()
            assert proceed.wait(5)
            return artifact
        def old_publisher():
            with store.write() as uow:
                publisher_id = connection(uow)
                uow.jobs.get_job(f['knowledge_lease'].job_id)  # old REPEATABLE READ snapshot
                try: publish(uow, f)
                except LeaseLost: return publisher_id, 'lease_lost'
                return publisher_id, 'unexpected_success'
        due = f['start'] + timedelta(seconds=3600)
        with patch.object(repositories, 'verify_artifact', pause_after_hash):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(old_publisher)
                assert hashed.wait(5)
                try:
                    with store.write() as recovery:
                        recovery_id = connection(recovery)
                        assert recovery.jobs.recover_expired(due) == {'requeued': 1, 'failed': 0}
                        current = JobLease(**recovery.jobs.claim_job('new-worker', due, 3600,
                            job_id=f['knowledge_lease'].job_id)['lease'])
                finally: proceed.set()
                publisher_id, result = future.result(timeout=7)
                assert publisher_id != recovery_id and result == 'lease_lost'
        assert_state(f, 'building', 'running')
        with store.write() as uow:
            publish(uow, f, current)
            uow.jobs.complete_job(current, {'index_id': f['index']['id']}, due)
        assert_state(f, 'ready', 'succeeded')
        passed('recovery_during_hash_rejects_stale_cached_publisher_new_lease_succeeds')

        f = fixture()
        due = f['start'] + timedelta(seconds=3600)
        with store.write() as publisher:
            publisher_id = connection(publisher)
            publish(publisher, f)
            with store.write() as recovery:
                assert connection(recovery) != publisher_id
                assert recovery.jobs.recover_expired(due) == {'requeued': 0, 'failed': 0}
            publisher.jobs.complete_job(f['knowledge_lease'], {'index_id': f['index']['id']}, f['start'])
        assert_state(f, 'ready', 'succeeded')
        passed('publisher_locks_prevent_requeue_and_commit_index_and_job_together')

        f = fixture()
        waiting, finished = Event(), Event()
        def second_publish():
            with store.write() as uow:
                second_id = connection(uow)
                event.listen(uow.session.connection(), 'before_cursor_execute', before_lock)
                try: publish(uow, f)
                except LeaseLost: return second_id, 'lease_lost'
                finally: finished.set()
                return second_id, 'unexpected_success'
        with ThreadPoolExecutor(max_workers=1) as pool:
            with store.write() as first:
                first_id = connection(first)
                publish(first, f)
                future = pool.submit(second_publish)
                assert waiting.wait(5)
                assert not finished.wait(0.1)
                first.jobs.complete_job(f['knowledge_lease'], {}, f['start'])
            second_id, result = future.result(timeout=7)
            assert second_id != first_id and result == 'lease_lost'
        with store.read() as uow:
            assert uow.knowledge.get_document(f['document']['id'])['row_version'] == f['document']['row_version'] + 1
        passed('two_publishers_only_one_commits_after_current_lease_recheck')

        f = fixture()
        try:
            with store.write() as uow:
                publish(uow, f)
                uow.jobs.complete_job(f['knowledge_lease'], {}, f['start'])
                raise RuntimeError('caller rollback')
        except RuntimeError: pass
        assert_state(f, 'building', 'running')
        with store.read() as uow:
            assert uow.knowledge.get_document(f['document']['id'])['active_index_version'] is None
        with store.write() as uow:
            publish(uow, f)
            uow.jobs.complete_job(f['knowledge_lease'], {}, f['start'])
        passed('outer_rollback_keeps_index_and_job_unpublished_then_retry_succeeds')

        path = store.data_root / f['artifact']['relative_path']
        path.write_bytes(b'x' * f['artifact']['byte_size'])
        with store.read() as uow:
            try: uow.knowledge.get_index_artifact_ref(f['document']['id'])
            except Conflict: pass
            else: raise AssertionError('published corrupted file was returned')
        assert_state(f, 'ready', 'succeeded')
        passed('read_rehash_rejects_corruption_without_deleting_committed_results')
        with store.read() as uow:
            mysql_version = uow.session.scalar(text('SELECT VERSION()'))
            head = uow.session.scalar(text('SELECT version_num FROM alembic_version'))
        return {'status':'passed', 'package_version': __version__, 'migration_head': head,
            'mysql_version': mysql_version, 'checks': checks, 'check_count': len(checks),
            'scope': 'isolated_mysql_synthetic_single_file_integrity',
            'verified_at': datetime.now(timezone.utc).isoformat(), 'DE_receiving_acceptance': 'pending',
            'limitations': ['Not E vector-format or chunk-to-vector semantic validation',
                'No atomic filesystem/database commit or production throughput claim',
                'File may change after the last check; callers must manage immutable files and recheck before loading']}
    finally: store.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        url = os.environ.get('TEST_DATABASE_URL')
        if not url: raise ValueError('Set TEST_DATABASE_URL for an exclusive empty test schema')
        report = verify(url, args.data_root)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({'status': report['status'], 'checks': report['check_count']}))
    except Exception as exc:
        print(json.dumps({'status':'failed','error_type': type(exc).__name__}))
        raise SystemExit(1)
