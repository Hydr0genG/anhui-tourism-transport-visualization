# D 回复 C：交接包验收回执（2026-10-11）

> 针对 `C给DE交接包-2026-10-11`（anhui-data-foundation 0.1.0）。
> 按《08-C到DE交接说明》第 8 节回执模板填写；**发现一个 MySQL 专项 Bug，见第二节，需要 C 修复后重发**。

## 一、回执

```text
接收方：D
包版本：0.1.0（迁移 head 0004）
Python / MySQL 版本：Python 3.13.16 / MySQL 8.4.11（均为 C/D/E 约定版本，Windows 本机服务）
运行范围：--part all（默认全量 18 项）
运行环境：local(SQLite) 与 mysql-test 均已运行

报告 status / check_count：
  local(SQLite)：passed / 18（一次通过）
  mysql-test（原样运行）：failed —— AssertionError「测试队列不是独占的，不能领取预期任务」
  mysql-test（D 侧将样例 utc_now 截断微秒后重跑）：passed / 18

通过的调用：SQLite 全部 18 项；MySQL 在绕开下述时间精度问题后全部 18 项
（认证/事务回滚/导入幂等与修订/缺期与覆盖率/不支持粒度拒绝/预测不足保存/
 知识片段写入去重与冲突/视频计数与完成发布/短时预测不足保存/作业失败读回）。

失败的调用、错误 code 和需要修改的字段：见下节 Bug 报告。

H01—H06 的确认/修改意见：见第三节。

真实文档/录像/模型联调：未进行。
下一动作与负责人：C 修复 DATETIME 精度问题并重发包 → D 开始路由实调适配（计划见第四节）。
```

## 二、Bug 报告：MySQL DATETIME 无小数秒精度，同秒内入队的作业领不到

**现象**：`--target mysql-test` 下，`e_knowledge` 环节 `enqueue_or_get` 之后立即 `claim_job` 返回 `None`，样例断言「测试队列不是独占的」失败。失败概率约 50%，与当前时刻的微秒值有关；SQLite 模式从未失败。

**根因**：`jobs.available_at` 等时间列为 `DATETIME`（无小数秒）。MySQL 对带微秒的值做**四舍五入**而不是截断：

```sql
SELECT CAST('2026-10-11 08:12:56.700' AS DATETIME);
-- → 2026-10-11 08:12:57  （进位到下一秒）
```

时序：`enqueue_or_get(JobCreate(...), now)` 时 `now = 08:14:07.700`，MySQL 把 `available_at` 存成 `08:14:08`；紧接着 `claim_job(worker, now)` 用同一个 `now` 判断 `available_at <= now` → `08:14:08 <= 08:14:07.7` 不成立 → 返回 `None`。SQLite 保留微秒全精度，所以 C 本地测试全过。

**最小复现**（D 的 venv + 空库即可）：

```python
from datetime import datetime, timezone
from data_foundation import create_store, JobCreate

store = create_store("mysql+pymysql://.../xxx_test", allow_sqlite_for_tests=True)
now = datetime.now(timezone.utc).replace(microsecond=700000)
with store.write() as w:
    w.jobs.enqueue_or_get(JobCreate("knowledge_index", "repro-1", {"x": 1}, "复现"), now)
with store.write() as w:
    print(w.jobs.claim_job("worker", now))   # → None（领不到）
```

**D 侧验证**：把样例脚本的 `utc_now()` 换成微秒截断版本（`replace(microsecond=0)`）后在 MySQL 重跑，18 项全部通过——证明包的其余部分（迁移、存储、事务、知识/视频/预测保存）在 MySQL 8.4.11 上没有问题。

**修复建议**（建议 1，任选其一）：

1. 迁移把时间列改为 `DATETIME(6)`（保留微秒，与 SQLite 行为一致，根治）；
2. 或写入前在 Python 侧统一 `now.replace(microsecond=0)`（影响所有入口，容易漏）。

**影响范围提示**：不止 `jobs.available_at`——凡是存「当前时刻」并与 `now` 做边界比较的列（如 `sessions.expires_at`）都有同类四舍五入风险，建议一并检查。

## 三、H01—H06 确认（D 侧）

| 编号 | D 的回复 |
|---|---|
| H01 | **确认按实际方法适配。** D 的 providers 将按 `create_store(database_url, data_root=...)` + `uow.auth` 五方法新写桥接（`save_session` / `get_active_session` 等）；`get_active_session` 返回的 `{session, user}` 由 D 做字段白名单后输出，不透传哈希。不需要 `new_auth_store` 兼容层，D 会把现有兼容代码移除。 |
| H02 | **确认 D 进程为唯一 Worker。** `app/worker.py` 领取（`claim_job` 无类型过滤 → D 领取后按 `type` 分发），E 提供算法函数，写回走 C 的 knowledge/video 仓储 + `JobLease`；心跳与失败由 worker 调 `heartbeat` / `fail_job`。D 现有的 SQLite 桥接作业库（`d_` 前缀表）按既定计划退役，统一用 C 的正式 `jobs` 表。 |
| H03 | E 回复（转给 E）。 |
| H04 | **D 侧优先级**：① `get_document` / `list_documents`（21 号接口 + 上传后查询依赖）② `claim_job` 类型/ID 过滤（避免领到不该处理的作业，也让样例不再依赖「独占库」假设）③ 视频读取/回放引用（23/24/25 号接口）④ `list_events` 补 `kind/entity_id/authority/since/until` 筛选（03 契约要求，当前只有 region_code/status）。**名称用当前名**（`activate_index` 等），不需要旧名兼容别名。 |
| H05 | **同意。** 事件 17/18 号路由由 D 加可选 `source_asset_id` / `new_source_asset_id` 字段（OpenAPI 同步更新，通知 A/B）；旧请求仅唯一解析时成功，禁止取「最新一篇」凑数。时序响应暂不放 `provenance`（D 内部保留追溯），需要前端展示时再按可选字段扩展——届时另行通知。 |
| H06 | 资料侧事项，不归 D。 |

## 四、D 下一步计划（等 C 重发包后执行；顺序即优先级）

1. providers / auth_service 适配 C：`FakeAuthStore` → C 的 `uow.auth` 五方法（`create_admin` 脚本改调 `auth.create_user`）。
2. 读取类路由实调：05/06/07（目录/指标/来源）、13（假期）、15/16（事件读取，受 H04-④ 限制）、08/09（时序 `query_series`，输出做字段白名单）。
3. 12 号历史预测：`query_series` 喂 D 已实现的季节基线算法 + `save_history_forecast` 落库。
4. worker 从 SQLite 桥接切换到 C 的 jobs + 租约（H02 已确认的分工）。

D 侧说明：`pyproject.toml` 已含 `python-multipart`；wheel 的 SQLAlchemy 2.0.x / Alembic / PyMySQL 依赖装入 D 的 venv 无版本冲突。正式库 `tourism_flow` 尚未执行迁移（保持空库，待适配联调时按流程执行）。
