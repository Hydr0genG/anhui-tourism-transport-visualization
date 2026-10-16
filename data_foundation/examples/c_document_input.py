"""Authorized internal original-document reference using unchanged C a2 APIs.

This is a C-owned integration example, not a new method on the released SDK.
D supplies its server-side document allowlist and must not expose this internal
reference over HTTP. A verified reference does not lock a file for later use;
consumers must keep immutable inputs and reverify bytes before indexing.
"""
from pathlib import Path

from data_foundation import InvalidPermission
from data_foundation.artifacts import verify_artifact


def get_document_input_ref(store, document_id, *, authorized_document_ids):
    if document_id not in frozenset(authorized_document_ids):
        raise InvalidPermission("未授权的知识原文")
    with store.read() as uow:
        document = uow.knowledge.get_document(document_id)
        asset = uow.provenance.get_asset(document["asset_id"], internal=True)
    verified = verify_artifact(store.data_root, asset["relative_path"], asset["sha256"], asset["byte_size"])
    verified.ensure_unchanged(store.data_root)
    return {"document_id": document_id, "document_version": document["document_version"],
            "source_id": document["source_id"], "asset_id": asset["id"],
            "sha256": verified.sha256, "byte_size": verified.byte_size,
            "path": str(Path(store.data_root) / verified.relative_path),
            "relative_path": verified.relative_path, "media_type": asset["media_type"]}
