"""Runnable single-file storage handoff; synthetic bytes, no E vector model."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_foundation import Conflict, __version__, create_store
from data_foundation.db import Base
from worker_lifecycle_fixture import make_fixture


def main():
    with TemporaryDirectory(prefix='c-index-artifact-smoke-') as temporary:
        root = Path(temporary)
        store = create_store(f'sqlite+pysqlite:///{root / "test.sqlite"}',
            data_root=root / 'managed', allow_sqlite_for_tests=True)
        try:
            Base.metadata.create_all(store.engine)
            f = make_fixture(store)
            # E constructs and persists files before the short publication txn.
            # f['artifact'] is produced with store.persist_file, content-addressed.
            artifact = f['artifact']
            with store.write() as uow:
                uow.knowledge.activate_index(document_id=f['document']['id'], version='test',
                    artifact_path=artifact['relative_path'], artifact_sha256=artifact['sha256'],
                    expected_artifact_byte_size=artifact['byte_size'], expected_chunk_count=1,
                    lease=f['knowledge_lease'])
                uow.jobs.complete_job(f['knowledge_lease'],
                    {'document_id':f['document']['id'], 'index_version':'test'}, f['start'])
            with store.read() as uow:
                # Internal E loading reference. D must not expose the path over HTTP.
                reference = uow.knowledge.get_index_artifact_ref(f['document']['id'])
                assert reference['sha256'] == artifact['sha256']
                assert reference['byte_size'] == artifact['byte_size']
                assert uow.jobs.get_job(f['knowledge_lease'].job_id)['status'] == 'succeeded'
            (store.data_root / artifact['relative_path']).write_bytes(b'corrupted')
            try:
                with store.read() as uow:
                    uow.knowledge.get_index_artifact_ref(f['document']['id'])
            except Conflict: pass
            else: raise AssertionError('corrupted artifact accepted')
            print(json.dumps({'status':'passed', 'package_version': __version__,
                'checks':['actual_file_publish_and_job_complete','internal_read_rehash','corrupted_file_rejected'],
                'scope':'synthetic_sqlite_single_file_storage',
                'DE_receiving_acceptance':'pending', 'vector_format_verified':False,
                'verified_at':datetime.now(timezone.utc).isoformat()}))
        finally: store.dispose()


if __name__ == '__main__': main()
