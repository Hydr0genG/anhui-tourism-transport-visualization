"""获取公开原件及来源清单；不连接数据库。需安装 [data] 可选依赖及 curl。

python examples/fetch_real_dataset.py --output real_data/2026-10-12
原件按内容摘要保存；HTTP/解析失败留在 acquisition_failures.json，不使用搜索摘要替代。
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import NAMESPACE_URL, uuid5

from lxml import html
import pdfplumber


INDEX = "https://hd.caac.gov.cn/HD_XXGK/HD_TJSJ/"
USAGE = "公开发布资料，用于本项目历史统计核验及带出处引用；原站未明确专项开放许可，全文对外再发布条件未核实。"


def stable_id(key: str) -> str:
    return str(uuid5(NAMESPACE_URL, "anhui-tourism-data:" + key))


ENTITIES = [
    {"id": stable_id("scenic:huangshan-main"), "name": "黄山风景区主景区", "type": "scenic", "region_code": "341000", "location_note": "按黄山市级登记；仅进山游客/主景区，不含全域接待量和索道运输量"},
    {"id": stable_id("rail:huangshan-north"), "name": "黄山北站", "type": "rail_station", "region_code": "341000", "location_note": "按黄山市级登记；铁路发送量，与黄山站、全市高铁站总量及到发总量分开"},
    {"id": stable_id("airport:TXN"), "name": "黄山屯溪机场", "type": "airport", "region_code": "341000", "location_note": "按黄山市级登记；民航局原表名称：黄山/屯溪"},
    {"id": stable_id("airport:HFE"), "name": "合肥新桥机场", "type": "airport", "region_code": "340100", "location_note": "按合肥市级登记；民航局原表名称：合肥/新桥"},
]


class Collector:
    def __init__(self, root: Path, *, refresh: bool = False):
        self.root = root
        self.assets: list[dict] = []
        self.failures: list[dict] = []
        self.sources: dict[str, dict] = {}
        self.requests: list[dict] = []
        previous = root / "acquisition.json"
        self.cache = {a["key"]: a for a in json.loads(previous.read_text(encoding="utf-8"))["assets"]} if previous.exists() and not refresh else {}

    def source(self, key: str, publisher: str, title: str, url: str, grain=None):
        sid = stable_id("source:" + key)
        self.sources[sid] = {"id": sid, "publisher": publisher, "title": title, "url": url,
            "usage_note": USAGE, "update_frequency": "monthly" if grain == "month" else "irregular",
            "native_grain": grain, "verification_status": "verified", "enabled": False,
            "adapter_key": "standard_csv", "adapter_config": {"acquisition": "explicit_source_plan", "licence_status": "not_stated"}}
        return sid

    def fetch(self, url: str, key: str, source_id: str, *, published_at=None, page_url=None):
        self.requests.append({"url": url, "key": key, "source_id": source_id, "published_at": published_at, "page_url": page_url or url})
        cached = self.cache.get(key)
        if cached and cached["url"] == url:
            path = self.root / cached["path"]
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == cached["sha256"]:
                asset = dict(cached)
                self.assets.append(asset)
                return asset, path.read_bytes()
        try:
            if urlparse(url).scheme != "https":
                raise ValueError("原件只从 HTTPS 来源获取")
            with tempfile.TemporaryDirectory(prefix="anhui-source-") as temporary:
                output = Path(temporary) / "body"
                result = subprocess.run(["curl", "--silent", "--show-error", "--fail", "--location",
                    "--proto", "=https", "--proto-redir", "=https", "--max-time", "120",
                    "--max-filesize", "30000000", "--output", str(output), "--write-out", "%{url_effective}", url],
                    capture_output=True, text=True, check=True)
                body = output.read_bytes()
            is_pdf = urlparse(url).path.lower().endswith(".pdf")
            if is_pdf and not body.startswith(b"%PDF-"):
                raise ValueError("PDF 链接未返回 PDF 原件")
            if not is_pdf and b"<html" not in body.lower()[:3000]:
                raise ValueError("网页链接未返回 HTML 原件")
            sha = hashlib.sha256(body).hexdigest()
            relative = f"originals/{key}-{sha[:12]}.{'pdf' if is_pdf else 'html'}"
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            asset = {"key": key, "source_id": source_id, "url": url, "effective_url": result.stdout,
                "page_url": page_url or url, "title": key, "path": relative, "sha256": sha,
                "byte_size": len(body), "media_type": "application/pdf" if is_pdf else "text/html",
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "published_at": published_at,
                "usage_note": USAGE}
            self.assets.append(asset)
            print(f"取得 {key} ({len(body)} bytes)", flush=True)
            return asset, body
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            message = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            self.failures.append({"url": url, "key": key, "reason": message})
            print(f"待获取 {key}: {message}", flush=True)
            return None, None

    def extract(self, asset: dict, body: bytes):
        if asset["media_type"] == "application/pdf":
            with pdfplumber.open(self.root / asset["path"]) as pdf:
                pages = [page.extract_text(layout=False) or "" for page in pdf.pages]
            text = "\n\n".join(f"[原件第{i}页]\n{part}" for i, part in enumerate(pages, 1))
        else:
            decoded = body.decode("utf-8")
            # 中国政府网页的共用页头含嵌套 HTML，通用解析可能只留下页头。
            # 从原件中明确标出的正文容器抽取，避免把空正文登记为已解析。
            content = re.search(r'<div[^>]+id="UCAP-CONTENT"[^>]*>([\s\S]*?)</td>', decoded)
            tree = html.fromstring(content[1] if content else decoded)
            for element in tree.xpath("//script|//style"):
                element.drop_tree()
            text = tree.text_content()
            pages = [text]
        relative = f"extracted/{asset['key']}.txt"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        asset["text_path"] = relative
        asset["text_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return pages


def save_collection(c: Collector):
    c.root.mkdir(parents=True, exist_ok=True)
    by_key = {a["key"]: a for a in c.assets}
    plan = {"schema_version": "v1", "entities": ENTITIES, "sources": list(c.sources.values()),
            "requests": [r | {"metadata": by_key.get(r["key"])} for r in c.requests]}
    for name, value in [("acquisition.json", {"assets": c.assets, "entities": ENTITIES, "sources": list(c.sources.values())}),
                        ("source_plan.json", plan), ("acquisition_failures.json", c.failures)]:
        (c.root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def replay_plan(root: Path, plan_path: Path, *, refresh: bool):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != "v1":
        raise ValueError("来源方案版本无效")
    c = Collector(root, refresh=refresh)
    c.sources = {s["id"]: s for s in plan["sources"]}
    for request in plan["requests"]:
        metadata = request.get("metadata")
        asset, body = c.fetch(request["url"], request["key"], request["source_id"],
            published_at=request.get("published_at") or (metadata or {}).get("published_at"), page_url=request["page_url"])
        if not asset:
            continue
        if metadata:
            if asset["sha256"] != metadata["sha256"]:
                c.assets.remove(asset)
                c.failures.append({"key": request["key"], "url": request["url"], "reason": "原件内容已修订，不能沿用旧核验清单；请重新采集并核验。"})
                continue
            for key in ("title", "published_at", "publication_precision", "period", "year", "month"):
                if key in metadata:
                    asset[key] = metadata[key]
            if metadata.get("text_path"):
                c.extract(asset, body)
        elif request.get("published_at"):
            asset["title"] = c.sources[request["source_id"]]["title"]
            asset["publication_precision"] = "date" if "T00:00:00" in asset["published_at"] else "minute"
            c.extract(asset, body)
    save_collection(c)


def collect(root: Path, *, refresh: bool = False):
    c = Collector(root, refresh=refresh)
    caac_sid = c.source("caac-east-monthly", "中国民用航空华东地区管理局", "华东地区机场月度运输生产统计", INDEX, "month")
    pages: dict[str, dict] = {}
    for number in range(4):
        url = INDEX if number == 0 else urljoin(INDEX, f"index_{number}.html")
        asset, body = c.fetch(url, f"caac-directory-{number}", caac_sid)
        if body is None:
            continue
        for a in html.fromstring(body.decode("utf-8")).xpath("//a[@href]"):
            title = "".join(a.itertext()).strip()
            match = re.search(r"(202[3-6])年(\d{1,2})月(?:份)?华东地区机场运输生产情况", title)
            annual = re.search(r"(202[3-5])年(?:民航)?华东地区运输生产情况", title)
            if match:
                year, month = map(int, match.groups())
                if year == 2026 and month > 8:
                    continue
                pages[f"{year}-{month:02}"] = {"url": urljoin(url, a.get("href")), "title": title, "year": year, "month": month}
            elif annual:
                year = int(annual[1])
                pages[f"{year}-annual"] = {"url": urljoin(url, a.get("href")), "title": title, "year": year, "month": None}
    for period, item in sorted(pages.items()):
        asset, body = c.fetch(item["url"], f"caac-{period}-page", caac_sid)
        if body is None:
            continue
        decoded = body.decode("utf-8")
        stamp = re.search(r"__\$pubtime='([^']+)'", decoded)
        if not stamp:
            raise ValueError(f"月报缺少可核验发布时间: {period}")
        # 民航网站显示北京时间，不能把显示时间直接当成 UTC。
        published_at = stamp[1].replace(" ", "T") + "+08:00"
        asset.update(title=item["title"], published_at=published_at)
        tree = html.fromstring(decoded)
        links = [(a, "".join(a.itertext()).strip()) for a in tree.xpath("//a[@href]")]
        selected = [(a, title) for a, title in links if a.get("href").lower().endswith(".pdf")
                    and "分省" not in title]
        if not selected:
            c.failures.append({"key": f"caac-{period}-pdf", "url": item["url"], "reason": "未找到机场吞吐量排名附件"})
        for index, (a, title) in enumerate(selected):
            pdf_asset, pdf_body = c.fetch(urljoin(item["url"], a.get("href")), f"caac-{period}-pdf-{index}", caac_sid,
                published_at=published_at, page_url=item["url"])
            if pdf_asset:
                pdf_asset.update(title=title, period=period, year=item["year"], month=item["month"])
                c.extract(pdf_asset, pdf_body)
    external = [
        ("huangshan-2024-report", "黄山旅游发展股份有限公司", "2024年年度报告摘要", "https://static.cninfo.com.cn/finalpage/2025-04-19/1223151679.PDF", "2025-04-19T00:00:00+08:00"),
        ("huangshan-2025-q1", "黄山旅游发展股份有限公司", "2025年第一季度报告", "https://static.cninfo.com.cn/finalpage/2025-04-30/1223418970.PDF", "2025-04-30T00:00:00+08:00"),
        ("huangshan-2025-report", "黄山旅游发展股份有限公司", "2025年年度报告", "https://static.cninfo.com.cn/finalpage/2026-04-18/1225118314.PDF", "2026-04-18T00:00:00+08:00"),
        ("huangshan-north-2025-h1", "安徽日报", "轨道上的文旅协奏曲", "https://szb.ahnews.com.cn/szb/content/202507/21/c1096188.html", "2025-07-21T00:00:00+08:00"),
        ("huangshan-2025-spring-notice", "黄山风景区管理委员会", "关于黄山风景区2025年春节黄金周开放时间、交通换乘和索道运营时间的公告", "https://www.huangshan.gov.cn/zxzx/tzgg/8409577.html", "2025-01-24T10:21:00+08:00"),
        ("huangshan-2026-may-notice", "黄山市文化和旅游局", "五一假期出游温馨提示", "https://www.huangshan.gov.cn/zwgk/public/6615714/12043498.html", "2026-04-22T09:53:00+08:00"),
    ]
    for key, publisher, title, url, published_at in external:
        sid = c.source(key, publisher, title, url)
        asset, body = c.fetch(url, key, sid, published_at=published_at)
        if asset:
            asset.update(title=title, publication_precision="date" if "T00:00:00" in published_at else "minute")
            c.extract(asset, body)
    save_collection(c)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-plan", type=Path, help="从已保存的来源清单复现原件；摘要改变时拒绝沿用旧核验")
    parser.add_argument("--refresh", action="store_true", help="重新检查网络来源；默认使用本地已校验缓存")
    args = parser.parse_args()
    if args.source_plan:
        replay_plan(args.output.resolve(), args.source_plan.resolve(), refresh=args.refresh)
    else:
        collect(args.output.resolve(), refresh=args.refresh)
