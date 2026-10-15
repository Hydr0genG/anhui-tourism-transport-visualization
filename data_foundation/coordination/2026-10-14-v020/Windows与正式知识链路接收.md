# C 0.2.0a2 → D/E 接收执行单

> 2026-10-15 更新：E 正式包、样本及 Windows 实机回执已收到，C/E 独立 MySQL 17 项通过；原 A/B 部分材料缺失已解除，D/E 正式联合链路仍 pending。最新版本、证据及 D 剩余动作见 [接收交接](../../../C接收E051与D正式MySQL切换交接-2026-10-15.md)。本页其余内容保留原执行单，不代表各项已经联合通过。

日期：2026-10-14。此文件是接收方操作清单，不是已通过回执；尚未发送。C 本机结果与缺口见 [a2 交接](../../25-Windows索引兼容修复与020a2交接.md)。三个团队文档中的日志/样本路径没有实际附到本仓库，仍由对应成员补齐。

## A. E：先放行 Windows C 原生接收

1. 校验 ZIP、wheel 和 contents 的 SHA256，与外部 `receipt.json` 一致；检查 ZIP 内逐文件摘要。解压到新目录，不混入 a1 或候选补丁。禁止猴子补丁替换 persist_file、ctime 处理或错误分类。记录 Windows 版本、Python、MySQL、C wheel 摘要和安装来源。
2. 在干净接收环境安装，以下 PowerShell 从解压后的 `data_foundation` 目录执行。使用依赖已安装的独立环境亦可，但必须证明包来源。全量回归需要 `[data]` 依赖；本次两组文件回归不需要 PDF 依赖。

```powershell
$env:PYTHONUTF8 = "1"
py -3.13 -m venv .venv-receive
$py = (Resolve-Path ".\.venv-receive\Scripts\python.exe").Path
& $py -m pip install ".\release\anhui_data_foundation-0.2.0a2-py3-none-any.whl" "pytest>=8,<10"
Get-FileHash ".\release\anhui_data_foundation-0.2.0a2-py3-none-any.whl" -Algorithm SHA256
# 避开解压目录源码遮蔽：将外部 tests/examples/alembic/assets/real_data 复制到
# 一个新接收目录，不复制 data_foundation Python 包，再在该目录运行下面命令。
& $py -c "import data_foundation as c; print(c.__version__); print(c.__file__)"
& $py -m pytest tests/test_index_artifacts.py tests/test_artifact_windows_compat.py -ra
```

`$py` 应在切换目录前改为 Resolve-Path 得到的绝对路径；包来源必须是接收虚拟环境的 site-packages，不是源码目录。不能只检查版本字符串。Windows 无 `os.mkfifo` 时记录该用例 skip 原因；目录拒绝和原生 os.replace 用例必须运行。若符号链接创建权限不够，不删除安全检查：启用 Windows 开发者模式/合适权限重跑，或逐例记录真实 skip 及残留覆盖缺口，不计为完整通过。

3. 管理员提供独占空 MySQL `_test` 库，设置 `TEST_DATABASE_URL`（密码不入报告），在该接收目录运行：

```powershell
& $py examples/verify_index_artifacts.py --data-root C:/c020a2-receive/artifacts --report C:/c020a2-receive/mysql-artifacts.json
```

该脚本拒绝已有表，执行迁移到 0006，并使用正式 C 原生 persist_file → activate_index → get_index_artifact_ref，含并发恢复/文件变化/回滚复验。以上合成字节测试只放行 Windows C 存储，不证明 E 真 JSON 已加载。

4. 用 E 0.5.1 的真实 JSON 产物在 Windows 执行原生落盘、索引激活、新 store 重新读取和 E 验证加载器加载。回传 C 包 SHA256、E 提交/包 SHA256、实际 doc/job/index ID、产物 SHA256/大小、日志与各用例结果。安装包不一致或任一失败则先修复，不用候选副本通过来关闭状态。

## B. E：补齐 0.5.1 正式交付

- 可安装发布包（wheel/其完整依赖交付方式）、提交号、SHA256、实际安装及模块来源证明。
- 实际 `index.json` 和 manifest 文件本体，不只给路径；保留 UTF-8 JSON/schema_version=1/text-parser-0.5.0/keyword-tfidf-0.5.0，不扩展向量结构。
- 验证加载器入口与调用签名、返回形状、typed errors；文档 UUID、document_version、index_version、chunk_no 连续性、文本及 locator 与 C 数据库逐项一致性。
- 原活动产物坏/缺失不得使用缓存或旧内存回答；摘要正确但 JSON 内容不同必须拒绝。提供各失败测试和重启加载证据。

## C. D：按正式包切换 C/MySQL 知识任务

只由 D 运行调度，禁止 C jobs 与桥接 SQLite 两份状态并用。先用 C 查询登记的 source/asset/document/document_version，enqueue 的 payload 绑定它们和新 index_version；领取时可用 job_type/job_id 精确筛选。E handler 只处理 D 提供的租约及输入。

必须给出以下实际代码/日志顺序（日志不打印令牌或内部路径）：

```python
with store.write() as uow:
    claimed = uow.jobs.claim_job(worker_id, now, job_type="knowledge_index", job_id=job_id)
# 此处领取事务已退出；D 负责在独立短事务 heartbeat/recover。
lease = JobLease(**claimed["lease"])

# 使用正式 E 0.5.1 构建器，具体签名以 E 包为准。
# 构建和 store.persist_file 都在事务外；不要凭此伪造 E API。
artifact = store.persist_file(source_id=source_id, original_name="index.json", stream=stream)
with store.write() as uow:
    index = uow.knowledge.begin_index(document_id=document_id, version=index_version,
        job_id=lease.job_id, parser_version="text-parser-0.5.0",
        embedding_model_version="keyword-tfidf-0.5.0", lease=lease)
for batch in chunk_batches:  # 每批不超过 C 现有 1000 上限
    with store.write() as uow:
        uow.knowledge.write_chunks(index["id"], batch, lease)
with store.write() as uow:
    uow.knowledge.activate_index(document_id=document_id, version=index_version,
        artifact_path=artifact["relative_path"], artifact_sha256=artifact["sha256"],
        expected_artifact_byte_size=artifact["byte_size"],
        expected_chunk_count=chunk_count, lease=lease)
    uow.jobs.complete_job(lease, {"index_id": index["id"]}, completed_at)
# 任何异常传播出上下文整体回滚；不捕获后继续提交。
```

D 核对旧回执“activate/complete 后才 persist”是记录错误还是实现错误。提交文件先存在的证据，以及每批事务/最终事务 ID 或对应代码行。上述只是 C API 片段，不代替 E handler 的实际适配。

问答仅配置 E 正式验证 `knowledge_snapshot_loader`，与 `knowledge_index_path` 互斥。C 未提供 StorageUnavailable，D 仍负责映射存储/验证加载失败为脱敏 503。HTTP 白名单不得包含 relative_path、artifact_path、产物字节、lease_token、数据库 URL、Cookie 或认证摘要。

## D. 一份历史公告验收矩阵

document_id `0c8e2020-2e0a-5842-bce9-d9193cf81095`；document_version `sha256:29aa803a0fa3e009feed5ce3aa07330e`；适用 2025-01-28 至 2025-02-04；来源及时区见 C manifest。每次真正联调新建 index_version/job，不拿独立 C 测试库里的任务作为正式任务。

| 用例 | 期望 | 必需证据 |
|---|---|---|
| 历史问答 | 回答包含文档、片段、来源链接和适用期 | 脱敏真实 HTTP 请求/响应；数据库 doc/job/index ID |
| 当前安排问答 | 明确历史资料不能证明当前运营 | 真实响应，不能仅断言算法有此分支 |
| Worker 和 API 重启 | 同一已提交文档/活动产物仍加载引用 | 重启前后 PID/启动记录、响应及 ID |
| 新产物摘要错 | 发布拒绝，旧活动版本不切换 | 故障前后 active_index_version、任务状态 |
| 活动产物损坏/缺失 | 两类均返回 503，无内存回退 | 实际响应，E 验证加载错误脱敏摘要 |
| activate 异常 / complete 异常 | 分别整体回滚 | 活动版本、index 状态、job 状态提交前后 |
| 旧租约 / 新租约 | 旧租约拒绝；同片段重传 skipped，无重复；新租约成功 | attempt_no、片段数、发布结果，令牌遮蔽 |
| SHA 正确但 JSON 与数据库文本/locator 不同 | E 拒绝加载，HTTP 503 | 重算 SHA 后注入不一致的真实失败测试 |

测试在专用联调环境注入故障，不破坏正式用户产物；异常产物/旧租约场景单独建测试版本。不得为了“验收”直接篡改生产活动文件。

## E. 回执字段与关闭规则

回传者填写：C/D/E 实装版本及提交；包 SHA256；Windows/Python/MySQL 版本；迁移 head/表数；document_version；正式 document/asset/job/index ID；产物 SHA256/大小；用例成功/失败/跳过原因；脱敏 HTTP；调用顺序证据文件。内部绝对路径、令牌和完整字节不进入 HTTP 回执。

分别记录 `C_independent_storage`、`Windows_official_C_receiving`、`formal_DE_knowledge_chain`。目前第一项已有 C 本机证据，后两项 pending。只有正式链路全部通过才恢复全省爬虫第二步；任何失败先修对应接收环节。G18、视频人数、DM-Count 不作为本轮阻塞或假通过项。
