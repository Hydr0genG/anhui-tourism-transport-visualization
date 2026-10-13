"""经核验的离线真实数据包：先完整校验，再显式写入。

默认验证不创建数据库、导入批次或原件。年度等补充事实保留在包中，
不冒充普通 day/week/month 观测。网络获取和 PDF 解析在准备阶段完成。
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..contracts import AssetCreate, EntityCreate, MetricCreate, SourceCreate
from ..db.models import Entity, Source
from ..errors import Conflict, InvalidArgument, NotFound
from ..store import DataStore
from .standard_csv import REQUIRED_COLUMNS, import_standard_csv


def package_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise InvalidArgument("资料包路径越出根目录", path=relative)
    if not path.is_file():
        raise InvalidArgument("资料包文件不存在", path=relative)
    return path


def verify_file(root: Path, item: dict[str, Any], field: str = "path", digest_field: str = "sha256") -> Path:
    path = package_path(root, item[field])
    if hashlib.sha256(path.read_bytes()).hexdigest() != item[digest_field]:
        raise InvalidArgument("资料包摘要不一致", path=item[field])
    return path


def validate_public_definitions(root, manifest, assets, entities):
    """所有扩展定义先离线核验；普通观测依旧只接纳明确日/月实绩。"""
    holidays = []
    if manifest.get("browser_captures"):
        captures = json.loads(verify_file(root, manifest["browser_captures"]).read_text())["captures"]
        for capture in captures:
            asset = assets[capture["key"]]
            snapshot = package_path(root, asset["text_path"]).read_text()
            if (asset.get("capture_method") != "official_browser_visible_text_transcription"
                    or asset["media_type"] != "text/plain" or capture["text"] not in snapshot
                    or any(capture[k] != asset[k] for k in ("title", "url", "published_at", "capture_scope"))):
                raise InvalidArgument("浏览器正文快照与采录记录不一致")
    if manifest.get("holiday_calendar"):
        item = manifest["holiday_calendar"]
        calendar_data = json.loads(verify_file(root, item).read_text())
        holidays = calendar_data["holidays"]
        if len(holidays) != item["row_count"]:
            raise InvalidArgument("假期定义数量不一致")
        ids, keys, covered_days = set(), set(), set()
        for holiday in holidays:
            asset = assets[holiday["asset_key"]]
            start, end = (date.fromisoformat(holiday[k]) for k in ("start_date", "end_date"))
            text = re.sub(r"\s+", "", package_path(root, asset["text_path"]).read_text())
            excerpt = re.sub(r"\s+", "", holiday["raw_excerpt"])
            if asset["source_id"] != holiday["definition_source_id"] or not excerpt or excerpt not in text:
                raise InvalidArgument("假期定义缺少匹配原文")
            clean = re.sub(r"（[^）]*）", "", excerpt)
            match = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日(?:至(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日)?", clean)
            if not match:
                raise InvalidArgument("假期原文日期无法核验")
            year = holiday["year"]
            original_start = date(int(match[1] or year), int(match[2]), int(match[3]))
            original_end = date(int(match[4] or year), int(match[5] or match[2]), int(match[6] or match[3]))
            if "与周末连休" in excerpt:
                if original_start.weekday() != 0 or original_start != original_end:
                    raise InvalidArgument("周末连休定义不符合归一化规则")
                original_start -= timedelta(days=2)
            if start != original_start or end != original_end or holiday["days_count"] != (end-start).days+1:
                raise InvalidArgument("假期日期与官方原文不一致")
            key = tuple(holiday[k] for k in ("kind", "year", "region_code", "scope_key"))
            days = {start+timedelta(days=n) for n in range(holiday["days_count"])}
            if holiday["id"] in ids or key in keys or covered_days & days:
                raise InvalidArgument("假期定义重复或窗口重叠")
            if holiday["region_code"] != "000000" or holiday["scope_key"] != "CN:national_general_public":
                raise InvalidArgument("全国假期必须标注全国适用范围")
            label = excerpt.split("：", 1)[0].split("、", 1)[1]
            expected_kind = {"元旦":"new_year", "春节":"spring_festival", "清明节":"qingming", "劳动节":"may_day",
                             "端午节":"dragon_boat", "中秋节":"mid_autumn", "国庆节":"national_day",
                             "中秋节、国庆节":"national_day", "国庆节、中秋节":"national_day"}.get(label)
            expected_components = ["mid_autumn", "national_day"] if "、" in label else [expected_kind]
            if holiday["kind"] != expected_kind or holiday["components"] != expected_components or holiday["name"] != f"{year}年{label}":
                raise InvalidArgument("假期类型或合并组成与官方条款不一致")
            ids.add(holiday["id"]); keys.add(key); covered_days.update(days)
        for year, days in calendar_data["adjusted_workdays"].items():
            if len(days) != len(set(days)):
                raise InvalidArgument("调休工作日重复")
            source_text = package_path(root, assets[f"holiday-{year}"]["text_path"]).read_text()
            clean = re.sub(r"\s+|（[^）]*）", "", source_text)
            clauses = re.findall(r"([\d年月日、]+)上班", clean)
            official = set()
            for clause in clauses:
                for month, day in re.findall(r"(\d{1,2})月(\d{1,2})日", clause):
                    official.add(date(int(year), int(month), int(day)))
            actual = {date.fromisoformat(d) for d in days}
            if actual != official or actual & covered_days:
                raise InvalidArgument("调休工作日与原文不一致")
    event_ids = set()
    for event in manifest.get("events", []):
        asset = assets[event["asset_key"]]
        text = package_path(root, asset["text_path"]).read_text()
        start, end = (datetime.fromisoformat(event[k]) for k in ("starts_at", "ends_at"))
        if (event["id"] in event_ids or event["source_id"] != asset["source_id"] or event["published_at"] != asset["published_at"]
                or event["authority"] != "official" or event["status"] != "expired"
                or start.utcoffset() is None or end.utcoffset() is None or end < start
                or end >= datetime.now(timezone.utc) or not set(event["affected_entity_ids"]) <= entities.keys()
                or not event["raw_excerpt"] or re.sub(r"\s+", "", event["raw_excerpt"]) not in re.sub(r"\s+", "", text)):
            raise InvalidArgument("历史官方事件原文、范围或有效期不一致")
        period = re.search(r"(\d{1,2})月(\d{1,2})(?:日)?[-—至](?:(\d{1,2})月)?(\d{1,2})日", text)
        year = datetime.fromisoformat(event["published_at"]).year
        if not period or start.date() != date(year, int(period[1]), int(period[2])) or end.date() != date(year, int(period[3] or period[1]), int(period[4])):
            raise InvalidArgument("历史事件起止日期与原文不一致")
        event_ids.add(event["id"])
    return holidays


def validate_real_dataset(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path).resolve()
    root = path.parent
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "v1":
        raise InvalidArgument("不支持的真实资料包版本")
    entities = {e["id"]: e for e in manifest["catalog"]["entities"]}
    sources = {s["id"]: s for s in manifest["catalog"]["sources"]}
    metrics = {m["code"]: m for m in manifest["catalog"]["metrics"]}
    assets = {a["key"]: a for a in manifest["assets"]}
    if len(assets) != len(manifest["assets"]):
        raise InvalidArgument("资料包原件 key 重复")
    for asset in assets.values():
        original = verify_file(root, asset)
        if asset["source_id"] not in sources or original.stat().st_size != asset["byte_size"]:
            raise InvalidArgument("原件来源或大小不一致", asset_key=asset["key"])
        if asset.get("text_path"):
            verify_file(root, asset, "text_path", "text_sha256")
        if asset["media_type"] == "application/pdf" and not original.read_bytes().startswith(b"%PDF-"):
            raise InvalidArgument("原件不是 PDF", asset_key=asset["key"])
    observations = manifest["observations"]
    keys = set()
    for row in observations:
        try:
            asset = assets[row["asset_key"]]
            metric = metrics[row["metric_code"]]
            entity = entities[row["entity_id"]]
            start, end = date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"])
            value, original, factor = (Decimal(row[k]) for k in ("standard_value", "original_value", "conversion_factor"))
            expected_factor = {"人": Decimal(1), "人次": Decimal(1), "万人": Decimal(10000), "万人次": Decimal(10000)}.get(row["original_unit"])
            if not all(v.is_finite() for v in (value, original, factor)) or original < 0 or expected_factor != factor or value != original * factor:
                raise InvalidArgument("真实数据单位换算不一致")
            if row["standard_unit"] != metric["unit"] or entity["type"] not in metric["supported_entity_types"]:
                raise InvalidArgument("真实数据指标或实体口径不一致")
            if asset["source_id"] != row["source_id"] or asset["published_at"] != row["published_at"]:
                raise InvalidArgument("观测与原件来源/发布时间不一致")
            if row["grain"] == "month":
                if start.day != 1 or end != date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]):
                    raise InvalidArgument("月度观测不是完整自然月")
            elif row["grain"] == "day":
                if start != end:
                    raise InvalidArgument("日度观测起止不一致")
            else:
                raise InvalidArgument("本资料包仅将明确的日/月实绩导入普通观测")
            if row.get("qualifier") is not None or row["quality_status"] != "verified" or not row.get("raw_excerpt") or not row["source_locator"]:
                raise InvalidArgument("真实观测未经核验或无原文定位")
            source_text = package_path(root, asset["text_path"]).read_text(encoding="utf-8")
            excerpt = re.sub(r"\s+", "", row["raw_excerpt"])
            if excerpt not in re.sub(r"\s+", "", source_text):
                raise InvalidArgument("原文摘录不在已存原件抽取文本中")
            if row["metric_code"] == "airport_throughput":
                match = re.search(r"(?:合肥/新桥|黄山/屯溪)\s+\d+\s+([\d,]+)", row["raw_excerpt"])
                if not match or Decimal(match[1].replace(",", "")) != original:
                    raise InvalidArgument("机场原件旅客吞吐量列与规范值不一致")
            elif row["metric_code"] == "rail_departures":
                match = re.search(r"单日发送旅客最多的是4月6日，为([\d.]+)万人次", row["raw_excerpt"])
                if not match or Decimal(match[1]) != original:
                    raise InvalidArgument("铁路实绩与原文引用不一致")
            stamp = datetime.fromisoformat(row["published_at"])
            if stamp.utcoffset() is None:
                raise InvalidArgument("发布时间缺少时区")
            key = tuple(row[k] for k in ("entity_id", "metric_code", "period_start", "period_end", "grain", "source_id"))
            if key in keys:
                raise InvalidArgument("资料包观测业务键重复")
            keys.add(key)
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise InvalidArgument("真实观测字段无效", error=str(exc)) from exc
    csv_rows = []
    for item in manifest["csv_files"]:
        csv_path = verify_file(root, item)
        asset = assets[item["asset_key"]]
        if asset["source_id"] != item["source_id"]:
            raise InvalidArgument("CSV 与原件不属于同一来源")
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            if not REQUIRED_COLUMNS <= set(fields):
                raise InvalidArgument("CSV 缺少必要列")
            rows = list(reader)
        expected = [r for r in observations if r["asset_key"] == item["asset_key"]]
        if len(rows) != item["row_count"] or rows != [{field: str(r[field]) for field in fields} for r in expected]:
            raise InvalidArgument("CSV 内容与核验清单不一致", path=item["path"])
        csv_rows.extend((item["asset_key"], r) for r in rows)
    if len(csv_rows) != len(observations) or len({f["asset_key"] for f in manifest["csv_files"]}) != len(manifest["csv_files"]):
        raise InvalidArgument("CSV 未恰好覆盖全部观测")
    facts_file = verify_file(root, manifest["supplementary_facts"])
    facts = json.loads(facts_file.read_text(encoding="utf-8"))["facts"]
    if len(facts) != manifest["supplementary_facts"]["row_count"]:
        raise InvalidArgument("补充事实数量不一致")
    for fact in facts:
        if fact["asset_key"] not in assets or fact["entity_id"] not in entities or fact["source_id"] != assets[fact["asset_key"]]["source_id"]:
            raise InvalidArgument("补充事实来源或实体无效")
        asset = assets[fact["asset_key"]]
        if fact["published_at"] != asset["published_at"] or date.fromisoformat(fact["period_start"]) > date.fromisoformat(fact["period_end"]):
            raise InvalidArgument("补充事实日期不一致")
        text = package_path(root, asset["text_path"]).read_text(encoding="utf-8")
        if re.sub(r"\s+", "", fact["raw_excerpt"]) not in re.sub(r"\s+", "", text):
            raise InvalidArgument("补充事实原文定位不一致")
        value, original, factor = (Decimal(fact[k]) for k in ("standard_value", "original_value", "conversion_factor"))
        expected_factor = {"人": Decimal(1), "人次": Decimal(1), "万人": Decimal(10000), "万人次": Decimal(10000)}.get(fact["original_unit"])
        if not all(v.is_finite() for v in (value, original, factor)) or original < 0 or factor != expected_factor or value != original * factor:
            raise InvalidArgument("补充事实单位换算不一致")
    verify_file(root, manifest["quality_report"])
    doc_ids = set()
    for document in manifest["documents"]:
        if document["id"] in doc_ids:
            raise InvalidArgument("文档 ID 重复")
        doc_ids.add(document["id"])
        asset = assets[document["asset_key"]]
        if document["source_id"] != asset["source_id"] or not set(document["entity_ids"]) <= entities.keys():
            raise InvalidArgument("文档来源或实体无效")
        if document["published_at"] != asset["published_at"] or document["text_path"] != asset["text_path"]:
            raise InvalidArgument("文档与原件日期或文本不一致")
        if datetime.fromisoformat(document["published_at"]).utcoffset() is None:
            raise InvalidArgument("文档发布时间缺少时区")
        if not document["document_version"] or len(document["document_version"]) > 64:
            raise InvalidArgument("文档版本标识过长或为空")
    holidays = validate_public_definitions(root, manifest, assets, entities)
    return {"dataset_id": manifest["dataset_id"], "status": "validated", "asset_count": len(assets),
            "holiday_count": len(holidays), "event_count": len(manifest.get("events", [])),
            "observation_count": len(observations), "supplementary_fact_count": len(facts), "document_count": len(doc_ids)}


def import_real_dataset(store: DataStore, manifest_path: str | Path) -> dict[str, Any]:
    result = validate_real_dataset(manifest_path)
    path = Path(manifest_path).resolve()
    root = path.parent
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if store.data_root is None:
        raise InvalidArgument("真实原件入库必须配置 DATA_ROOT")
    with store.write() as uow:
        for item in manifest["catalog"]["metrics"]:
            uow.catalog.upsert_metric(MetricCreate(**item))
        for item in manifest["catalog"]["entities"]:
            existing = uow.session.get(Entity, item["id"])
            if existing is None:
                uow.catalog.create_entity(EntityCreate(**item))
            elif existing.name != item["name"] or existing.type != item["type"] or existing.region_code != item["region_code"]:
                raise Conflict("真实资料包实体 ID 与现有实体不一致", id=item["id"])
        for item in manifest["catalog"]["sources"]:
            values = dict(item)
            for key in ("coverage_start", "coverage_end"):
                if isinstance(values.get(key), str):
                    values[key] = date.fromisoformat(values[key])
            existing = uow.session.get(Source, item["id"])
            if existing is None:
                uow.provenance.register_source(SourceCreate(**values))
            elif existing.url != item["url"] or existing.publisher != item["publisher"]:
                raise Conflict("真实资料包来源 ID 与现有来源不一致", id=item["id"])
    asset_ids = {}
    for item in manifest["assets"]:
        with package_path(root, item["path"]).open("rb") as stream:
            persisted = store.persist_file(source_id=item["source_id"], original_name=item["path"], stream=stream)
        with store.write() as uow:
            asset = uow.provenance.register_asset(AssetCreate(
                source_id=item["source_id"], url=item["url"], title=item["title"],
                retrieved_at=datetime.fromisoformat(item["retrieved_at"]),
                published_at=datetime.fromisoformat(item["published_at"]) if item["published_at"] else None,
                relative_path=persisted["relative_path"], sha256=persisted["sha256"], byte_size=persisted["byte_size"],
                media_type=item["media_type"], usage_note=item["usage_note"],
            ))
        asset_ids[item["key"]] = asset["id"]
    totals = {"processed": 0, "imported": 0, "revised": 0, "skipped": 0, "quarantined": 0}
    runs = []
    for item in manifest["csv_files"]:
        summary = import_standard_csv(store, package_path(root, item["path"]), source_id=item["source_id"],
            asset_id=asset_ids[item["asset_key"]], dry_run=False, reason=f"经核验真实资料包 {manifest['dataset_id']}")
        for key in totals:
            totals[key] += summary["counts"][key]
        runs.append(summary)
    inserted = skipped = 0
    with store.write() as uow:
        for item in manifest["documents"]:
            try:
                existing = uow.knowledge.get_document(item["id"])
            except NotFound:
                existing = None
            if existing:
                if existing["asset_id"] != asset_ids[item["asset_key"]] or existing["entity_ids"] != sorted(item["entity_ids"]):
                    raise Conflict("真实文档 ID 与已登记内容不一致", id=item["id"])
                skipped += 1
                continue
            values = {key: item[key] for key in ("id", "source_id", "title", "publisher", "source_url", "permission_note", "document_version")}
            values["asset_id"] = asset_ids[item["asset_key"]]
            for key in ("published_at", "valid_from", "valid_until"):
                values[key] = datetime.fromisoformat(item[key]) if item.get(key) else None
            uow.knowledge.register_document(values, item["entity_ids"])
            inserted += 1
    definitions = {"holidays": {"inserted": 0, "skipped": 0}, "events": {"inserted": 0, "skipped": 0}}
    holiday_fields = ("id", "name", "kind", "year", "start_date", "end_date", "days_count", "region_code", "scope_key", "definition_source_id")
    with store.write() as uow:
        holidays = json.loads(package_path(root, manifest["holiday_calendar"]["path"]).read_text())["holidays"] if manifest.get("holiday_calendar") else []
        for item in holidays:
            values = {k: item[k] for k in holiday_fields}
            values["definition_asset_id"] = asset_ids[item["asset_key"]]
            for key in ("start_date", "end_date"):
                values[key] = date.fromisoformat(values[key])
            try:
                existing = uow.holidays.get_holiday(item["id"])
            except NotFound:
                existing = None
            if existing:
                if any(existing[k] != v for k, v in values.items()):
                    raise Conflict("假期 ID 与已登记定义不一致", id=item["id"])
                definitions["holidays"]["skipped"] += 1
            else:
                uow.holidays.create_holiday(values)
                definitions["holidays"]["inserted"] += 1
        for item in manifest.get("events", []):
            values = {k: v for k, v in item.items() if k not in {"asset_key", "raw_excerpt"}}
            values["asset_id"] = asset_ids[item["asset_key"]]
            for key in ("starts_at", "ends_at", "published_at", "last_verified_at"):
                values[key] = datetime.fromisoformat(values[key]) if values.get(key) else None
            try:
                existing = uow.events.get_event(item["id"])
            except NotFound:
                existing = None
            if existing:
                if any(existing[k] != v for k, v in values.items() if k != "affected_entity_ids") or sorted(existing["affected_entity_ids"]) != sorted(values["affected_entity_ids"]):
                    raise Conflict("事件 ID 与已登记内容不一致", id=item["id"])
                definitions["events"]["skipped"] += 1
            else:
                uow.events.create_event(values, operator_id=None, now=datetime.now(timezone.utc))
                definitions["events"]["inserted"] += 1
    return result | {"status": "imported" if not totals["quarantined"] else "partial_import",
        "definitions": definitions,
        "counts": totals, "documents": {"inserted": inserted, "skipped": skipped}, "runs": runs,
        "asset_ids": asset_ids, "database_dialect": store.engine.dialect.name,
        "supplementary_facts_imported_as_observations": False, "knowledge_indexes": "pending_E"}
