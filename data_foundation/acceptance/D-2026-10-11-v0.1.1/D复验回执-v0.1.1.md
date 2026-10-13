# D 复验回执：data_foundation v0.1.1（MySQL 时间精度修复）

> 写的人：D（后端 api_platform）
> 日期：2026-10-11 晚
> 对象：C（交接包 v0.1.1，迁移 head 0005）
> 结论先行：**v0.1.1 验收通过，无新问题。**D 回执中的 MySQL 时间精度缺陷已确认修复，两条复验路径（新空库 / 已有 0004 库升级）均按你 §5 回复稿的要求完成。

---

## 一、按你的回复稿逐项反馈

```text
D 复验结果（Windows 接收环境，Python 3.13.16 / MySQL 8.4.11 LTS）：

1. 版本与迁移：package_version=0.1.1，migration_head=0005，与预期一致。
2. SQLite 全量样例：21/21 passed，含 precision-job / precision-lease / precision-session 三项。
3. 真实 MySQL 新空库（原样运行，未截断 utc_now 微秒）：21/21 passed，三项 precision 检查通过。
4. 77 个 DATETIME 列精度核查（INFORMATION_SCHEMA）：新空库与升级库两处验证，均为 77 列、精度全部 = 6。
5. 已有 0004 库升级路径：D 无现成 0004 库（上轮测试库已删、正式库从未迁移），
   故用 0.1.0 wheel 重新造了一个 0004 存量库（写入 1 用户 + 1 作业）完整走你的 §4 步骤。
   alembic current：0004 → upgrade head → 0005；存量行保留可读；升级后新写入微秒
   （.700000）原样读回；对微秒作业做同时刻领取成功。
6. D 原回执最小复现（同一 now 带 .700000 微秒 入队→立即领取）：0.1.0 失败 / 0.1.1 成功。
```

## 二、复验环境与方法（可复现）

| 项 | 值 |
|---|---|
| 操作系统 | Windows（D 的开发机） |
| Python | 3.13.16（C/D/E 统一版本约定） |
| MySQL | 8.4.11 LTS，本机服务，仅 127.0.0.1 |
| 包安装 | `pip install --upgrade ./data_foundation/release/anhui_data_foundation-0.1.1-py3-none-any.whl`，`import data_foundation; __version__ == "0.1.1"` |
| ZIP 完整性 | `sha256sum -c SHA256SUMS.txt` 31/31 全部 OK |
| 微秒截断 | **未使用**。本轮所有 MySQL 运行均为原样 `de_handoff_smoke.py`，与 0.1.0 验收时的绕过手段不同 |

## 三、已有 0004 库升级路径的验证细节

D 没有遗留的 0004 测试库，为完整验证你的升级路径，按以下方式构造：

1. 安装 0.1.0 wheel → 用其 alembic 建库至 head 0004（30 张业务表）
2. 用 0.1.0 的 `create_store` 写入存量数据：1 个用户、1 个 `knowledge_index` 作业
3. 重装 0.1.1 wheel → 按你的 §4 步骤：`current`=0004 → `upgrade head` → `current`=0005
4. 核查结果：
   - `INFORMATION_SCHEMA.COLUMNS`：77 个 datetime 列，`MIN/MAX(DATETIME_PRECISION)` 均为 6
   - 存量用户、存量作业均保留且可正常读取（status/payload 不变）
   - 升级后新写入：`available_at = 2026-10-11T12:49:06.700000` 写入后原样读回
   - 同时刻领取：`claim_job(worker, now=available_at)` 成功领到该微秒作业
   - MySQL ALTER 过程无报错；遵守了你提示的"失败不视为可自动回滚"，本轮未触发

一点如实说明：升级库验证时 D 第一次断言"同时刻领取失败"，排查后确认是 D 测试脚本的问题——队列里 0004 时代的存量作业 available_at 更早，`claim_job` 按 FIFO 先领走了它（属正常行为）。对微秒作业单独验证后领取成功。**不是 C 的缺陷，无需处理。**

## 四、D 侧受影响面回归

- api_platform 全量 **69 项测试通过**（0.1.1 下回归，无破坏）
- venv 中与 E 的 `intelligent_analysis` 包共存，无依赖冲突
- D 的 SQLite 桥接作业库（d_ 前缀表）不受影响；切换到 C 的正式 jobs 表仍按原计划在 H01/H02 适配阶段执行

## 五、metric_dictionary.json v0.1.0 已收到

5 个指标（scenic_visits / rail_departures / airport_throughput / camera_entries / camera_exits）的口径、单位、实体类型、聚合规则齐备。**D 的 P1（智能体指标查询注入白名单）就此解锁**，将按字典 v0.1.0 实现：注入函数只接受 `(entity_id, metric_code, grain, start, end)` 结构化参数，白名单以本字典为准。若字典后续升版请同步 D。

## 六、遗留与下一步（对齐用）

| 项 | 状态 |
|---|---|
| H01 auth 适配（包装 `uow.auth` 为认证存储） | D 计划下一步实施，用 0.1.1 的五方法签名 |
| H02 D 唯一 Worker + 按 type 分发 | 已确认，worker 切 C 正式作业表的时机随 H01 |
| H04 接口补充（get_document / list_documents / 失败生命周期等） | 知悉在你后续优先级，D 的 21 号路由暂以桩/桥接过渡 |
| H05 source_asset_id 扩展 | 已确认 |
| C 的 DATETIME(0) 历史舍入不可恢复 | 知悉；D 无历史库受影响 |

附件：`c-v011-local-sqlite.json`（SQLite 21 项）、`c-v011-mysql.json`（MySQL 新空库 21 项，原样运行）。

以上任何一条需要补充验证的，直接回对应节号。
