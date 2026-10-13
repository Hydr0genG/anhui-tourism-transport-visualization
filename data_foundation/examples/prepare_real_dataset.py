"""从已下载原件准备规范 CSV、原粒度事实、文档清单与质量报告（离线）。"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fetch_real_dataset import ENTITIES, stable_id


def sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def airport_rows(text: str, *, month: int | None):
    """只提取旅客吞吐量排名表的第一数值列，不取累计/货邮/架次列。"""
    result = []
    page = 0
    allowed = False
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"\[原件第(\d+)页\]", line)
        if marker:
            page = int(marker[1])
        if "旅客吞吐量" in line:
            allowed = True
        if not allowed:
            continue
        for label, code in (("合肥/新桥", "HFE"), ("黄山/屯溪", "TXN")):
            if line.startswith(label):
                numbers = re.findall(r"[+-]?\d[\d,]*(?:\.\d+)?", line[len(label):])
                if len(numbers) < 3 or not numbers[0].isdigit() or not numbers[1].replace(",", "").isdigit():
                    raise ValueError(f"机场行结构不确定: {line}")
                result.append({"code": code, "original_value": numbers[1].replace(",", ""),
                               "cumulative_value": numbers[3].replace(",", "") if "本年累计" in text[:500] else None,
                               "page": page, "line": number, "raw_line": line})
    # 排名表和分省表不能同时输入；重复行不能靠任意选择一行解决。
    if len(result) != 2 or len({r["code"] for r in result}) != 2:
        raise ValueError(f"应有两座机场且各一行，实际 {len(result)} 行")
    if month is not None:
        header = text[:500]
        label = re.search(r"(\d{1,2})月", header)
        if not label or int(label[1]) != month or "本期完成" not in header:
            raise ValueError("月报表头未明确匹配月份及本期完成列")
    return result


def prepare(root: Path):
    acquisition = json.loads((root / "acquisition.json").read_text(encoding="utf-8"))
    assets = {a["key"]: a for a in acquisition["assets"]}
    facts, files, observations, warnings, resolved_warnings = [], [], [], [], []
    fields = ["entity_id", "metric_code", "period_start", "period_end", "grain", "original_value", "original_unit",
              "standard_value", "standard_unit", "conversion_factor", "source_locator", "methodology_version", "published_at", "quality_status"]

    def fact(asset, entity, metric, start, end, grain, value, unit, locator, excerpt, status="verified", qualifier=None):
        factor = "10000" if unit in {"万人", "万人次"} else "1"
        record = {"entity_id": entity, "metric_code": metric, "period_start": start, "period_end": end, "grain": grain,
            "original_value": value, "original_unit": unit, "standard_value": str(Decimal(value) * Decimal(factor)),
            "standard_unit": "人次", "conversion_factor": factor, "source_locator": locator,
            "methodology_version": "official-published-period-v1", "published_at": asset["published_at"],
            "quality_status": status, "source_id": asset["source_id"], "asset_key": asset["key"],
            "raw_excerpt": excerpt, "publication_precision": asset.get("publication_precision", "minute"), "qualifier": qualifier}
        if grain in {"month", "day"} and status == "verified" and qualifier is None:
            observations.append(record)
        else:
            facts.append(record)

    for asset in acquisition["assets"]:
        key = asset["key"]
        if not key.startswith("caac-") or "-pdf-" not in key:
            continue
        text = (root / asset["text_path"]).read_text(encoding="utf-8")
        month, year = asset["month"], asset["year"]
        try:
            rows = airport_rows(text, month=month)
            if month is not None:
                # 若附件标签标错年份，原 PDF 未给出年份时保持待核验，而非由网页强行覆盖。
                title_year = re.search(r"(20\d{2})年", asset["title"])
                pdf_year = re.search(r"(20\d{2})年", text[:500])
                if title_year and int(title_year[1]) != year and (not pdf_year or int(pdf_year[1]) != year):
                    # 2024-01附件标签写作2022；只用下一月明确发布的本年累计校验原件值，不生成差分值。
                    next_asset = assets.get(f"caac-{year}-{month+1:02}-pdf-0")
                    if month != 1 or next_asset is None:
                        raise ValueError(f"附件标签年份 {title_year[1]} 与页面 {year} 不符，无法消除歧义")
                    next_rows = {r["code"]: r for r in airport_rows((root / next_asset["text_path"]).read_text(encoding="utf-8"), month=2)}
                    if any(next_rows[r["code"]]["cumulative_value"] is None or Decimal(r["original_value"]) + Decimal(next_rows[r["code"]]["original_value"]) != Decimal(next_rows[r["code"]]["cumulative_value"]) for r in rows):
                        raise ValueError("附件年份标签不一致且相邻月累计校验失败")
                    resolved_warnings.append({"asset_key": key, "corroborating_asset_key": next_asset["key"],
                        "reason": f"附件标签年份{title_year[1]}与{year}网页不符；两机场1月原件值+2月明确单月值均与2月明确累计列一致。保留原件值，未用差分生成月度值。"})
            for row in rows:
                if month is None:
                    start, end, grain = f"{year}-01-01", f"{year}-12-31", "year"
                else:
                    start, end, grain = f"{year}-{month:02}-01", f"{year}-{month:02}-{calendar.monthrange(year, month)[1]}", "month"
                fact(asset, stable_id("airport:" + row["code"]), "airport_throughput", start, end, grain,
                     row["original_value"], "人", f"{asset['title']}；PDF第{row['page']}页；{row['code']}对应机场行；旅客吞吐量/本期完成；抽取文本第{row['line']}行",
                     row["raw_line"])
        except ValueError as exc:
            warnings.append({"asset_key": key, "reason": str(exc), "status": "pending_review"})

    scenic_id = stable_id("scenic:huangshan-main")
    for key, year, grain in (("huangshan-2024-report", 2024, "year"), ("huangshan-2025-q1", 2025, "quarter"), ("huangshan-2025-report", 2025, "year")):
        if key not in assets:
            continue
        asset = assets[key]
        text = (root / asset["text_path"]).read_text(encoding="utf-8")
        compact = re.sub(r"\s+", "", text)
        match = re.search(r"黄山景区累计接待进山游客([\d.]+)万人", compact)
        if not match:
            warnings.append({"asset_key": key, "reason": "未定位到进山游客统计句", "status": "pending_review"})
            continue
        page_texts = re.split(r"\[原件第\d+页\]", text)[1:]
        page_no = next(i for i, content in enumerate(page_texts, 1) if "累计接待进山游客" in re.sub(r"\s+", "", content))
        start, end = f"{year}-01-01", f"{year}-{'03-31' if grain == 'quarter' else '12-31'}"
        excerpt = compact[match.start():match.start()+180]
        fact(asset, scenic_id, "scenic_visits", start, end, grain, match[1], "万人", f"PDF第{page_no}页；累计接待进山游客统计句", excerpt)
        previous = re.search(r"较去年同期([\d.]+)万人", excerpt)
        if previous:
            fact(asset, scenic_id, "scenic_visits", f"{year-1}-01-01", f"{year-1}-{'03-31' if grain == 'quarter' else '12-31'}",
                 grain, previous[1], "万人", f"PDF第{page_no}页；明确列出的上年同期值", excerpt)

    railway = assets.get("huangshan-north-2025-h1")
    if railway:
        text = (root / railway["text_path"]).read_text(encoding="utf-8")
        match = re.search(r"黄山北站共发送旅客([\d.]+)万人次", text)
        if not match:
            raise ValueError("铁路原件未找到北站实际发送量")
        fact(railway, stable_id("rail:huangshan-north"), "rail_departures", "2025-01-01", "2025-06-30", "half_year", match[1], "万人次",
             "正文首段；2025年1月至6月黄山北站发送量（安徽日报报道，非铁路原始统计表）", text[max(0, match.start()-90):match.end()+60])
        daily = re.search(r"单日发送旅客最多的是4月6日，为([\d.]+)万人次", text)
        if daily:
            fact(railway, stable_id("rail:huangshan-north"), "rail_departures", "2025-04-06", "2025-04-06", "day", daily[1], "万人次",
                 "正文十年筑就米字高铁网雏形章节；站方受访者说明2025年4月6日单日发送量", text[max(0, daily.start()-60):daily.end()+20])

    processed = root / "standard"
    processed.mkdir(exist_ok=True)
    for key in sorted({r["asset_key"] for r in observations}):
        rows = [r for r in observations if r["asset_key"] == key]
        path = processed / f"{key}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)
        files.append({"path": path.relative_to(root).as_posix(), "sha256": sha(path), "asset_key": key,
                      "source_id": rows[0]["source_id"], "row_count": len(rows)})
    facts_path = root / "supplementary_facts.json"
    facts_path.write_text(json.dumps({"schema_version": "v1", "facts": facts}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    documents = []
    document_keys = ["caac-2025-01-pdf-0", "caac-2026-08-pdf-0", "huangshan-2024-report", "huangshan-2025-q1", "huangshan-2025-report", "huangshan-north-2025-h1", "huangshan-2025-spring-notice", "huangshan-2026-may-notice"]
    for key in document_keys:
        if key not in assets:
            continue
        asset = assets[key]
        if key.startswith("caac"):
            entity_ids = [stable_id("airport:HFE"), stable_id("airport:TXN")]
        elif "north" in key:
            entity_ids = [stable_id("rail:huangshan-north")]
        else:
            entity_ids = [scenic_id]
        source = next(s for s in acquisition["sources"] if s["id"] == asset["source_id"])
        precision = asset.get("publication_precision", "minute")
        related = [r for r in observations+facts if r["asset_key"] == key]
        valid_from = min((r["period_start"] for r in related), default=None)
        valid_until = max((r["period_end"] for r in related), default=None)
        documents.append({"id": stable_id("document:" + key + ":" + asset["sha256"]), "asset_key": key, "source_id": asset["source_id"],
            "title": asset["title"], "publisher": source["publisher"], "source_url": asset["page_url"], "published_at": asset["published_at"],
            "publication_precision": precision, "valid_from": valid_from + "T00:00:00+08:00" if valid_from else None,
            "valid_until": valid_until + "T23:59:59.999999+08:00" if valid_until else None,
            "permission_note": source["usage_note"] + ("；发布时间仅公开到日，00:00为日期归一化边界，不代表实际发布时刻。" if precision == "date" else ""),
            "document_version": "sha256:" + asset["sha256"][:32], "entity_ids": entity_ids,
            "text_path": asset["text_path"], "applicability_note": "历史统计/报道资料，仅适用于注明的统计周期；不代表当前运营规定。"})
    months = [f"{year}-{month:02}" for year in range(2023, 2027) for month in range(1, 13) if (year, month) <= (2026, 8)]
    coverage = []
    for entity in ENTITIES:
        rows = [r for r in observations if r["entity_id"] == entity["id"]]
        actual = sorted({r["period_start"][:7] for r in rows if r["grain"] == "month"})
        longest = consecutive = 0
        for month in months:
            consecutive = consecutive + 1 if month in actual else 0
            longest = max(longest, consecutive)
        coverage.append({"entity_id": entity["id"], "name": entity["name"], "expected_months": len(months), "verified_months": len(actual),
            "coverage_ratio": str(Decimal(len(actual))/Decimal(len(months))), "missing_months": [m for m in months if m not in actual],
            "longest_consecutive_months": longest, "continuous_12_months_acceptance": longest >= 12,
            "available_grains": sorted({r["grain"] for r in rows}), "supplementary_grains": sorted({r["grain"] for r in facts if r["entity_id"] == entity["id"]}),
            "forecast_readiness": "由D按样本量、缺期与回测要求判断；本批不承诺可预测"})
    report = {"schema_version": "v1", "status": "partial_coverage", "target_start": "2023-01-01", "target_end": "2026-08-31",
        "observation_count": len(observations), "supplementary_fact_count": len(facts), "document_count": len(documents),
        "coverage": coverage, "warnings": warnings, "resolved_warnings": resolved_warnings, "acquisition_failures": json.loads((root / "acquisition_failures.json").read_text()),
        "notes": ["不从累计值差分，不把年度/季度/半年值拆成月份，不插值补齐。", "铁路数据来自安徽日报的站方采访报道；尚无铁路原始月度统计表。", "许可未明示；来源未开启自动更新。", "景区和铁路月报缺口保留，三类月度数据完整验收未通过。"]}
    (root / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    lines = ["# 首批真实数据覆盖报告", "", "数据批次：anhui-real-2026-10-12；目标周期：2023-01 至 2026-08。状态：部分覆盖。", "",
        f"普通观测 {len(observations)} 条，补充事实 {len(facts)} 条，知识资料 {len(documents)} 份。", "",
        "| 对象 | 月度覆盖 | 最长连续月份 | 普通查询粒度 | 补充资料粒度 |", "|---|---:|---:|---|---|"]
    for item in coverage:
        lines.append(f"| {item['name']} | {item['verified_months']}/{item['expected_months']} | {item['longest_consecutive_months']} | {', '.join(item['available_grains']) or '无'} | {', '.join(item['supplementary_grains']) or '无'} |")
    lines += ["", "## 缺口与验收边界", "", "- 两机场均缺 2023-12、2024-12、2025-12，连续12个月验收未通过。来源提供年报时保留年值，不差分生成12月。",
        "- 景区、铁路连续月报未取得；景区年/季值和铁路半年值保留在 supplementary_facts.json，不能用于冒充月度曲线。",
        "- 铁路1条日度观测来自安徽日报对站方的采访报道，区别于铁路原始统计表；半年值来自同一报道。",
        "- 13条补充事实包含同一景区2024年值在两个正式报告中的各自出处，合计12个不同实体/指标/周期组合；不能重复相加。",
        "- 6份知识资料中：民航局统计PDF 2份、企业正式披露PDF 3份、铁路原始报道1份；指定的两个政府运营公告尚未取得。",
        "- 不支持粒度、空序列、缺期和预测不足应分别处理。是否能回测由D判定，本批未运行预测或E索引。", "",
        "## 来源获取失败", ""]
    for item in report["acquisition_failures"]:
        lines.append(f"- {item['key']}：{item['reason']}；[原链接]({item['url']})。未使用搜索摘要替代原件。")
    lines += ["", "## 已消除的来源歧义", ""]
    for item in resolved_warnings:
        lines.append(f"- {item['asset_key']}：{item['reason']} 对照原件：{item['corroborating_asset_key']}。")
    lines += ["", "## 原件与使用条件", "", "每条数值均保留来源、原件SHA-256、发布时间、采集时间和原文定位。公开发布不等于明确的开放许可；许可未明示的来源记录其未知状态，未启用自动更新。仅公开到日的发布时间用当天00:00归一化，并在元数据中标为date，不代表实际发布时刻。", "",
        "机器可读明细见 [quality_report.json](quality_report.json)、[manifest.json](manifest.json) 和 [supplementary_facts.json](supplementary_facts.json)。", ""]
    (root / "quality_report.md").write_text("\n".join(lines), encoding="utf-8")
    metrics = json.loads((Path(__file__).resolve().parents[1] / "assets/metric_dictionary.json").read_text())["metrics"]
    # 目录快照在包内固定，避免后续台账更新改变旧批次的解释。
    used_sources = {a["source_id"] for a in acquisition["assets"]}
    for source in acquisition["sources"]:
        if source["id"] not in used_sources:
            source["verification_status"] = "pending_review"
        related = [r for r in observations+facts if r["source_id"] == source["id"]]
        source["coverage_start"] = min((r["period_start"] for r in related), default=None)
        source["coverage_end"] = max((r["period_end"] for r in related), default=None)
    manifest = {"schema_version": "v1", "dataset_id": "anhui-real-2026-10-12", "catalog": {"entities": ENTITIES, "sources": acquisition["sources"], "metrics": metrics},
        "assets": acquisition["assets"], "csv_files": files, "observations": observations, "documents": documents,
        "supplementary_facts": {"path": "supplementary_facts.json", "sha256": sha(facts_path), "row_count": len(facts)},
        "quality_report": {"path": "quality_report.json", "sha256": sha(root / "quality_report.json")}}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"observations": len(observations), "facts": len(facts), "documents": len(documents), "warnings": warnings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    prepare(parser.parse_args().root.resolve())
