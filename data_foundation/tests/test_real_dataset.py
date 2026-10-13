from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from data_foundation import InvalidArgument, NotFound, Page, SeriesQuery
from data_foundation.db.models import KnowledgeDocument, Observation, Source, SourceAsset
from data_foundation.ingestion.real_dataset import import_real_dataset, validate_real_dataset
from data_foundation.seed import seed_catalog
from conftest import SCENIC_ID, SOURCE_ID, register_asset


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "real_data/2026-10-12/manifest.json"


def test_document_details_pagination_and_entity_filter(store):
    asset_id = register_asset(store)
    with store.write() as uow:
        first = uow.knowledge.register_document({
            "source_id": SOURCE_ID, "asset_id": asset_id, "title": "历史公告",
            "publisher": "测试单位", "source_url": "https://example.invalid/notice",
            "published_at": datetime(2025, 1, 24, tzinfo=timezone.utc),
            "valid_from": datetime(2025, 1, 28, tzinfo=timezone.utc),
            "valid_until": datetime(2025, 2, 4, tzinfo=timezone.utc),
            "permission_note": "测试", "document_version": "v1",
        }, [SCENIC_ID])
        uow.knowledge.register_document({
            "source_id": SOURCE_ID, "asset_id": asset_id, "title": "另一个实体资料",
            "publisher": "测试单位", "source_url": "https://example.invalid/other",
            "published_at": datetime(2026, 1, 24, tzinfo=timezone.utc),
            "permission_note": "测试", "document_version": "v2",
        })
    with store.read() as uow:
        document = uow.knowledge.get_document(first["id"])
        assert document["entity_ids"] == [SCENIC_ID]
        assert document["valid_until"].date() == date(2025, 2, 4)
        assert "relative_path" not in document and "text_path" not in document
        page = uow.knowledge.list_documents(entity_id=SCENIC_ID, page=Page(1, 1))
        assert page["total"] == 1 and page["items"][0]["id"] == first["id"]
        assert uow.knowledge.list_documents(page=Page(3, 1))["items"] == []
        assert uow.knowledge.list_documents(page=Page(1, 1))["total"] == 2
        with pytest.raises(NotFound):
            uow.knowledge.get_document("missing")
        with pytest.raises(NotFound):
            uow.knowledge.list_documents(entity_id="missing")


def test_real_snapshot_import_is_idempotent_and_exact_source_is_queryable(store, tmp_path):
    store.data_root = tmp_path / "raw"
    manifest = json.loads(MANIFEST.read_text())
    assert validate_real_dataset(MANIFEST)["observation_count"] >= 80
    first = import_real_dataset(store, MANIFEST)
    assert first["counts"]["quarantined"] == 0
    assert first["counts"]["imported"] == len(manifest["observations"])
    second = import_real_dataset(store, MANIFEST)
    assert second["counts"]["imported"] == second["counts"]["revised"] == 0
    assert second["documents"]["inserted"] == 0
    assert second["documents"]["skipped"] == len(manifest["documents"])
    hfe_id = next(e["id"] for e in manifest["catalog"]["entities"] if e["name"] == "合肥新桥机场")
    rail_id = next(e["id"] for e in manifest["catalog"]["entities"] if e["type"] == "rail_station")
    with store.read() as uow:
        series = uow.observations.query_series(SeriesQuery(hfe_id, "airport_throughput", "month", date(2025, 1, 1), date(2025, 1, 31)))
        assert series["points"][0]["value"] == Decimal("1111644")
        assert series["points"][0]["provenance"]["asset_url"].endswith("P020250224343413036795.pdf")
        asset_id = series["points"][0]["provenance"]["asset_id"]
        asset = uow.provenance.get_asset(asset_id, internal=True)
        assert hashlib.sha256((store.data_root / asset["relative_path"]).read_bytes()).hexdigest() == asset["sha256"]
        assert uow.session.scalar(select(func.count()).select_from(Observation)) == len(manifest["observations"])
        assert uow.session.scalar(select(func.count()).select_from(KnowledgeDocument)) == len(manifest["documents"])
        assert uow.session.scalar(select(func.count()).select_from(SourceAsset)) == len(manifest["assets"])
        assert uow.session.scalar(select(func.count()).select_from(Observation).where(Observation.entity_id == rail_id)) == 1
        assert all(d["index_status"] == "pending" for d in uow.knowledge.list_documents()["items"])


def test_tampered_original_rejected_before_any_database_write(store, tmp_path):
    package = tmp_path / "package"
    shutil.copytree(MANIFEST.parent, package)
    manifest = json.loads((package / "manifest.json").read_text())
    asset = manifest["assets"][0]
    (package / asset["path"]).write_bytes(b"tampered")
    store.data_root = tmp_path / "raw"
    with pytest.raises(InvalidArgument, match="摘要"):
        import_real_dataset(store, package / "manifest.json")
    with store.read() as uow:
        assert uow.session.scalar(select(func.count()).select_from(Observation)) == 0
        assert uow.session.scalar(select(func.count()).select_from(SourceAsset)) == 0


@pytest.mark.parametrize("change", ["conversion", "period", "qualifier", "path"])
def test_invalid_real_package_is_rejected(tmp_path, change):
    # 共享原件副本；每个变体只改清单，防止不一致或错误单位进入正式库。
    package = tmp_path / "package"
    shutil.copytree(MANIFEST.parent, package)
    path = package / "manifest.json"
    manifest = json.loads(path.read_text())
    if change == "conversion":
        manifest["observations"][0]["conversion_factor"] = "10000"
    elif change == "period":
        manifest["observations"][0]["period_start"] = "2023-01-02"
    elif change == "qualifier":
        manifest["observations"][0]["qualifier"] = "预计"
    else:
        manifest["assets"][0]["path"] = "../outside.html"
    path.write_text(json.dumps(manifest))
    with pytest.raises(InvalidArgument):
        validate_real_dataset(path)


def test_catalog_seed_parses_iso_dates(store, tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    shutil.copy(ROOT / "assets/metric_dictionary.json", catalog)
    (catalog / "entity_registry.json").write_text('{"entities": []}')
    source_id = "00000000-0000-0000-0000-000000000990"
    (catalog / "source_registry.json").write_text(json.dumps({"sources": [{
        "id": source_id, "publisher": "测试", "title": "日期核验", "url": "https://example.invalid/dates",
        "usage_note": "测试", "update_frequency": "monthly", "coverage_start": "2023-01-01", "coverage_end": "2026-08-31",
    }]}))
    seed_catalog(store, catalog)
    with store.read() as uow:
        assert uow.catalog.get_source(source_id)["coverage_start"] == date(2023, 1, 1)


def test_pdf_parser_distinguishes_current_column_and_annual_continuation():
    pytest.importorskip("pdfplumber")
    pytest.importorskip("lxml")
    sys.path.insert(0, str(ROOT / "examples"))
    from prepare_real_dataset import airport_rows
    root = MANIFEST.parent / "extracted"
    monthly = (root / "caac-2024-02-pdf-0.txt").read_text()
    parsed = {r["code"]: r for r in airport_rows(monthly, month=2)}
    assert parsed["HFE"]["original_value"] == "1161266"
    assert parsed["HFE"]["cumulative_value"] == "2121435"
    with pytest.raises(ValueError, match="月份"):
        airport_rows(monthly, month=1)
    annual = airport_rows((root / "caac-2024-annual-pdf-0.txt").read_text(), month=None)
    txn = next(r for r in annual if r["code"] == "TXN")
    assert txn["page"] == 2 and txn["original_value"] == "422573"
