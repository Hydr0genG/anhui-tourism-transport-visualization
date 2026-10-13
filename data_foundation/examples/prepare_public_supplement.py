"""离线整合公开补充资料；保留已验收首批数据，不从累计值推算月份。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fetch_real_dataset import Collector, USAGE, save_collection, stable_id


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(root: Path, baseline: Path):
    manifest = json.loads((baseline / "manifest.json").read_text())
    acquisition = json.loads((root / "acquisition.json").read_text())
    collector = Collector(root)
    collector.assets = acquisition["assets"]
    collector.sources = {s["id"]: s for s in acquisition["sources"]}
    collector.requests = json.loads((root / "source_plan.json").read_text())["requests"]
    collector.failures = json.loads((root / "acquisition_failures.json").read_text())
    assets = {a["key"]: a for a in collector.assets}
    for year in range(2023, 2027):
        asset = assets[f"holiday-{year}"]
        collector.extract(asset, (root / asset["path"]).read_bytes())
    captures = json.loads((root / "browser_captures.json").read_text())["captures"]
    scenic_id = next(e["id"] for e in manifest["catalog"]["entities"] if e["type"] == "scenic")
    for capture in captures:
        key = capture["key"]
        sid = collector.source(key, capture["publisher"], capture["title"], capture["url"])
        snapshot = root / f"browser_snapshots/{key}.txt"
        snapshot.parent.mkdir(exist_ok=True)
        snapshot.write_text(f"标题：{capture['title']}\n来源：{capture['publisher']}\n网址：{capture['url']}\n发布时间：{capture['published_at']}\n采录范围：{capture['capture_scope']}\n\n{capture['text']}\n", encoding="utf-8")
        previous = assets.get(key)
        asset = {"key": key, "source_id": sid, "url": capture["url"], "page_url": capture["url"],
                 "title": capture["title"], "path": str(snapshot.relative_to(root)), "sha256": digest(snapshot),
                 "byte_size": snapshot.stat().st_size, "media_type": "text/plain", "published_at": capture["published_at"],
                 "retrieved_at": previous["retrieved_at"] if previous else datetime.now(timezone.utc).isoformat(),
                 "text_path": str(snapshot.relative_to(root)), "text_sha256": digest(snapshot),
                 "capture_method": "official_browser_visible_text_transcription", "capture_scope": capture["capture_scope"],
                 "usage_note": USAGE + "；直接读取官方网页后的正文采录，非HTML字节原件；" + capture["capture_scope"]}
        assets[key] = asset
    collector.assets = list(assets.values())
    save_collection(collector)
    manifest["dataset_id"] = "anhui-public-supplement-2026-10-12"
    manifest["assets"] = collector.assets
    manifest["catalog"]["sources"] = list(collector.sources.values())
    documents = manifest["documents"]

    def document(asset, entities, start, end, note):
        documents.append({"id": stable_id("document:" + asset["key"]), "asset_key": asset["key"], "source_id": asset["source_id"],
                          "title": asset["title"], "publisher": collector.sources[asset["source_id"]]["publisher"],
                          "source_url": asset["url"], "published_at": asset["published_at"],
                          "publication_precision": asset.get("publication_precision", "minute"),
                          "valid_from": start, "valid_until": end, "entity_ids": entities,
                          "permission_note": asset["usage_note"], "document_version": "sha256:" + asset["sha256"][:32],
                          "text_path": asset["text_path"], "applicability_note": note})

    for capture in captures:
        document(assets[capture["key"]], [scenic_id], capture["valid_from"], capture["valid_until"],
                 "历史资料，仅适用于注明周期；" + capture["capture_scope"] + "；不代表当前运营规定。")
    for year in range(2023, 2027):
        document(assets[f"holiday-{year}"], [], f"{year-1}-12-30T00:00:00+08:00", f"{year}-12-31T23:59:59.999999+08:00",
                 "该年度全国统一放假调休安排，不包含学校寒暑假；个别元旦连休跨年，以日历记录边界为准。")
    newspaper = assets["scenic-2024-08-newspaper"]
    document(newspaper, [scenic_id], "2024-01-01T00:00:00+08:00", "2024-08-31T23:59:59.999999+08:00",
             "历史报道；PDF为整版，多栏文本有交错，引用须核对版面；主景区范围待进一步核对。")

    # 同一年合并发布的中秋/国庆只保存一个窗口，避免重复累计。
    definitions = {
      2023: [("元旦","new_year","2022-12-31","2023-01-02"),("春节","spring_festival","2023-01-21","2023-01-27"),("清明节","qingming","2023-04-05","2023-04-05"),("劳动节","may_day","2023-04-29","2023-05-03"),("端午节","dragon_boat","2023-06-22","2023-06-24"),("中秋节、国庆节","national_day","2023-09-29","2023-10-06")],
      2024: [("元旦","new_year","2023-12-30","2024-01-01"),("春节","spring_festival","2024-02-10","2024-02-17"),("清明节","qingming","2024-04-04","2024-04-06"),("劳动节","may_day","2024-05-01","2024-05-05"),("端午节","dragon_boat","2024-06-08","2024-06-10"),("中秋节","mid_autumn","2024-09-15","2024-09-17"),("国庆节","national_day","2024-10-01","2024-10-07")],
      2025: [("元旦","new_year","2025-01-01","2025-01-01"),("春节","spring_festival","2025-01-28","2025-02-04"),("清明节","qingming","2025-04-04","2025-04-06"),("劳动节","may_day","2025-05-01","2025-05-05"),("端午节","dragon_boat","2025-05-31","2025-06-02"),("国庆节、中秋节","national_day","2025-10-01","2025-10-08")],
      2026: [("元旦","new_year","2026-01-01","2026-01-03"),("春节","spring_festival","2026-02-15","2026-02-23"),("清明节","qingming","2026-04-04","2026-04-06"),("劳动节","may_day","2026-05-01","2026-05-05"),("端午节","dragon_boat","2026-06-19","2026-06-21"),("中秋节","mid_autumn","2026-09-25","2026-09-27"),("国庆节","national_day","2026-10-01","2026-10-07")]
    }
    workdays = {2023:["01-28","01-29","04-23","05-06","06-25","10-07","10-08"],
                2024:["02-04","02-18","04-07","04-28","05-11","09-14","09-29","10-12"],
                2025:["01-26","02-08","04-27","09-28","10-11"],
                2026:["01-04","02-14","02-28","05-09","09-20","10-10"]}
    holidays = []
    for year, periods in definitions.items():
        asset = assets[f"holiday-{year}"]
        text = re.sub(r"\s+", "", (root / asset["text_path"]).read_text())
        for name, kind, start, end in periods:
            match = re.search(r"[一二三四五六七]、" + re.escape(name) + r"：.*?(?=[一二三四五六七]、|节假日期间|鼓励单位和个人|$)", text)
            if not match:
                raise ValueError(f"缺少原文条款 {year} {name}")
            normalized_weekend = year == 2024 and kind in {"new_year", "dragon_boat"}
            holidays.append({"id": stable_id(f"holiday:{year}:{kind}:CN"), "name": f"{year}年{name}", "kind": kind, "year": year,
                             "start_date": start, "end_date": end, "days_count": (date.fromisoformat(end)-date.fromisoformat(start)).days+1,
                             "region_code": "000000", "scope_key": "CN:national_general_public", "definition_source_id": asset["source_id"],
                             "asset_key": asset["key"], "raw_excerpt": match[0], "source_locator": f"放假调休条款：{name}",
                             "normalization_note": "按原文‘与周末连休’纳入紧邻周六、周日；非增加法定节日天数。" if normalized_weekend else "按原文放假调休起止日期；含首尾。",
                             "components": ["mid_autumn", "national_day"] if "、" in name else [kind]})
    calendar_path = root / "holiday_calendar.json"
    write_json(calendar_path, {"schema_version":"v1", "note":"全国通用，地区000000；2024除夕鼓励休息不计入统一窗口。中秋国庆合并发布只登记一次。year按安排年度而非跨年元旦开始年。", "holidays":holidays,
                               "adjusted_workdays":{str(y):[f"{y}-{s}" for s in days] for y,days in workdays.items()}})
    manifest["holiday_calendar"] = {"path":calendar_path.name,"sha256":digest(calendar_path),"row_count":len(holidays)}
    closure = assets["scenic-2024-closure-browser"]
    manifest["events"] = [{"id":stable_id("event:huangshan:2024-07-26-closure"), "title":closure["title"],
                           "description":"台风‘格美’影响，2024年7月26—27日天都峰、西海大峡谷、西大门区域临时关闭。", "kind":"closure", "authority":"official", "severity":"warning", "region_code":"341000", "status":"expired",
                           "starts_at":"2024-07-26T00:00:00+08:00", "ends_at":"2024-07-27T23:59:59.999999+08:00", "published_at":closure["published_at"], "last_verified_at":closure["retrieved_at"],
                           "source_id":closure["source_id"],"asset_key":closure["key"],"affected_entity_ids":[scenic_id],
                           "operator_note":"历史公告。起止只精确到日期，时刻为日边界归一化，不代表实际开关门时刻；不作为当前预警。", "raw_excerpt":"决定7月26日、27日对天都峰、西海大峡谷、西大门区域进行临时关闭"}]
    facts = json.loads((baseline/"supplementary_facts.json").read_text())["facts"]

    def fact(key, start, end, grain, value, unit, excerpt, scope_status):
        asset=assets[key]
        text=(root/asset["text_path"]).read_text()
        if re.sub(r"\s+","",excerpt) not in re.sub(r"\s+","",text):
            raise ValueError("原文摘录不存在")
        facts.append({"entity_id":scenic_id,"metric_code":"scenic_visits","period_start":start,"period_end":end,"grain":grain,
                      "original_value":value,"original_unit":unit,"standard_value":str(Decimal(value)*10000),"standard_unit":"人次","conversion_factor":"10000",
                      "source_id":asset["source_id"],"asset_key":key,"published_at":asset["published_at"],"publication_precision":asset.get("publication_precision","minute"),
                      "quality_status":"pending_scope_confirmation","qualifier":None,"raw_excerpt":excerpt,"source_locator":"正文客流段落；完整采录范围见原件元数据",
                      "methodology_version":"source-published-scope-v1","scope_note":scope_status,"ordinary_observation_eligible":False})
    scope_note="原文称‘黄山风景区’，未明确与既有‘主景区进山游客’完全一致；保留公布数值，确认范围前不并入主景区曲线。"
    fact("scenic-2024-01-statistics-browser","2024-01-01","2024-01-31","month","17.1","万人","黄山风景区接待游客17.1万人",scope_note)
    # PDF首栏单独抽取，防止整版跨栏串读；仍保留原版全文与页码。
    import pdfplumber
    with pdfplumber.open(root/newspaper["path"]) as pdf:
        page=pdf.pages[0]
        column=page.crop((0,0,page.width*0.17,page.height)).extract_text() or ""
    column_path=root/"extracted/scenic-2024-08-newspaper-first-column.txt"
    column_path.write_text(column,encoding="utf-8")
    # 使用已抽取全文中连续存在的原句定位，并保留整版图像复核。
    fact("scenic-2024-08-newspaper","2024-08-01","2024-08-31","month","67.3","万人","其中8月接待游客67.3万人",scope_note)
    fact("scenic-2024-08-newspaper","2024-01-01","2024-08-31","cumulative","329.44","万人","329.44万人",scope_note + "；1—8月累计值，不推算任何单月。")
    fact("scenic-2025-may-browser","2025-05-01","2025-05-05","holiday","14.7","万人次","吸引游客14.7万人次",scope_note + "；对应2025年劳动节五天，非五月自然月。")
    facts_path=root/"supplementary_facts.json"
    write_json(facts_path,{"schema_version":"v1","facts":facts})
    manifest["supplementary_facts"]={"path":facts_path.name,"sha256":digest(facts_path),"row_count":len(facts)}
    quality={"dataset_id":manifest["dataset_id"],"as_of":"2026-10-12","counts":{"assets":len(assets),"server_originals":sum(a.get("capture_method") is None for a in assets.values()),"browser_text_snapshots":len(captures),"observations":len(manifest["observations"]),"supplementary_facts":len(facts),"holiday_windows":len(holidays),"historical_official_events":1,"knowledge_documents":len(documents)},
             "supplementary_facts_added":4,"airport_months_each":41,"airport_longest_contiguous_months":11,"scenic_verified_main_months":0,"rail_verified_months":0,"current_official_events":0,
             "remaining_gaps":["两机场2023、2024、2025年12月单月原始实绩（不得年减累计替代）","黄山主景区同口径连续月报；新增两个月公布数值仍需主景区范围确认","黄山北站连续发送月报与完整假期实绩","省内其他景区、车站的实体与实绩","各学校适用寒暑假校历","授权入口录像、权限凭证、摄像头位置、越线配置和人工进出标注","可复现预测训练/回测；知识文档E侧索引；目标业务库实际导入"],
             "sufficiency":{"history_display":"机场真实数据可展示，月份缺口须保留；景区和铁路仅支持已有原周期事实展示。","holiday_compare":"全国假期窗口已可用，完整同口径客流仍不足。","official_notice_qa":"新增公告可作历史问答素材，尚未建立E索引；提示中的天气不可作当前预报。","forecast":"尚不满足连续24—36个月历史序列目标，也不具备短时计数输入。"}}
    write_json(root/"quality_report.json",quality)
    manifest["quality_report"]={"path":"quality_report.json","sha256":digest(root/"quality_report.json")}
    manifest["browser_captures"]={"path":"browser_captures.json","sha256":digest(root/"browser_captures.json")}
    write_json(root/"manifest.json",manifest)
    return quality


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--baseline",type=Path,default=Path("real_data/2026-10-12"))
    args=parser.parse_args()
    print(json.dumps(prepare(args.root,args.baseline),ensure_ascii=False,indent=2))
