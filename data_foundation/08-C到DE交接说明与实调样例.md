# C→D/E 交接说明与实调样例

> 当前入口：2026-10-14 C0.1.6 / 迁移0006。0.1.0历史限制不适用于新版已实现的进度、重试、索引/分析失败及视频读取。请先读[0.1.6交接](./16-任务生命周期与016交接.md)；下方历史调用仅作背景。

日期：2026-10-11；C 包：`anhui-data-foundation 0.1.1`，Python import 名：`data_foundation`；迁移 head：`0005`。本说明已更新至 MySQL 微秒精度修复版本，复验步骤见[修复与回复 D](./10-MySQL时间精度修复与回复D.md)。本说明按当前代码核对，供 D/E 安装、适配和反馈。与[实装核对及缺口清单](./09-接口实装核对与缺口清单.md)一起阅读。

**当前结论：可交接最小存储流程和测试样例；C 独立 MySQL 8.4.11 合成存储复核通过；D 新版复验、E 环境实调、真实资料与完整设计接口仍待完成。**实际方法、参数和限制以本页及 09 的代码清单为准；04 的后续设计部分不表示都能调用。

## 1. 可直接发给 D/E 的说明

```text
D/E：

C v0.1.1 的最小存储流程已在本地通过。请先按本说明运行
examples/de_handoff_smoke.py，再回传版本、数据库类型、通过/失败项。

重点对齐：
1. create_store(database_url) 返回 DataStore；认证方法在
   store.read()/store.write() 的 reader.auth/writer.auth 内。
   真实方法为 save_session、get_active_session，不是 create_session/get_session。
2. 索引激活用 knowledge.activate_index，不存在 activate_index_version。
   视频 create_analysis 参数顺序为 (video_id, job_id, config)；
   complete_analysis 为 (analysis_id, expected_window_count, lease, manual_validation=None)。
3. D 运行唯一 Worker；请 D/E 确认谁领取任务、谁调用 E 的处理函数、
   谁通过 C 写回，以及失败/重试责任。C 提供事务和租约入口。
4. get_document、get_video/get_playback_ref、手工标注读写、索引/分析失败接口、
   作业自动恢复/重试等仍是 C 的待实现项，详见 09。不能按旧设计直接调用。
5. 样例资料全部为合成数据。真实知识文档、授权录像、人工标注及历史序列
   尚未交付，不能以样例结果代表真实问答、计数或预测效果。

样例和说明已准备好；目前状态为“v0.1.1 C 本地通过，D 待复验、E 待验收”。
请按本文末尾回执反馈需要调整的接口与首次实调结果。
```

此文本是交接稿，尚未代用户向 D/E 发送。

## 2. 安装包与配套文件

接收方需要 C 目录中的 wheel、`alembic.ini`、整个 `alembic/`、`assets/metric_dictionary.json`、样例脚本和本说明。wheel 包含 Python 代码和默认字典，**不包含仓库级迁移目录、样例和说明文件**；只拷 wheel 不足以完成迁移实调。

从项目根目录执行，使用 D/E 自己的 Python 3.11+ 环境：

```bash
# 正常安装；pip 按包声明安装运行依赖
python -m pip install ./data_foundation/release/anhui_data_foundation-0.1.1-py3-none-any.whl

# 运行依赖为 SQLAlchemy 2.0.x、Alembic 1.14+（<2）、PyMySQL >=1.1,<3（以安装包依赖声明为准）
python -c "import data_foundation; print(data_foundation.__version__)"
```

运行依赖已经装好时，可用 `python -m pip install --no-index --no-deps ./data_foundation/release/anhui_data_foundation-0.1.1-py3-none-any.whl` 离线安装。开发环境可改用 `python -m pip install -e ./data_foundation`。安装完成应输出 `0.1.1`。正式配置由 D 提供：`DATABASE_URL` 使用 `mysql+pymysql://...`；`DATA_ROOT` 为持久化资产根目录。C import 时不自动读取 .env、不自动连库或建表；`create_store` 接收显式参数，关停时调用 `dispose()`。

## 3. 先跑不依赖 MySQL 的本地样例

[样例脚本](./examples/de_handoff_smoke.py)默认新建临时 SQLite，调用真正的五段 Alembic 迁移，运行合成数据存取，退出后清理临时库和资料。它不使用现有 `DATABASE_URL`，不会把测试资料写入已配置的业务库。

```bash
# 从项目根目录运行；一次跑 D/E 两组
python data_foundation/examples/de_handoff_smoke.py --report .tmp/c-de-handoff-local.json

# 也可分别运行，便于定位接口问题
python data_foundation/examples/de_handoff_smoke.py --part d
python data_foundation/examples/de_handoff_smoke.py --part e
```

预期：退出码 0，JSON 的 `status=passed`、`database_dialect=sqlite`、`package_version=0.1.1`、`migration_head=0005`、`scope=synthetic_storage_only`。`all` 有 21 个检查项；`d` 和 `e` 各有 13 个（包含共用的迁移、三项微秒边界检查和资料准备）。这不是 21 个真实业务功能或算法测试。

| 样例组 | 输入与动作 | 应观察到什么 |
|---|---|---|
| 共用 | 空临时库、五段迁移、五指标及合成实体/来源 | 30 张业务表，doctor 可连接 |
| D 认证 | 合成用户与会话；查询、到期、撤销 | 内部用户查询有哈希，会话用户无密码哈希；撤销后查不到 |
| D 事务 | 在 read 中尝试写；写入后故意异常 | 只读写入被拒绝，异常退出写事务回滚 |
| D 客流 | 一条合成月度 CSV，dry-run→apply→重复→修订 | dry-run 无观测；首次新增 1；重复跳过 1；修订后版本 2 |
| D 查询 | 三个月仅一个月有值，另查周粒度/非法白名单 | 两期缺失；不支持粒度明确报错；非法指标拒绝 |
| D 预测保存 | 一个不足状态、空预测点和原因 | save_history_forecast 返回 data_insufficient，不产生预测点 |
| E 索引 | 合成 MD、两个手工片段、一个 JSON 产物 | 初始未就绪；写入/重传/冲突正确；activate_index 后读回两片段 |
| E 视频 | 合成授权、非真实视频字节、两个五分钟计数 | 仅验证元数据/存储；未成功时不能查询，重传跳过、冲突拒绝 |
| E 发布 | 完成分析和作业放在同一 write 事务 | analysis/job 成功后读回两窗口，进入值为 8 和 6 |
| E 预测/作业 | 不足预测、越过截止的输入、完成后续约、失败作业 | 不足无点；未来输入拒绝；完成租约不能续期；失败状态可读回 |

测试用户名的密码哈希只是占位符，不可用于真正的密码校验。视频字节不是真实录像且不允许回放；片段手工给定，没有执行 RAG 检索、YOLO/OpenVINO 或预测算法。历史修订旧快照由原有自动化测试验证，样例通过公开查询只检查新值与版本。

## 4. 再由 D 提供独占 MySQL 测试库

样例的 MySQL 模式需 `TEST_DATABASE_URL`，库名以 `_test` 结尾且库内无任何表；资产目录也必须为空。这个限制用于防止测试夹带业务数据。脚本负责迁移，**不要先在该库手工执行 upgrade**。每次复测另备空库/空目录，样例不删表、不清空已有库。

```bash
# TEST_DATABASE_URL 由接收方在本地终端设置；不把真实值写进说明或回执
python data_foundation/examples/de_handoff_smoke.py \
  --target mysql-test \
  --data-root /absolute/path/to/empty-test-assets \
  --report .tmp/c-de-handoff-mysql.json
```

PowerShell 可写成单行，资产目录用本机绝对路径。预期仍为 21 项通过，`database_dialect=mysql`。迁移和存储通过不等于多 Worker 并发、任务恢复、回放授权或完整 HTTP 通过。C 已收到 D 对旧版的 MySQL 缺陷回执及修改样例后的通过报告；C 新版已在独立 MySQL 8.4.11 上通过 21 项及已有库升级检查；D 的接收环境新版复验仍需回传。

## 5. D：怎样调用 C

### 5.1 认证、事务与返回字段

```python
from data_foundation import create_store, SessionCreate

store = create_store(database_url, data_root=data_root)
with store.read() as reader:
    user = reader.auth.get_user_by_username(username)
# user 为 dict 或 None。密码校验、哈希算法、限速、Cookie 和 CSRF 由 D 实现。

with store.write() as writer:
    saved = writer.auth.save_session(
        SessionCreate(id_hash=session_hash, user_id=user["id"], expires_at=expires_at), now
    )
with store.read() as reader:
    active = reader.auth.get_active_session(session_hash, now)
# active = {"session": {...}, "user": {...}} 或 None
with store.write() as writer:
    changed = writer.auth.revoke_session(session_hash, now)
```

上述为流程片段；用户名、已验证用户、摘要和带时区时间由 D 的认证服务提供，可运行版本见脚本 `d_auth`。不要把 `new_auth_store`、`create_session` 或 `get_session` 当成 C 已导出的函数。认证 facade 若 D 需要，放在 D 的 provider 中，短事务内调用上述入口。

`read()/write()` 目前都会产生包含各仓储的工作单元；受约束写方法在只读状态拒绝操作。成功退出 write 提交，抛异常退出回滚。不要把 `uow` 或 Session 带出上下文，也不要把一次视频推理/文件下载包在数据库事务中。

输入有 dataclass DTO，也有普通 dict；大部分结果是普通 dict/list，`datetime/date/Decimal` 仍为 Python 对象。D 负责字段白名单和 JSON 序列化；日期时间转换为带时区 ISO 字符串，数值按 HTTP Schema 处理。不得直接把包含 `password_hash/id_hash/relative_path/artifact_path/evidence_path` 或内部 payload 的结果透传浏览器。

### 5.2 查询与指标工具

`reader.observations.query_series(SeriesQuery(...))` 返回实体、指标、点、缺期和覆盖率，点中包含来源和原件追溯字段。样例 `demo_metric_query` 展示 D 包装成结构化、受白名单约束的函数；其白名单仅限合成景区的一项月指标。真实白名单尚待 C 的对象/来源登记，不可照搬。

视频分钟计数从 `video.query_counts(video_id, analysis_id)` 读；当前只有两参数，不提供 start/end 或 interval 聚合。E 与 D 若需要过滤和聚合，需在契约中另行确认并实现，不能直接传额外参数。

### 5.3 结果和错误映射

| 当前 C 结果/错误 | D 应处理为 |
|---|---|
| `InvalidArgument`、`NotFound` | 400、404；HTTP 字段验证仍由 D 请求模型处理 |
| `UnsupportedGrain/MetricEntityMismatch/InvalidPermission` | 422；用户无操作权限由 D 自己返回 403 |
| `Conflict/AnalysisNotReady` | 409，统一错误体；AnalysisNotReady.code 为 analysis_not_ready |
| `QueryLimitExceeded` | 422，提示缩小范围 |
| `LeaseLost` | Worker 停止当前写入，记录内部失败；不直接透传租约 token |
| 数据库驱动/SQLAlchemy 异常 | D 转统一 503，不输出连接或 SQL；C 没有导出 StorageUnavailable |
| 保存预测返回 `data_insufficient` 等状态 | D 按业务响应处理，保持空结果点和原因 |

`jobs.get_job` 返回的主键是 `id`，不是 `job_id`；关联 ID 可在完成时写入 `result_refs`。知识作业用 `document_id/index_version`，视频作业用 `analysis_id`；不要把文档 ID 填进 analysis_id。作业完成前的关联展示需 D/E 另确认，不能假设 Job 已有 analysis_id 列。

## 6. E：怎样写入知识和视频结果

### 6.1 实际顺序

1. D/C 登记原件与文档或录像，创建作业；视频 `create_analysis(video_id, job_id, config)` 与作业创建同事务。
2. 唯一 Worker 调用 `claim_job(worker_id, now)`，将 `claimed["lease"]` 转为 `JobLease(**...)`。
3. 解析或模型推理在事务外运行；独立路径用短 write 事务 `heartbeat`。默认租约 120 秒；长视频需 D/E 实测调整。
4. 知识：`begin_index`→`write_chunks`→产物完整落盘→`activate_index`。视频：`write_counts`→`complete_analysis`。
5. 激活索引/完成分析与 `complete_job` 放在同一个 write 事务。样例已有实际调用，不能省略作业完成。
6. 数据不足时 E 计算状态，C 用 `forecasts.save_short_forecast(data, points)` 保存。两个不足状态也需要原因与空 points。

当前没有 `begin_analysis`：首次 write_counts 会将分析置 running。没有 `fail_index/fail_analysis`：`fail_job` 只更新作业，不会补上索引/分析失败状态。自动恢复、手动重试、进度更新和业务失败接口仍需 C 完成后才能宣称完整 Worker 流程就绪。

`claim_job` 无 type/job_id 过滤，领取任一到期 queued 作业。D 需领取后按 type 分发；不能让两个 E 独立进程都假定只会收到自己的任务。样例仅用于独占测试库，确保队列中无他人任务。

### 6.2 入参形状

| 写入项 | 当前必需内容 | 样例位置 |
|---|---|---|
| 文档 | source_id/asset_id/title/publisher/source_url/published_at/permission_note/document_version；有效期可空；实体 ID 作为第二参数 | `e_knowledge` |
| 片段 | `chunk_no`、`text`、`locator`；非 ChunkInput 类 | `e_knowledge` 的 chunks |
| 索引激活 | document_id/version/artifact_path/artifact_sha256/expected_chunk_count/lease | `activate_index` 调用 |
| 视频分析配置 | pipeline_version/detector_model_version/detector_sha256/openvino_version/device/tracker_config/count_line/entry_side/interval_minutes；tracker_config_id 可空 | `e_video` 的 config |
| 计数 | `CountInput(window_start, window_end, entries, exits, quality_status)`，时间带时区 | `e_video` 的 points |
| 预测保存 | data dict 与 points list 分开；模型、输入快照与摘要、状态/原因；不足时 points=[] | `d_series_and_forecast/e_video` |

HTTP 的七项 AnalysisConfig 不能原样满足 C 的存储配置：pipeline_version、detector_sha256、tracker_config 等由 D/E 内部补充并确认，不要求前端填写模型内部细节。E 的 lower/upper 若保存至 C，需要转为 `lower_bound/upper_bound`；当前样例仅演示不足状态保存。

## 7. 本轮仍需 D/E 明确回复的事项

| 编号 | 谁回复 | 需要确认 | 当前默认/缺口 |
|---|---|---|---|
| H01 | D | 按真实 auth 方法适配，session/user 字段怎样转换 | C 不提供 new_auth_store 或直接四方法对象 |
| H02 | D/E | 唯一 Worker、处理函数签名、谁写回、谁续约/失败 | 默认 D 进程领取并分发，E 提供算法处理；写回细节待确认 |
| H03 | E | locator、pipeline_version、权重摘要、tracker_config 和 artifact 格式 | 目前只验证合成存储，未验证 E 实际模型产物 |
| H04 | D/E/C | 优先补哪些缺失接口，以及使用旧设计名还是当前名 | 见 09；缺口包含文档/视频读取、失败状态和任务恢复 |
| H05 | D/C | 事件请求传精确 asset_id，时序的 provenance 怎样展示 | 不允许凭 source_id 猜某一公告，兼容扩展仍待确认 |
| H06 | C/资料负责人 | 首批对象、文档、授权录像与标注的交付状态 | 当前均未取得真实交付证据；不填写猜测日期 |

## 8. 接收方回执

复制以下内容填写；报告中无需数据库地址、账号、密码、哈希或租约 token。

```text
接收方：D / E
包版本：
迁移 head：
Python / MySQL 版本：
运行范围：--part d / e / all
运行环境：local(SQLite) / mysql-test
报告 status / check_count：
通过的调用：
失败的调用、错误 code 和需要修改的字段：
H01—H06 的确认/修改意见：
真实文档/录像/模型联调：未进行 / 已进行（另附版本与结果）
下一动作与负责人：
```

只有返回真实 MySQL 和接收方运行记录，才更新项目根目录的《06-项目完成图总表》（内部计划，不包含在交接包内）的接收方状态。旧版已有 D 回执；新版 0.1.1 待 D 复验，E 与全组验收仍待完成。

## 9. v0.1.1 微秒精度复验补充

新增 precision-job / precision-lease / precision-session 三项公共检查：任务使用 700000 微秒原值入队后立即领取，租约与会话均在到期前一微秒有效、到期时失效。不要修改 utc_now() 截断微秒。新空库按本文运行；已有 0004 测试库的升级验证见 10，不能使用要求空库的样例直接升级已有库。D 已确认唯一 Worker 分工和按实际方法适配；E 的确认仍待取得。
