# 致 D / E：C 0.2.0a2 正式修复包交接报告

日期：2026-10-14。来自 C。本报告回应《C020接收回执与Windows问题》《D升级E051对接清单》《致E-0.5.0对接回执-2026-10-14》，用于双方接收执行和回传证据；不是 D/E 已完成联合验收的声明。

## 一、结论与接收版本

C 已完成 Windows 接收问题的代码修复、本机回归、独立 MySQL 发布竞争验证，以及 wheel/完整 ZIP 交付。请接收 **C 0.2.0a2**，停止以原 a1 或本地候选补丁作为本轮放行包。迁移仍为 **0006**，业务表仍为 **31 张**，公开方法签名不变，不增加迁移。

- C 修复与交付提交：[`06d192f068817aa9ffd24ad988e92d9737592aff`](https://github.com/Hydr0genG/anhui-tourism-transport-visualization/commit/06d192f068817aa9ffd24ad988e92d9737592aff)。提交前另复跑 Python 3.13 文件规则，69 项通过。
- 原 a1 基线：`ed0400e323b7d48089a6f9b8003807ceca556e01`，不是 a2 提交。
- 当前放行状态：C 独立存储 **passed**；Windows 实机正式包接收 **pending**；D/E 正式知识链路 **pending**。
- 本轮不做 G18、视频人数存储和 DM-Count 权重；全省爬虫第二步继续暂停，正式知识闭环通过后恢复。

构包时生成的 ZIP 内文档和原始验收 JSON 保留当时“未提交/未上传”和 `release_commit=null` 的记录，以维持已发布摘要不变。现在的 C 提交信息以本报告上述提交为准；没有在旧文件名下改写包内容。报告撰写与 Git 上传不等于已向 D/E 发送消息或取得其接收回执。

## 二、交付文件与 SHA256

以下是本轮固定字节的交付件，请先核对摘要，再安装：

| 文件 | SHA256 |
|---|---|
| [anhui_data_foundation-0.2.0a2-py3-none-any.whl](./data_foundation/release/anhui_data_foundation-0.2.0a2-py3-none-any.whl) | `4cf48f5c8c4303e2cb73a4169d62dd6111dba03df266256eced48ec565590fbe` |
| [C-index-artifacts-0.2.0a2-2026-10-14.zip](./data_foundation/release/C-index-artifacts-0.2.0a2-2026-10-14.zip) | `7fcbbc0031a13520b5d02aa9ebc8f9066d7b5a237609b0b2c6b0a87a96e54ad5` |
| [逐文件摘要清单 contents.json](./data_foundation/release/C-index-artifacts-0.2.0a2-2026-10-14.contents.json) | `a554fd38650cf8942958cc1224b2c278c5746306e9ddc4759e07e34d8ff93b0d` |

ZIP 共 744 个文件、14,421,091 字节，配套 [验收与交付回执](./data_foundation/release/C-index-artifacts-0.2.0a2-2026-10-14.receipt.json)。wheel 不含外部迁移、样例、测试和真实资料，必须结合完整 ZIP 使用。原 406 份已跟踪真实资料字节未改变，暂停的爬虫审计文件没有混入本次 Git 提交。

## 三、已修复内容与实际测试

1. 首次文件哈希及发布锁后复核统一使用打开的文件描述符和 `os.fstat()`，复核时重新打开并关闭描述符。不再混用 `Path.stat()` 与 `fstat()` 的 ctime。device、inode、size、mtime_ns、ctime_ns 五项检查全部保留。
2. Windows 目录输入返回 `InvalidArgument`；普通文件权限或读取失败返回 `Conflict`；不存在返回 `NotFound`。保留 DATA_ROOT、规范相对路径、符号链接拒绝和普通文件限制。
3. FIFO 按平台能力执行；Windows 没有 `os.mkfifo` 时记录明确跳过原因，不能因为 FIFO 跳过而略去目录拒绝或原生重命名落盘测试。没有替换 C 原生 persist_file。

| 实际检查 | 结果 |
|---|---|
| 源码全量 SQLite、安装 wheel、解压 ZIP 后安装 | 各 311 项通过 |
| 独立 MySQL 文件规则 / Python 3.13 文件规则 | 各 69 项通过 |
| 独立 MySQL 发布竞争 / 安装 wheel 后发布竞争 | 各 6 项通过 |
| MySQL 生命周期、恢复、领取筛选 | 8 / 6 / 5 项通过 |
| MySQL 分析读取、覆盖、质量 | 6 / 6 / 5 项通过 |
| MySQL 及安装包的旧存储入口 | 各对应 21 项通过 |
| 文件调用样例 | 各对应 3 项通过 |

测试环境为 macOS、Python 3.12.14（全量/MySQL/安装）及 3.13.16（文件规则）、独立 MySQL 8.4.11。只使用专用空 `_test` schema，未访问 D/E 正式库；测试实例已关闭，数据未删除。完整证据见 [本机验收目录](./data_foundation/acceptance/C-0.2.0a2-windows-compat/README.md)。这些通过项不代替 Windows 实机、E JSON 语义验证或正式 HTTP 问答。

## 四、请 E 优先完成

先按 [Windows 与正式知识链路接收执行单](./data_foundation/coordination/2026-10-14-v020/Windows与正式知识链路接收.md) 在 Windows 安装上述正式 wheel，记录实际包来源和摘要；不得继续使用候选补丁或替换 persist_file。验证原生落盘、索引激活、新 store 重新读取及 E 验证加载器加载，回传明确结果和跳过原因。

随后提供 E **0.5.1 可安装发布包**、提交号、包 SHA256，以及实际 `index.json` / manifest 文件本体。此前文档仅提及的样本和日志路径，C 尚未收到对应文件，请补齐。当前不把 E 旧 0.5.0 或候选 0.5.1 当正式已接收版本。

本轮只用现有单文件 UTF-8 JSON 关键词索引，保留既有 `schema_version=1`、`text-parser-0.5.0`、`keyword-tfidf-0.5.0` 和版本字段，不扩展向量格式。E 验证加载器须核对 C 文档身份、document_version、index_version、算法/解析器，以及片段编号、文本、locator 与数据库的对应关系；完整 SHA256 正确不等于这些语义一致。

请提供加载器的实际入口/签名及异常约定，并证明“摘要正确但 JSON 片段与数据库不一致”会拒绝加载，活动产物坏/缺失时不使用旧内存回答。

## 五、请 D 按正式版本接入

安装相同 C wheel，并按 E 0.5.1 正式包接入，不继续按旧 0.5.0 的加载契约运行。知识任务必须实际存在于 **C/MySQL jobs**，由 D 唯一领取、续租、恢复和完成；E handler 不再领取另一份队列。沿用 C 登记的 source、asset、document_id 和 document_version，不以 SQLite 桥状态替代正式任务。

必须提供实际代码或脱敏日志，证明顺序为：

`领取并退出短事务 → 事务外构建及原生 persist_file 落盘 → begin_index / 分批 write_chunks 提交 → 同一短事务 activate_index + complete_job`

文件必须先于激活存在。请核对旧 D 回执中的“激活/完成后落盘”是否只是记录顺序错误；若实现如此，必须修正。索引发布或任务完成任何一步异常均须传播出写事务上下文并整体回滚，不捕获后继续提交。每批片段不超过 C 既有 1000 上限。

问答配置 E 的 `knowledge_snapshot_loader`，与 `knowledge_index_path` 互斥；加载失败映射 503，不能用旧内存回答。HTTP 仍按白名单输出，过滤内部路径、产物字节、租约令牌及数据库连接信息。实际 C 调用片段和接收命令见执行单，不在本报告虚构 E API 或 HTTP 路由。

## 六、联合验收固定资料及场景

固定文档为《关于黄山风景区2025年春节黄金周开放时间、交通换乘和索道运营时间的公告》：

- document_id：`0c8e2020-2e0a-5842-bce9-d9193cf81095`。
- document_version：`sha256:29aa803a0fa3e009feed5ce3aa07330e`。
- source_id：`e4628668-ec52-5d6e-be17-7682c3cdbc2e`。
- 公告发布时间：2025-01-24 10:21（+08:00）；适用期：2025-01-28 至 2025-02-04。
- [官方来源](https://www.huangshan.gov.cn/zxzx/tzgg/8409577.html)；已留存资料为官方正文采录 TXT，不是 HTML 字节原件。使用说明、日期和资料路径以 [真实 manifest](./data_foundation/real_data/2026-10-12-v2/manifest.json) 为准。

C 已在独立测试 MySQL 用安装 wheel 验证该公告原件落盘、文档登记、queued 知识作业幂等及新 engine 读回。测试 asset_id 为 `41a6e3c3-6ac2-4946-9d70-233426fc6a04`，job_id 为 `aaaec2fa-b13e-415a-aac0-a1bb4b605d31`，**index_id 为 null**。这些只证明 C 独立准备，不是 D/E 的正式任务；请在双方联调环境用真实 C 文档版本新建 index_version/job，并回传实际正式 ID。

联合验收须逐项回传，不能只报测试总数：

| 场景 | 必须达到的结果 |
|---|---|
| 历史问题 / 当前运营问题 | 前者引用文档、片段、来源和适用期；后者明确历史资料不足以证明现状 |
| API 和 Worker 重启 | 同一已提交文档和活动索引仍可加载引用 |
| 新产物摘要错误 | 发布拒绝，旧活动版本不切换 |
| 活动产物损坏 / 缺失 | 两类都返回脱敏 503，不继续用旧内存回答 |
| 激活异常 / 完成异常 | 分别验证活动版本、索引与作业整体回滚 |
| 旧租约 / 新租约 | 旧租约拒绝；新租约相同片段重传不重复，随后成功发布 |
| 摘要正确但 JSON 文本或 locator 与数据库不同 | E 拒绝加载，问答不能继续成功返回 |

故障注入只在专用联调环境执行，不损坏生产产物。脱敏 HTTP 请求/响应、重启日志、代码顺序证据以及故障前后状态均应留存。

## 七、回传格式与放行顺序

请 D/E 各自回传：实装 C/D/E 版本与提交、包摘要、Windows/Python/MySQL 版本、迁移号/业务表数、正式 document/asset/job/index ID、document_version、新索引版本、产物摘要/大小，以及每项成功/失败/跳过原因和对应证据文件。不要回传密码、Cookie、完整连接串或租约令牌。

回执分别列出 `C_independent_storage`、`Windows_official_C_receiving`、`formal_DE_knowledge_chain`。C 目前只关闭第一项；E 先放行 Windows 正式修复包，再与 D 对齐正式 0.5.1 和 C/MySQL 调用链，最后关闭真实公告问答。任何接收环节失败先修该环节，不将候选副本或 SQLite 桥通过算作正式联调完成。
