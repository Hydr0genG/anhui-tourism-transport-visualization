# C 0.2.0a2：Windows 索引接收修复与真实公告交接

日期：2026-10-14。按本轮批准的小计划执行，0.2.0a2 改为 Windows 兼容修复版；原 a2 的 G18 工作延期。迁移 `0006`、31 张业务表、公开方法签名不变。全省爬虫第二步继续暂停；视频人数存储、DM-Count 权重、预测校验不在本轮范围。

## 1. 本机实际完成与边界

| 验收层 | 状态 | 证据 |
|---|---|---|
| C 源码 SQLite 回归 | 311 项通过 | acceptance/C-0.2.0a2-windows-compat/sqlite-tests.txt |
| C 独立 MySQL 文件规则 | 69 项通过 | 同目录 mysql-artifact-tests.txt |
| C 独立 MySQL 发布竞争 | 6 项通过 | mysql-artifacts.json |
| 任务生命周期/恢复/筛选 | 8 / 6 / 5 项通过 | mysql-lifecycle/recovery/claim.json |
| 分析读取/覆盖/质量 | 6 / 6 / 5 项通过 | mysql-reads/coverage/quality.json |
| 独立安装与完整 ZIP | 以最终回执为准 | release/C-index-artifacts-0.2.0a2-2026-10-14.receipt.json |
| Windows 实机 C 正式修复包接收 | 待 E 回执 | coordination/2026-10-14-v020/Windows与正式知识链路接收.md |
| E 0.5.1 正式包与样本 | 未收到 | 不把旧 0.5.0 或候选补丁算作已接收 |
| D/E 正式 MySQL Worker/HTTP/问答 | 未执行、待接收 | 本仓库没有 D/E 实装目录，不模拟通过 |

测试环境为 macOS / Python 3.12.14（全量、MySQL、隔离安装）及 Python 3.13.16（69项文件规则补充）/ 独立 MySQL 8.4.11。全部 MySQL 使用新建空 `_test` schema；没有连接正式业务库。Windows/Python/MySQL 接收环境版本由 E 填写。C 基线提交为 `ed0400e323b7d48089a6f9b8003807ceca556e01`，本轮改动尚未新提交；内容清单记录实际交付字节，不能把基线提交称为 a2 提交。

## 2. 修复内容

`artifacts.py` 的首次哈希和锁后复核都使用打开的描述符及 `os.fstat()`。复核会重新打开文件，并在成功/异常时关闭描述符；不再把 `Path.stat()` 的 ctime 与 `fstat()` 比较。继续检查 device、inode、size、mtime_ns、ctime_ns 全部五项，不删除任何指纹字段。

Windows 目录可能在 `os.open()` 返回 PermissionError：现在显式分类目录为 `InvalidArgument`；普通文件的权限或读取失败为 `Conflict`；不存在为 `NotFound`。路径规范、DATA_ROOT 边界、所有路径分量的符号链接拒绝、普通文件限制均保留。目录分类所用的路径元数据不参与指纹。

增加 15 项可移植回归：模拟 Windows stat/fstat 时间差，分别改变五项指纹，检查成功、fstat/fdopen/read 异常的描述符关闭，目录/目录替换/普通文件读取失败分类，以及原生 `persist_file` 的临时文件关闭后重命名。原 FIFO 用例在无 `os.mkfifo` 平台用明确原因跳过，`pytest -ra` 留存原因。本机支持 FIFO，因此无此跳过；Windows 模拟测试不等于 Windows 实机验收。

没有改动 store.persist_file、仓储公开签名、模型或迁移。仍然不承诺文件系统与数据库原子提交，也不能阻止最后检查后的任意外部文件改写。受管理产物发布后应保持不可变。

## 3. 交付与安装

- [wheel](./release/anhui_data_foundation-0.2.0a2-py3-none-any.whl)
- [完整 ZIP](./release/C-index-artifacts-0.2.0a2-2026-10-14.zip)
- [逐文件 SHA256 清单](./release/C-index-artifacts-0.2.0a2-2026-10-14.contents.json)
- [摘要及验收回执](./release/C-index-artifacts-0.2.0a2-2026-10-14.receipt.json)
- [本机报告目录](./acceptance/C-0.2.0a2-windows-compat/README.md)
- [Windows 与正式知识链路接收步骤](./coordination/2026-10-14-v020/Windows与正式知识链路接收.md)

wheel 不包含外部迁移、tests、examples 和真实资料，必须配套完整 ZIP。现有 0006 不需升级结构；旧 0005 环境正常执行 `alembic upgrade head` 到 0006。历史包保留，禁止在原 0.2.0a1 文件名下替换字节。包仅在本工作区生成，没有上传 GitHub 或向团队发送消息。

## 4. 冻结本轮 D/E 契约

只接 E 单文件 UTF-8 JSON 关键词索引，保留 `schema_version=1`、`text-parser-0.5.0`、`keyword-tfidf-0.5.0` 和已有版本字段。manifest 样本及实际 chunk 编号/定位由 E 0.5.1 正式产物确认，不新建向量格式或声称 C 已验证 JSON 语义。

D 的顺序必须是：领取并退出短事务；事务外构建与 C 原生落盘；begin_index 和分批 write_chunks 分别提交；最终同一短事务 activate_index + complete_job。若 D 旧回执的“激活后落盘”确为实现，必须修正。租约由 D 唯一领取/续租/恢复，使用 C/MySQL jobs，E 不重复领取。

D 配置 E 的验证 `knowledge_snapshot_loader`，与 `knowledge_index_path` 互斥。加载失败应返回 503，不能继续用旧内存回答。C 只复核文件完整性；E 负责文件文档/版本/解析器/算法和数据库片段逐项绑定，包括“摘要正确但 JSON 文本或定位不同”的拒绝。D 的 HTTP 白名单不输出内部路径、文件字节、lease_token 或数据库连接串。

## 5. 真实公告准备

固定文档 `0c8e2020-2e0a-5842-bce9-d9193cf81095`：关于黄山风景区2025年春节黄金周开放时间、交通换乘和索道运营时间的公告。适用期 2025-01-28 至 2025-02-04，版本 `sha256:29aa803a0fa3e009feed5ce3aa07330e`。来源链接和完整带时区日期以原 manifest 为准，不重新抓取或修改现有快照。

新增 [prepare_historical_notice.py](./examples/prepare_historical_notice.py)，仅允许独占空 MySQL `_test` schema，迁移后登记这一份来源/原件/文档及 queued 知识任务，重复 enqueue 返回同一 job，新 store/engine 可再次读回。幂等键用 document_id + document_version + 新索引版本的 SHA256，符合现有字段长度。原件以 C 原生 persist_file 在写事务外落盘。

这只是 C 独立准备：不领取 D 的任务，不产生伪 E 索引，index_id 保持 null。实际测试 document/asset/job ID 见 [real-notice-preparation.json](./acceptance/C-0.2.0a2-windows-compat/real-notice-preparation.json)。这些 ID 所在测试库不是 D/E 正式业务库，D/E 须在自己的正式联调环境重新登记并回传 job/index ID。

留存形式为官方正文采录 TXT，不是 HTML 字节原件；保留来源和使用说明。历史问题可引用适用期，当前运营问题不得把此过期公告当现状证明。

## 6. 下一放行条件

本机 C 通过不关闭 Windows/联合状态。依次等待：E 用本 wheel 的 Windows 原生落盘、激活和重新读取回执；E 0.5.1 可安装包/提交/SHA256及实际 index.json/manifest；D C/MySQL 作业及事务顺序代码/日志；真实历史问答与重启、坏产物、回滚、租约恢复、内容绑定失败回执。全部正式链路通过后才恢复爬虫第二步。
