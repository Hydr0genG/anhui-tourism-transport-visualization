# 0.2.0a1 实际验收记录

2026-10-14；预发布0.2.0a1，迁移0006，模型/迁移文件与已提交0.1.9逐字节相同，无新表。测试仅使用临时SQLite或本地socket上的独立空MySQL8.4.11_test库；未访问D/E业务数据库。

| 检查 | 实际结果 | 文件 |
|---|---|---|
| 源码全量SQLite | 296通过 | sqlite-tests.txt |
| 54项新文件规则MySQL | 54通过，每用例独立schema | mysql-new-tests.txt |
| 文件与发布竞争 | 6通过 | mysql-artifacts.json / verify_index_artifacts.py |
| 生命周期/恢复/领取 | 8/6/5通过 | mysql_lifecycle.json、mysql_recovery.json、mysql_claim.json |
| 标注读取/覆盖/质量 | 6/6/5通过，含真实快照追溯与幂等 | mysql_reads.json、mysql_coverage.json、mysql_quality.json |
| 旧存储样例 | SQLite/MySQL各21通过 | sqlite-legacy.json、mysql_legacy.json |
| 隔离安装wheel | 296测试、MySQL竞争6、MySQL/SQLite存储各21通过 | installed-tests.txt、wheel-mysql-artifacts.json等 |
| 解压ZIP再安装 | 296测试、SQLite存储21和样例3通过 | zip-installed-tests.txt、zip-sqlite-legacy.json等 |
| 单文件调用样例 | 源码与安装包各3通过 | sqlite-artifact-smoke.json、installed-artifact-smoke.json |
| wheel/模型 | 源码字节匹配，0006模型/迁移无改动 | wheel-and-schema.json |
| ZIP/真实资料 | CRC、逐文件SHA256、解压资料校验，见实际回执 | ../../release/C-index-artifacts-0.2.0a1-2026-10-14.receipt.json |

命令：从仓库根目录执行`python -m pytest data_foundation/tests`；MySQL设置仅本次独占空_test库TEST_DATABASE_URL，运行examples/verify_index_artifacts.py --data-root 临时目录 --report 输出JSON。其他回归脚本名与对应JSON一致；54项新测试使用既有测试conftest的COVERAGE_MYSQL_ADMIN_URL空anchor逐例新建schema机制。安装验证将wheel装入独立target，只复制外部tests/examples/alembic/assets/real_data，未复制包源码；installed-origin.txt核对模块来源。

源代码及文件测试使用合成索引字节；这不是E向量内容或实际RAG效果证明。新读取入口复验完整文件，但不能阻止最后检查后的外部改写。D/E正式接收、版本/片段语义绑定、G18及真实Worker/HTTP/问答仍待完成，不按本目录关闭这些状态。
