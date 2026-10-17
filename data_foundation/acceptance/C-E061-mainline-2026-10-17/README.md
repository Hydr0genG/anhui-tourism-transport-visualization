# C/E061 主线续验

结论见项目根目录《致D和E-E061接收与MySQL主线续验-2026-10-17.md》。C 官方 a2 未改；E0.6.1 正式件及原始回执存于 upstream。upstream 文档是 E 的接收结论，独立测试结果在其余报告，不能混同。

## 范围和证据

- ingestion-original：D 原版 + C a2/E061，16/17，保留30号失败。
- ingestion-candidate：仅一次性副本应用既有两处30号补丁，17/17，不算D已应用。
- knowledge：首轮29/29；knowledge-final：增加新进程严格恢复，30/30，均为未修改的D。
- C-E061-mysql-tests.xml：53项；queue-mysql-tests.xml：75项，合计128项。
- worker-reference-sqlite.xml：8项。E061-installed-tests.xml：117通过/7跳过，原因见test-counts.json。
- D-source-verification.json：从D固定Git提交重新读取原文件，核对知识测试原快照和一次性候选的源码摘要。
- runtime-versions.json、commands.json、mysql-shutdown.json：实际运行依赖、复跑命令与临时实例关闭确认。

raw 是仅供本地复查的运行目录（真实公开原件、隔离账号数据库关联文件），不加入Git或补充ZIP。报告不含口令、会话/CSRF或租约令牌。知识故障注入仅操作新测试库中的索引，发布失败日志属于主动故障验证。

## 接收和复跑

将新补充ZIP按原目录合并到已收到的C完整交接目录；不要改旧wheel/ZIP。D可安装随附的E0.6.1 wheel（在已具有E依赖的环境执行）：

```bash
python -m pip install --no-deps --upgrade data_foundation/acceptance/C-E061-mainline-2026-10-17/upstream/shumei_intelligent_analysis-0.6.1-py3-none-any.whl
```

知识主线需D当前源码、D声明的HTTP依赖、C官方wheel、E官方wheel、C real_data/2026-10-12-v2 数据集和C alembic目录。用环境变量 C_MAINLINE_DATABASE_URL 指向**新建且为空**的 cde_readiness_*_test MySQL schema；不要提供业务库。每轮使用新schema和不存在的输出目录。程序只迁移所给空库，不删表或清空旧库。

```bash
python -I data_foundation/examples/verify_cde_knowledge_mysql.py \
  --d-root /path/to/D-checkout --d-commit FULL_D_COMMIT \
  --e-wheel data_foundation/acceptance/C-E061-mainline-2026-10-17/upstream/shumei_intelligent_analysis-0.6.1-py3-none-any.whl \
  --output /path/to/new-knowledge-evidence
```

29/30/19 使用 `verify_cde_mainline_http.py`，同样显式传入 `--e-wheel`；默认仍兼容旧0.6.0验证。D正式提交修复后用新SHA复跑，不传候选标记。C本轮另外传 `--require-socket-only-instance` 确保无数据库网络监听；接收方专用测试MySQL可不传。

上述HTTP是ASGI TestClient，没有API网络监听。预测和人数调用使用合成素材，只验证接口、质量边界和MySQL事务。真实授权视频、人工复核、真实MAE、D机器HTTP及浏览器仍待验收。
