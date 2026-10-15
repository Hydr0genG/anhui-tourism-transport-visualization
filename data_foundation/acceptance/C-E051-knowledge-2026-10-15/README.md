# C 接收 E 0.5.1：证据索引与复验边界

日期：2026-10-15。C/E 核心 wheel 固定不变，C 不修改 D/E 仓库。本目录不是正式 D/MySQL/HTTP 联合通过回执。

## 本地独立结果

- [mysql-notice-run-2.json](./mysql-notice-run-2.json)：最终工具第二次在新空 MySQL 库中运行，17 项全部通过。实装 C 0.2.0a2 / E 0.5.1，Python 3.13.16 / MySQL 8.4.11，迁移 0006 / 31 表。包含真实 source/asset/document/job/index ID、17 项矩阵、短事务轨迹、新 E 进程重读及服务响应。
- [mysql-notice.json](./mysql-notice.json)：第一轮，另一个空库，17 项通过；保留初始记录，不复用其 ID 作为正式联合任务。
- [c-source-tests-complete.xml](./c-source-tests-complete.xml)：C 当前源码完整回归，356 tests / 0 failures / 0 errors / 0 skipped，其中 19 项为新接收工具保护测试。
- [e051-knowledge-tests.xml](./e051-knowledge-tests.xml)：从固定 E 0.5.1 ZIP 复制知识/RAG、加载器、持久化、Worker 和服务契约测试，不复制 E 包源码，实装 wheel 执行；31/31 通过。不是重跑其全部视频/预测测试。
- [installed-dependencies.json](./installed-dependencies.json)：独立实装环境依赖版本。
- [mysql-shutdown.json](./mysql-shutdown.json)：仅本轮关闭网络监听的临时实例关闭请求；测试库/原件/索引保留，不删除数据。最终关闭确认见 verification-summary.json。
- [D-readonly-code-review.json](./D-readonly-code-review.json)：固定 D `0cefaf3` 的只读源码片段与文件 SHA，定位桥库、非原子完成、重复完成、租约、身份和加载路径缺口。没有执行 D Worker/HTTP，也没有改 D 源码。

17 项真实 MySQL 检查涵盖原生落盘、分批片段、原子发布、历史/当前问答、重新启动 E 读取进程、活动文件缺失/损坏拒绝、坏摘要旧版本保持、activate/complete 分别写入后异常回滚、续租/过期恢复、旧租约拒绝、新尝试重复片段跳过并发布，以及正确 SHA 的文本/locator/两类版本不匹配拒绝。最终另加载 E 随包提供的实际 index.json，不仅验证自造样本。

这里的问答是 E 服务直接返回，不是 HTTP；DependencyUnavailable 不等于已观测到 D 的 503。新 E 进程重读不等于 API/Worker 进程重启。编排由 C 侧隔离工具执行，不能将事务轨迹冒用为 D 已修改的证据。

## E 的原始接收回执

[upstream-provenance.json](./upstream-provenance.json) 记录原始来源、固定发布提交及逐文件摘要。`upstream/E-0.5.1/` 保留 E 固定 ZIP 中 Windows/知识/bridge/wheel 安装报告及实际 JSON 样本，另从固定发布提交取 E 包摘要回执。按原始字节保存，Git `-text` 防止 Windows 换行改变摘要。

E 自述结果：Windows 11 build26200 / Python3.13.13，C 官方 a2 310 passed / 1 FIFO skip；原生 persist_file，无候选补丁；16 份资料 / 655 片段及重读。E 使用独立 SQLite，未连接 MySQL。该回执是 **E 实机接收证据**，与 C 本机 MySQL、C Windows CI、正式 D/E 联合链路分别记录。本机没有重跑 Windows。

## 失败尝试不隐藏

1. 从项目根目录误用旧 `.venv` 运行源码测试时，加载了旧安装 C，引发 6 个收集错误。没有据此判断当前源码失败，也没有将其算通过。后续明确使用新环境和 C 源码根目录。
2. [c-source-tests.xml](./c-source-tests.xml) 保留旧环境的完整失败记录：355 项中 1 个缺 lxml 失败、1 个 PDF 能力跳过。独立环境补齐 C 的 `[data]` 可选依赖后，最终 356 项全部通过（其间补充 1 个空字节路径保护用例）。
3. 临时 MySQL 最初在沙箱映射权限处初始化/启动崩溃，Unix socket 连接也被沙箱拒绝；随后获得工具授权，才在本轮专用目录成功初始化、运行。没有以 SQLite 回退代替 MySQL 结果。

上面是调用/环境失败，不是把正式包测试改为跳过；最终结果以完整独立实装环境和真实 MySQL JSON 为准。

原始 JUnit XML 的失败 traceback 含 pytest 输出的行尾空格，按原始字节保留，不为代码风格检查改写失败记录。仅该失败 XML 从 diff 的行尾空格检查中排除；工具、测试、文档及其余记录仍检查。三个 XML 的原始 SHA256 记录在 verification-summary.json。

## 复验命令

从项目根目录运行；先取得上述固定 E 发布提交的 `release/`，不要用当前 0.6.0 wheel。以下 `E_RELEASE_DIR` 只是已下载文件目录，不是新仓库地址。

```bash
python3.13 -m venv .tmp/ce051-receive
.tmp/ce051-receive/bin/python -m pip install \
  data_foundation/release/anhui_data_foundation-0.2.0a2-py3-none-any.whl \
  "$E_RELEASE_DIR/shumei_intelligent_analysis-0.5.1-py3-none-any.whl"

# 由管理员提供本次独占空 mysql+pymysql _test 库；连接地址只放环境变量。
# TEST_DATABASE_URL 必须显式设置，工具不创建 schema、不删除已有表。
.tmp/ce051-receive/bin/python -I data_foundation/examples/verify_ce051_knowledge.py \
  --run-root .tmp/ce051-new-run \
  --report .tmp/ce051-new-receipt.json \
  --c-wheel data_foundation/release/anhui_data_foundation-0.2.0a2-py3-none-any.whl \
  --c-zip data_foundation/release/C-index-artifacts-0.2.0a2-2026-10-14.zip \
  --e-wheel "$E_RELEASE_DIR/shumei_intelligent_analysis-0.5.1-py3-none-any.whl" \
  --e-zip "$E_RELEASE_DIR/E-visible-people-0.5.1-2026-10-15.zip"
```

run-root 和 report 都必须是新路径，已有库表/目录拒绝运行。工具核对四个固定摘要及实装模块来源，从 C 正式 ZIP 只取迁移与单公告，不把包源码加入环境。故障注入只涉及新建隔离产物；报告不含数据库 URL、密码、lease_token 或内部产物路径。失败的公开输出只有错误类型，不输出第三方连接异常。

下一步交接见[主报告](../../../C接收E051与D正式MySQL切换交接-2026-10-15.md)。D 需按原计划在自己的仓库完成 C/MySQL 接入，并给正式 HTTP/Worker 回执，才能关闭第二、三步和恢复爬虫。
