# C 0.2.0a2 本机实际验收记录

2026-10-14。C0.2.0a2 / 0006 / 31张业务表。macOS；全量及安装/MySQL脚本使用Python3.12.14，补充文件规则使用Python3.13.16；独立MySQL8.4.11仅通过临时Unix socket监听。本轮没有访问生产库或D/E业务库。

| 检查 | 结果 | 实际报告 |
|---|---|---|
| 源码全量 SQLite | 311通过，无跳过 | sqlite-tests.txt |
| Python3.13文件规则 | 69通过 | python313-artifact-tests.txt |
| 独立 MySQL 文件规则 | 69通过，每个store用例独立schema | mysql-artifact-tests.txt |
| MySQL发布竞争 | 6通过 | mysql-artifacts.json |
| 生命周期/恢复/领取 | 8/6/5通过 | mysql-lifecycle/recovery/claim.json |
| 分析读取/覆盖/质量 | 6/6/5通过 | mysql-reads/coverage/quality.json |
| MySQL旧存储入口 | 21通过 | mysql-legacy.json |
| wheel隔离安装 | 311通过，来源为临时target而非包源码 | wheel-installed-tests.txt / wheel-origin.txt |
| 安装wheel的MySQL竞争/旧存储 | 6/21通过 | wheel-mysql-artifacts/legacy.json |
| 安装wheel的SQLite旧存储/文件样例 | 21/3通过 | wheel-sqlite-legacy.json / wheel-artifact-smoke.txt |
| 真实公告独立准备 | 4项通过：原生落盘、文档版本、任务幂等、新engine读取 | real-notice-preparation.json |
| 解压ZIP后隔离安装 | 最终回执核验311回归、21旧存储、3文件样例 | zip-installed-tests.txt / zip-sqlite-legacy.json / zip-artifact-smoke.txt |
| 源码/迁移/真实快照/ZIP校验 | 见最终生成的完整记录 | release-verification.json / ../../release/C-index-artifacts-0.2.0a2-2026-10-14.receipt.json |

`pytest -ra` 在支持FIFO的平台没有跳过；Windows不提供os.mkfifo时显示并记录明确skip原因。当前Windows、E加载器、D正式Worker/HTTP并未运行，不能从上述通过项推断其通过。

## 重跑命令

源码测试从项目根目录执行 `PYTHONPATH=data_foundation python -m pytest data_foundation/tests -ra`（安装了dev/data依赖）。这避免本机旧版site-packages遮蔽源码。隔离安装验证仅复制外部tests/examples/alembic/assets/real_data，不复制Python包源码，并记录__file__实际来源。

MySQL测试先由管理员建立新的独占空 `_test` schema，再设置TEST_DATABASE_URL运行 `examples/verify_index_artifacts.py --data-root 独立目录 --report 输出JSON`。旧回归脚本及参数见a1交接和对应报告；69项文件规则使用既有conftest的COVERAGE_MYSQL_ADMIN_URL空anchor机制，不删除已有库。所有执行过程中创建的新schema都在此次临时MySQL实例内。

`examples/prepare_historical_notice.py --manifest real_data/2026-10-12-v2/manifest.json --data-root 独立目录 --report 输出JSON` 也只接受独占空_test库，创建queued任务而不伪造E索引。其真实document/asset/job ID仅属于C独立测试；index_id为null，正式D/E须自己回传正式ID。

## 发布与剩余状态

源码及wheel/ZIP测试已执行的结果由最终回执逐项核对；release-verification.json记录五项指纹/签名/模型/迁移不变及原406个已跟踪真实资料文件字节不变。打包不包含此次暂停的爬虫审计临时报告，既有工作区内容原样保留。

正式Windows接收：pending。E0.5.1包和实际index.json/manifest：missing。D/E正式知识链路：pending。SHA正确但JSON与数据库文本/locator不一致的验证属于E，当前未执行。历史/当前问答、重启后的HTTP加载、503及对外脱敏仍须D/E真实证据。本机C可验证的坏产物、旧租约、恢复、回滚等通过不替代这些联合结果。

本机首次使用普通venv全量运行遇到旧安装包遮蔽及缺少PDF/HTML可选依赖，随后在有完整依赖的Python3.12.14环境重跑全量通过；Python3.13.16仅另跑不依赖这些可选库的69项文件规则，不将其标成全量311通过。
