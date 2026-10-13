# 首批真实数据包与 D/E 交接说明

批次 `anhui-real-2026-10-12`，C 包 0.1.2，迁移 head 0005，指标字典仍为 0.1.0。数据覆盖状态是**部分交付**，不代表预测、知识索引或正式 HTTP 链路通过。

## 已交付内容

| 内容 | 数量/状态 |
|---|---|
| 固定实体 | 黄山主景区、黄山北站、黄山屯溪机场、合肥新桥机场；按市级行政区登记，未猜测坐标 |
| 完整原件 | 96份；每份含来源URL、采集时间、摘要、大小和媒体类型 |
| 普通时序观测 | 83条：两机场各41个月值，铁路1个日值 |
| 原粒度补充事实 | 13条来源记录，12个不同实体/指标/周期组合；年、季、半年值未塞入月度查询 |
| 知识资料 | 6份：民航局PDF 2份、景区企业正式披露PDF 3份、铁路报道1份 |
| 本地测试 | 18项通过，包含篡改拒绝、换算、日期边界、重复导入、分页/实体筛选和PDF列识别 |
| SQLite / MySQL 8.4.11 | 独立空库从0001迁移到0005；83条实绩导入两次、6份文档去重、五条查询追溯通过 |
| 正式业务库 / HTTP / Worker / E索引 | 尚未联调；文档均为pending，没有伪造向量索引或问答结果 |

完整缺口、来源歧义和使用条件见 [覆盖报告](quality_report.md)。两机场均缺2023—2025年12月单月值，最长连续段为11个月；连续12个月验收未通过。景区和铁路月报仍缺，两份原计划政府运营公告获取失败，知识语料先采用已取得的统计原件。

## 数据与原件定位

- [manifest.json](manifest.json)：目录快照、原件、规范CSV、文档、实体绑定和日期精度；离线导入的唯一清单。
- `originals/`：下载的原始PDF/HTML；没有把搜索摘要保存成原件。
- `extracted/`：从原件解析得到的UTF-8 TXT；PDF页号标识为`[原件第N页]`，全文定位可与manifest/事实逐项对照。
- `standard/`：按原件分组的规范CSV；source_id/asset_id由导入器传入。
- [supplementary_facts.json](supplementary_facts.json)：年度、季度、半年数字及原文定位，适用于历史说明；不支持现有普通时序查询。
- [source_plan.json](source_plan.json)：固定URL与原件摘要，支持来源清单重取；原件改变时必须重新核验。
- [acquisition_failures.json](acquisition_failures.json)：失败链接与实际原因。

每个CSV记录均保留原文数值/单位与Decimal换算。机场表的“人”按旅客吞吐量指标规范为“人次”，因子为1；“万人/万人次”按10000换算，并保留发布精度。发送量、到发量、景区进山量和机场吞吐量不能混加。

## 安装和离线导入

在交接包的`data_foundation`目录内运行（Python 3.11及以上）：

```bash
python -m pip install 'release/anhui_data_foundation-0.1.2-py3-none-any.whl'
python examples/import_real_dataset.py --manifest real_data/2026-10-12/manifest.json
```

默认仅验证文件和字段，不写入数据库或新建导入批次。确认已有MySQL连接和原件目录后，使用D提供的环境配置：

```bash
# DATABASE_URL：由D配置mysql+pymysql地址；DATA_ROOT：由D配置可管理原件目录。
python -m alembic upgrade head
python examples/import_real_dataset.py --manifest real_data/2026-10-12/manifest.json --apply
```

导入时先完整验证摘要、实体/指标、自然月、原文定位和单位换算，再登记目录/原件、导入CSV和登记文档。每份CSV采用独立事务；数据库故障可能产生部分已完成批次，修复后重跑即可续入，不删除已有数据。相同文件重跑新增和修订均为0。`runs`输出保留每份原件的结果。

SQLite仅用于存储验证，须显式加`--allow-sqlite-validation`。本次独立MySQL通过不表示D的业务库已导入。`verify_real_dataset.py`会写入数据，仅在已迁移的独立验收库运行，不能对生产库执行。

## 重新获取和准备

```bash
python -m pip install 'release/anhui_data_foundation-0.1.2-py3-none-any.whl[data]'
python examples/fetch_real_dataset.py --source-plan real_data/2026-10-12/source_plan.json --output real_data/reacquired
python examples/prepare_real_dataset.py --root real_data/reacquired
```

需要系统curl。来源方案重取只接受原有摘要；原件修订记录为待重新核验。新月份可用`fetch_real_dataset.py --output 新目录`重新发现，当前日期范围固定截至2026年8月；扩展日期须明确调整采集范围后再次核验。默认复用摘要匹配的缓存，`--refresh`才重新访问已有URL。没有接入D的每周调度，sources.enabled仍为false。

## D 的读取方式

固定ID从manifest的`catalog.entities`读取；实际普通查询能力为：机场`airport_throughput/month`，黄山北站`rail_departures/day`（仅2025-04-06），景区尚无普通时序值。D注入指标工具时将`metric_code`显式映射到`SeriesQuery.metric`。

```python
from datetime import date
from data_foundation import Page, SeriesQuery, create_store

store = create_store(database_url, data_root=data_root)
with store.read() as reader:
    series = reader.observations.query_series(SeriesQuery(
        entity_id="28380b0b-e209-5ced-8d09-58854248f26e",
        metric="airport_throughput", grain="month",
        start=date(2025, 1, 1), end=date(2025, 11, 30),
    ))
    documents = reader.knowledge.list_documents(entity_id=None, page=Page(1, 20))
    detail = reader.knowledge.get_document(documents["items"][0]["id"])
store.dispose()
```

序列provenance新增`asset_url`，直接指向具体原件；既有`source_url`保留来源目录URL。文档列表返回`items/page/page_size/total`，按发布时间降序、ID排序；详情含`entity_ids`。不存在的文档或筛选实体抛NotFound。D继续负责认证、HTTP序列化和输出白名单；接口不包含文件内部路径。

## E 的资料使用方式

manifest.documents的ID已在数据库登记，每个条目给出原件key、TXT路径、发布单位、链接、日期精度、适用周期和对应实体。可先解析民航局的两份统计PDF，再接景区正式报告；铁路资料标明“安徽日报报道”，不能冒称铁路原始报表。

年/季/半年数值不通过metric_query伪装成月度；在历史说明中引用对应资料。统计资料valid_from/valid_until表示适用的历史统计周期，不代表当前旅游规则。仅公开到日的文件以00:00归一化，并明确标为date精度。未完成E索引前，应保持pending/证据不足状态。

实际验收报告见 [MySQL](../../acceptance/C-0.1.2-real-data/mysql.json) 和 [SQLite](../../acceptance/C-0.1.2-real-data/sqlite.json)。未向D/E发送消息，也未修改其他成员仓库。
