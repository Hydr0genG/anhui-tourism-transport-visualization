from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import EntityCreate, MetricCreate, SourceCreate
from .db.models import Entity, Source
from .store import DataStore


DEFAULT_ASSET_DIR = Path(__file__).resolve().parent / "defaults"


def load_asset(name: str, asset_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(asset_dir) if asset_dir else DEFAULT_ASSET_DIR
    return json.loads((root / name).read_text(encoding="utf-8"))


def seed_catalog(store: DataStore, asset_dir: str | Path | None = None) -> dict[str, int]:
    metrics_doc = load_asset("metric_dictionary.json", asset_dir)
    entities_doc = load_asset("entity_registry.json", asset_dir)
    sources_doc = load_asset("source_registry.json", asset_dir)
    counts = {"metrics": 0, "entities": 0, "sources": 0}
    with store.write() as uow:
        for item in metrics_doc["metrics"]:
            uow.catalog.upsert_metric(MetricCreate(**item))
            counts["metrics"] += 1
        for item in entities_doc["entities"]:
            if uow.session.get(Entity, item["id"]):
                continue
            uow.catalog.create_entity(EntityCreate(**item))
            counts["entities"] += 1
        for item in sources_doc["sources"]:
            if uow.session.get(Source, item["id"]):
                continue
            uow.provenance.register_source(SourceCreate(**item))
            counts["sources"] += 1
    return counts
