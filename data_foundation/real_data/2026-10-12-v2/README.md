# 公开资料补充包与 D/E 调用说明

数据集 `anhui-public-supplement-2026-10-12`，配套 C 0.1.3；迁移仍为 0005。首批目录 `2026-10-12` 和 0.1.2 发布包保留。本目录是完整累计快照，含首批数据，可独立验证及导入。

| 内容 | 首批 | 本次累计 |
|---|---:|---:|
| 服务器 PDF/HTML 原件 | 96 | 101 |
| 直接读取官方网页后的正文文本快照 | 0 | 5 |
| 已核验普通日/月观测 | 83 | 83 |
| 原周期补充事实 | 13 | 17 |
| 全国统一假期窗口 | 0 | 26 |
| 历史官方关闭事件 | 0 | 1 |
| 已登记知识文档 | 6 | 16 |

新增四份国务院 2023—2026 年放假安排、安徽日报原版 PDF，以及五份官方网页的正文采录。正文快照含来源、发布时间、采录范围和摘要；统计/实绩两篇仅采录相关段落，三篇公告/提示采录正文全文。它们不是服务器 HTML 字节原件，也不是搜索摘要。详见 [采录记录](browser_captures.json)。

新增景区事实：2024 年 1 月 17.1 万人、2024 年 8 月 67.3 万人、2024 年 1—8 月累计 329.44 万人、2025 年五一五天 14.7 万人次。来源分别为统计局、安徽日报、管委会。原文范围称“黄山风景区”，与已有“主景区进山游客”尚未明确核对，标为 `pending_scope_confirmation`；不导入主景区普通时序。五一实绩不作为五月月值，累计值不相减补造月份。

## 验证和导入

从交接包 `data_foundation` 目录运行：

```bash
python -m pip install release/anhui_data_foundation-0.1.3-py3-none-any.whl
python examples/import_real_dataset.py --manifest real_data/2026-10-12-v2/manifest.json
```

默认只做离线完整性、口径和原文核验，不访问数据库。D 配置 `DATABASE_URL` 和 `DATA_ROOT` 后执行：

```bash
python -m alembic upgrade head
python examples/import_real_dataset.py --manifest real_data/2026-10-12-v2/manifest.json --apply
```

导入器登记来源/原件、83 条普通观测、16 份文档、26 个假期和 1 条历史事件。17 条补充事实仍保留在 JSON，未伪装成普通观测。分批事务失败可能已有部分批次完成，修复后重跑即可；本次新增日历/事件同样支持重复导入。没有将真实资料写入用户目标业务库；独立验收通过不等于 D/E 接收或 HTTP/Worker 验收。

## 假期和事件读取

```python
with store.read() as uow:
    holidays = uow.holidays.list_holidays(year=2026, region_code="000000")
    may_day = uow.holidays.list_holidays(year=2025, kind="may_day")
    historical = uow.events.list_events(region_code="341000", status="expired")
```

`000000` 表示全国通用范围；D 查询地区假期时应同时取全国记录，不能仅以 `341000` 筛选。全国范围为 `CN:national_general_public`，不包含学校寒暑假。kind 包括 `new_year/spring_festival/qingming/may_day/dragon_boat/mid_autumn/national_day`。2023、2025 年中秋国庆合并发布，只存一个 `national_day` 窗口，日历 JSON 的 `components` 记录两个节日，避免重复计数。

2024 元旦与端午按原文“与周末连休”纳入紧邻周末；2024 除夕鼓励休息不算全国统一放假。跨年元旦按通知年度查询，窗口起点可能在上一年。调休工作日、归一化说明、原文条款位于 [日历文件](holiday_calendar.json)，数据库保留窗口、来源和原件绑定。

唯一事件为 2024 年 7 月 26—27 日部分景点关闭，状态 `expired`。起止时刻是日期边界归一化，不能解读为实际关门时刻。两份运营公告/提示已补回正文采录，适用日期分别为 2025 年春节、2026 年五一；即使原网页标“有效”，也不能据此回答当前开放、天气或预警问题。

## E 的资料入口

读取 `manifest.documents` 的 `text_path`、`applicability_note`、`permission_note`，或使用 C 文档详情/列表。16 份文档全部为 `pending`，尚未生成 E 索引。数值问答须检查事实的 `quality_status/scope_note/grain`；待确认范围的事实可带出处说明原公布值，不应作为主景区曲线或预测输入。完整原件摘要在 assets；document_version 采用摘要前 32 位以符合 64 字符字段。

## 离线重新准备

```bash
python -m pip install 'release/anhui_data_foundation-0.1.3-py3-none-any.whl[data]'
python examples/prepare_public_supplement.py --root real_data/2026-10-12-v2 --baseline real_data/2026-10-12
python examples/import_real_dataset.py --manifest real_data/2026-10-12-v2/manifest.json
```

此步骤使用已保存原件和可审计正文采录，不联网。网络 `source_plan` 仅用于服务器原件重取；浏览器采录需人工重新读取官方页面并复核，不能用下载失败页或搜索摘要替换。原件变化后须重新核验。

仍缺：机场三年十二月原始单月实绩、景区/铁路连续同口径月报、学校校历、授权入口录像和人工进出标注；详见 [覆盖及充足性报告](quality_report.md)。
