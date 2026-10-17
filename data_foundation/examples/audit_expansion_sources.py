"""第一步来源审计：留存公开样本、检查已有机场原件，不连接业务数据库。

使用带 pdfplumber/lxml 的 Python 运行。默认仅检查本地文件；--fetch 才下载台账中
每个来源的一个样本，最多 3 个并发、每个 URL 一次请求、20 秒超时，无自动重试。
输出审计结果而不是普通观测；HTTP 200/正文匹配不代表数值核验通过。
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlparse

from lxml import html
import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
logging.getLogger("pdfminer").setLevel(logging.ERROR)


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def read_text(path: Path, media: str) -> str:
    if media == "application/pdf":
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    body = path.read_bytes()
    try:
        body.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        encoding = "gb18030"
    doc = html.fromstring(body, parser=html.HTMLParser(encoding=encoding))
    for node in doc.xpath("//script|//style|//noscript"):
        node.drop_tree()
    return re.sub(r"\s+", " ", doc.text_content()).strip()


def sample_source(source: dict, output: Path) -> dict:
    result = {"source_id": source["id"], "url": source["url"],
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "numeric_verified": False, "auto_write_ready": False}
    if urlparse(source["url"]).scheme != "https":
        return result | {"status": "invalid_scheme"}
    try:
        with tempfile.TemporaryDirectory(prefix="anhui-source-audit-") as temp:
            raw = Path(temp) / "body"
            response = subprocess.run([
                "curl", "--silent", "--show-error", "--location", "--proto", "=https",
                "--proto-redir", "=https", "--max-time", "20", "--max-filesize", "10000000",
                "--output", str(raw), "--write-out", "%{http_code}\n%{url_effective}",
                source["url"],
            ], capture_output=True, text=True, timeout=25)
            lines = response.stdout.splitlines()
            status = int(lines[0]) if lines and lines[0].isdigit() else 0
            result.update(http_status=status, effective_url=lines[1] if len(lines) > 1 else None,
                          curl_exit_code=response.returncode)
            if response.returncode or not 200 <= status < 300:
                return result | {"status": "fetch_failed", "error": response.stderr.strip()[:300] or f"HTTP {status}"}
            body = raw.read_bytes()
            media = "application/pdf" if body.startswith(b"%PDF-") else "text/html"
            if source["kind"] == "pdf" and media != "application/pdf":
                return result | {"status": "unexpected_content", "error": "PDF URL did not return PDF"}
            if media == "text/html" and b"<html" not in body[:10000].lower():
                return result | {"status": "unexpected_content", "error": "Missing HTML document"}
            sha = digest(body)
            suffix = ".pdf" if media == "application/pdf" else ".html"
            relative = Path("originals") / f"{source['id']}-{sha[:12]}{suffix}"
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            result.update(path=str(relative), sha256=sha, byte_size=len(body), media_type=media)
            text = read_text(destination, media)
            extracted = Path("extracted") / f"{source['id']}-{sha[:12]}.txt"
            (output / extracted).parent.mkdir(parents=True, exist_ok=True)
            (output / extracted).write_text(text, encoding="utf-8")
            matches = {marker: marker in text for marker in source["expected_markers"]}
            result.update(text_path=str(extracted), text_sha256=digest(text.encode()),
                          character_count=len(text), marker_matches=matches)
            matched = len(text) >= 300 and all(matches.values())
            result["status"] = ("entry_available" if source["kind"] == "entry" else "sample_available") if matched else "content_review_required"
            return result
    except Exception as exc:
        return result | {"status": "audit_failed", "error": str(exc)[:300]}


def airport_audit(config: dict) -> dict:
    dataset = ROOT / "real_data/2026-10-12"
    plan = json.loads((dataset / "source_plan.json").read_text())
    airports = [entity for entity in config["entities"] if entity["type"] == "airport"]
    presence = {entity["key"]: [] for entity in airports}
    checked, errors, samples = [], [], []
    for request in plan["requests"]:
        meta = request.get("metadata") or {}
        period = meta.get("period", "")
        if not re.fullmatch(r"20\d{2}-\d{2}", period) or meta.get("media_type") != "application/pdf":
            continue
        try:
            raw = dataset / meta["path"]
            extracted = dataset / meta["text_path"]
            if digest(raw.read_bytes()) != meta["sha256"]:
                raise ValueError("原件摘要不符")
            if digest(extracted.read_bytes()) != meta["text_sha256"]:
                raise ValueError("既有提取文本摘要不符")
            text = extracted.read_text(encoding="utf-8")
            checked.append({"period": period, "path": str(raw.relative_to(ROOT)),
                            "sha256": meta["sha256"], "url": meta["url"]})
            for entity in airports:
                rows = [line for line in text.splitlines()
                        if any(re.match(r"^" + re.escape(alias) + r"\s+\d+\s+\d+\s", line)
                               for alias in entity["aliases"])]
                if rows:
                    presence[entity["key"]].append(period)
                    if period == "2026-08":
                        match = re.match(r"^(.+?)\s+(\d+)\s+(\d+)\s", rows[0])
                        samples.append({"entity_key": entity["key"], "name": entity["name"],
                                        "period_start": "2026-08-01", "period_end": "2026-08-31",
                                        "metric": "airport_throughput", "grain": "month",
                                        "value": int(match.group(3)), "unit": "人次",
                                        "raw_row": rows[0], "source_locator": "原件第1页，旅客吞吐量，本期完成列",
                                        "asset_path": str(raw.relative_to(ROOT)), "asset_sha256": meta["sha256"],
                                        "source_url": meta["url"], "quality_status": "pending_review",
                                        "note": "第一步抽样核对；未入普通观测库"})
        except Exception as exc:
            errors.append({"period": period, "error": str(exc)})
    return {"checked_assets": checked, "errors": errors, "row_presence": [
        {"entity_key": entity["key"], "name": entity["name"], "periods": sorted(set(presence[entity["key"]])),
         "period_count": len(set(presence[entity["key"]])),
         "note": "只核验已保存报表内行的存在，不证明未取得月份已发布或所有数值通过核验"}
        for entity in airports], "samples": samples}


def existing_evidence() -> list[dict]:
    """原件重新校验；保留原采集日期，不冒充本轮下载成功。"""
    dataset = ROOT / "real_data/2026-10-12"
    plan = json.loads((dataset / "source_plan.json").read_text())
    keys = {"huangshan-2025-report": "scenic:huangshan-main",
            "huangshan-north-2025-h1": "rail:huangshan-north"}
    result = []
    for request in plan["requests"]:
        if request["key"] not in keys or not request.get("metadata"):
            continue
        meta = request["metadata"]
        raw = dataset / meta["path"]
        valid = raw.is_file() and digest(raw.read_bytes()) == meta["sha256"]
        result.append({"entity_key": keys[request["key"]], "path": str(raw.relative_to(ROOT)),
                       "url": meta["url"], "sha256": meta["sha256"], "checksum_valid": valid,
                       "original_retrieved_at": meta["retrieved_at"],
                       "note": "既有资料包证据，仅证明原件留存与摘要一致；本轮网络结果另列"})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--reparse", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/expansion-step1-2026-10-14")
    args = parser.parse_args()
    config = json.loads((ROOT / "assets/expansion_source_audit.json").read_text())
    assert len(config["entities"]) == config["core_entity_count"] == 44
    assert len({e["key"] for e in config["entities"]}) == 44
    ids = {s["id"] for s in config["sources"]}
    assert all(set(e["source_ids"]) <= ids for e in config["entities"])
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    acquisition = output / "acquisition.json"
    results = json.loads(acquisition.read_text()) if acquisition.exists() else []
    if args.fetch:
        selected = [source for source in config["sources"] if not args.source_id or source["id"] in args.source_id]
        if args.source_id and set(args.source_id) - ids:
            raise ValueError("Unknown source ID")
        old_results = list(results)
        selected_ids = {source["id"] for source in selected}
        results = [result for result in results if result["source_id"] not in selected_ids]
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = {pool.submit(sample_source, source, output): source for source in selected}
            for future in as_completed(pending):
                result = future.result()
                results.append(result)
                print(f"{result['source_id']}: {result['status']}", flush=True)
        results.sort(key=lambda result: result["source_id"])
        history_path = output / "acquisition_history.json"
        history = json.loads(history_path.read_text()) if history_path.exists() else []
        history.extend(result for result in old_results if result["source_id"] in selected_ids)
        history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n")
    if args.reparse:
        source_by_id = {source["id"]: source for source in config["sources"]}
        for result in results:
            if not result.get("path"):
                continue
            source = source_by_id[result["source_id"]]
            if result["url"] != source["url"]:
                result["status"] = "source_definition_changed"
                continue
            raw = output / result["path"]
            if digest(raw.read_bytes()) != result["sha256"]:
                raise ValueError("Saved sample checksum mismatch")
            text = read_text(raw, result["media_type"])
            (output / result["text_path"]).write_text(text, encoding="utf-8")
            matches = {marker: marker in text for marker in source["expected_markers"]}
            result.update(text_sha256=digest(text.encode()), character_count=len(text), marker_matches=matches)
            matched = len(text) >= 300 and all(matches.values())
            result["status"] = ("entry_available" if source["kind"] == "entry" else "sample_available") if matched else "content_review_required"
    acquisition.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    airport = airport_audit(config)
    existing = existing_evidence()
    existing_by_entity = {item["entity_key"]: item for item in existing if item["checksum_valid"]}
    by_source = {item["source_id"]: item for item in results}
    airport_rows = {item["entity_key"]: item for item in airport["row_presence"]}
    assessment = []
    for entity in config["entities"]:
        evidence = [{"source_id": source_id, "status": by_source.get(source_id, {}).get("status", "not_fetched")}
                    for source_id in entity["source_ids"]]
        if entity.get("target_metric_blocker"):
            outcome = "target_metric_not_confirmed"
        elif entity["type"] == "airport":
            outcome = "monthly_sample_available" if airport_rows[entity["key"]]["period_count"] else "sample_not_found"
        elif entity["key"] in existing_by_entity:
            outcome = "existing_sample_available"
        else:
            statuses = {item["status"] for item in evidence}
            outcome = "sample_available_scope_review" if "sample_available" in statuses else "entry_only" if "entry_available" in statuses else "sample_unavailable"
        assessment.append(entity | {"audit_outcome": outcome, "fetch_evidence": evidence,
                                    "existing_evidence": existing_by_entity.get(entity["key"])})
    report = {"schema_version": "source-audit-result-v1", "generated_at": datetime.now(timezone.utc).isoformat(),
              "business_database_written": False, "source_auto_update_enabled": False,
              "summary": {"entities": len(assessment), "sources": len(config["sources"]),
                          "attempted_sources": len(results), "fetch_status_counts": dict(Counter(r["status"] for r in results)),
                          "entity_status_counts": dict(Counter(e["audit_outcome"] for e in assessment)),
                          "airport_assets_checked": len(airport["checked_assets"]), "airport_errors": len(airport["errors"]),
                          "airport_sample_count": len(airport["samples"])},
              "entities": assessment, "airport_audit": airport, "existing_evidence": existing}
    (output / "audit_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
