"""Prepare the real notice in an exclusive empty MySQL _test schema.

Only C storage preparation is verified. No E index, worker, or HTTP is simulated.
Never point this script at a shared/production database.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from data_foundation import AssetCreate, EntityCreate, JobCreate, SourceCreate, __version__, create_store
from data_foundation.artifacts import verify_artifact
from data_foundation.db.models import Job
from data_foundation.ingestion.real_dataset import validate_real_dataset
from de_handoff_smoke import migrate

DOCUMENT_ID = '0c8e2020-2e0a-5842-bce9-d9193cf81095'


def prepare(url, manifest_path, data_root, index_version):
    parsed = make_url(url)
    if parsed.drivername != 'mysql+pymysql' or not (parsed.database or '').endswith('_test'):
        raise ValueError('Only an exclusive empty MySQL schema ending in _test is allowed')
    engine = create_engine(url)
    try:
        if inspect(engine).get_table_names():
            raise ValueError('Schema must be empty; existing data is never deleted')
    finally:
        engine.dispose()
    validate_real_dataset(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    document = next(d for d in manifest['documents'] if d['id'] == DOCUMENT_ID)
    asset = next(a for a in manifest['assets'] if a['key'] == document['asset_key'])
    source = next(s for s in manifest['catalog']['sources'] if s['id'] == document['source_id'])
    entities = [e for e in manifest['catalog']['entities'] if e['id'] in document['entity_ids']]
    migrate(url)
    store = create_store(url, data_root=data_root)
    now = datetime.now(timezone.utc)
    try:
        with store.write() as uow:
            for entity in entities:
                uow.catalog.create_entity(EntityCreate(**entity))
            values = dict(source)
            for key in ('coverage_start', 'coverage_end'):
                if isinstance(values.get(key), str):
                    values[key] = date.fromisoformat(values[key])
            uow.provenance.register_source(SourceCreate(**values))
        # The native C persistence operation is deliberately outside a write UOW.
        with (manifest_path.parent / asset['path']).open('rb') as stream:
            persisted = store.persist_file(source_id=asset['source_id'], original_name=asset['path'], stream=stream)
        with store.write() as uow:
            stored_asset = uow.provenance.register_asset(AssetCreate(
                source_id=asset['source_id'], url=asset['url'], title=asset['title'],
                retrieved_at=datetime.fromisoformat(asset['retrieved_at']),
                published_at=datetime.fromisoformat(asset['published_at']),
                relative_path=persisted['relative_path'], sha256=persisted['sha256'],
                media_type=asset['media_type'], byte_size=persisted['byte_size'], usage_note=asset['usage_note']))
            values = {k: document[k] for k in
                      ('id', 'source_id', 'title', 'publisher', 'source_url', 'permission_note', 'document_version')}
            values['asset_id'] = stored_asset['id']
            for key in ('published_at', 'valid_from', 'valid_until'):
                values[key] = datetime.fromisoformat(document[key]) if document.get(key) else None
            uow.knowledge.register_document(values, document['entity_ids'])
            key = sha256(json.dumps([DOCUMENT_ID, document['document_version'], index_version],
                                    ensure_ascii=False).encode('utf-8')).hexdigest()
            request = JobCreate('knowledge_index', key, {
                'document_id': DOCUMENT_ID, 'document_version': document['document_version'],
                'source_id': source['id'], 'asset_id': stored_asset['id'],
                'index_version': index_version, 'parser_version': 'text-parser-0.5.0',
                'embedding_model_version': 'keyword-tfidf-0.5.0',
            }, '真实历史公告C独立存储准备；待D/E正式处理')
            job = uow.jobs.enqueue_or_get(request, now)
            assert uow.jobs.enqueue_or_get(request, now)['id'] == job['id']
    finally:
        store.dispose()
    # A new store/engine must load committed provenance and the queued job.
    store = create_store(url, data_root=data_root)
    try:
        with store.read() as uow:
            loaded = uow.knowledge.get_document(DOCUMENT_ID)
            queued = uow.jobs.get_job(job['id'])
            head = uow.session.scalar(text('SELECT version_num FROM alembic_version'))
            mysql_version = uow.session.scalar(text('SELECT VERSION()'))
            assert loaded['asset_id'] == stored_asset['id']
            assert loaded['document_version'] == document['document_version']
            assert queued['status'] == 'queued'
            # get_job deliberately omits the internal payload from its public DTO.
            assert uow.session.get(Job, job['id']).payload['document_id'] == DOCUMENT_ID
            assert uow.knowledge.get_index_artifact_ref(DOCUMENT_ID)['status'] == 'index_not_ready'
        verify_artifact(store.data_root, persisted['relative_path'], asset['sha256'], asset['byte_size'])
        return {
            'status': 'passed', 'scope': 'c_independent_mysql_real_notice_preparation',
            'package_version': __version__, 'python_version': platform.python_version(),
            'os': platform.platform(), 'mysql_version': mysql_version, 'migration_head': head,
            'document_id': DOCUMENT_ID, 'document_version': loaded['document_version'],
            'source_id': source['id'], 'asset_id': stored_asset['id'], 'asset_sha256': asset['sha256'],
            'source_url': document['source_url'], 'asset_key': asset['key'],
            'retention_form': 'official_browser_transcription_txt_not_original_html',
            'published_at': document['published_at'], 'valid_from': document['valid_from'],
            'valid_until': document['valid_until'], 'job_id': job['id'], 'job_status': queued['status'],
            'requested_index_version': index_version, 'index_id': None,
            'checks': ['native_original_persistence', 'registered_real_document_version',
                       'mysql_queued_job_idempotency', 'new_engine_rereads_document_and_job'],
            'windows_receiving': 'pending', 'formal_DE_chain': 'pending', 'HTTP_verified': False,
            'verified_at': now.isoformat(),
        }
    finally:
        store.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--index-version', default='c020a2-historical-handoff-20261014')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        raise SystemExit('TEST_DATABASE_URL must identify an exclusive empty MySQL _test schema')
    report = prepare(url, args.manifest.resolve(), args.data_root.resolve(), args.index_version)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
