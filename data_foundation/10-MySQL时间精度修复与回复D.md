# MySQL 时间精度修复与回复 D

日期：2026-10-11；新包版本 0.1.1；迁移 head 0005。

## 1. 本次修复

D 回执记录：旧版 SQLite 18 项通过，原样 MySQL 在领取作业时失败，截断样例微秒后 MySQL 18 项通过。该成功报告是绕过问题后的结果，不代表原包通过。

模型的全部 77 个时间列统一使用 MySQL DATETIME(6)，包含任务 available_at、租约、会话及知识/视频/预测时间。SQLite 时间类型保持兼容。新增独立 0005 迁移升级 0004 的现有列，未改写已发行 0001—0004 迁移。API 名称与参数保持本轮既有形式。

MySQL 默认不声明精度时使用 0 位小数秒，较高精度输入默认会舍入；依据：[MySQL 8.4 时间小数秒文档](https://dev.mysql.com/doc/refman/8.4/en/fractional-seconds.html)。数据库继续保存 UTC 无时区时间，精度调整不改变时区约定。

新增样例检查：700000 微秒原值读回和同一时刻领取任务；租约及会话在到期前一微秒有效，到期时失效。样例不截断 utc_now()。

## 2. C 验证证据与边界

- 自动化测试 9 项通过：原有 6 项，加模型/迁移的 MySQL SQL 编译检查、已有 SQLite 0004 数据升级保留检查、微秒边界行为检查。
- 新版全量合成存储样例预期 21 项；D/E 分别运行各 13 项。
- MySQL 0004→0005 离线 SQL 编译覆盖 77 列；SQL 编译通过不是服务器执行通过。
- 复核时启动了独立临时 MySQL 8.4.11 实例（macOS、仅本地 socket、无网络监听）：从交接 ZIP 解压目录运行，新空库 all 21 项、d/e 各 13 项通过。
- 真实 MySQL 已有 0004 测试库升级到 0005：77 个时间列精度均为 6，原有测试记录保留，新写入 700000 微秒准确读回；升级前的 DATETIME(0) 舍入现象也已复现。
- 临时实例已关闭。结果见 acceptance/C-0.1.1-mysql/；D 的 Windows 接收环境仍需复验。

原始 D 材料归档在 acceptance/D-2026-10-11/。不据此声称真实资料、算法、HTTP 路由或 E 环境已验收。

## 3. D 复验：新空测试库

在新 ZIP 的解压根目录，用自己的 Python 环境：

```bash
python -m pip install --upgrade ./data_foundation/release/anhui_data_foundation-0.1.1-py3-none-any.whl
python -c "import data_foundation; print(data_foundation.__version__)"
python data_foundation/examples/de_handoff_smoke.py
python data_foundation/examples/de_handoff_smoke.py --target mysql-test --data-root /absolute/path/to/empty-test-assets --report .tmp/c-de-handoff-mysql-0.1.1.json
```

TEST_DATABASE_URL 在本地设置，必须指向名称以 _test 结尾的新空库，资产目录为空。Windows 将资产路径替换成本机绝对路径。不要修改样例中的 utc_now，也不要手工预迁移该空库。预期 package_version=0.1.1、migration_head=0005、status=passed、check_count=21。三个 precision-* 检查必须通过。

## 4. D 复验：已有 0004 测试库升级

这条路径用于验证已有库升级，使用独立测试库，不是上节空库模式。

1. 备份该测试库，暂停该库相关 Worker 和写入；确认安装包为 0.1.1。
2. 本地设置 DATABASE_URL 指向该 0004 测试库，取消 DATA_FOUNDATION_MIGRATION_STAGE。
3. 在解压根目录执行：

```bash
python -m alembic -c data_foundation/alembic.ini current
python -m alembic -c data_foundation/alembic.ini upgrade head
python -m alembic -c data_foundation/alembic.ini current
```

前后应分别为 0004、0005，原有行保留。MySQL ALTER TABLE 可能锁表，并涉及隐式提交；若中途失败，保留日志和实际列状态，不把失败当作可自动事务回滚。

4. 在 MySQL 核查所有时间列的小数秒精度：

```sql
SELECT TABLE_NAME, COLUMN_NAME, DATETIME_PRECISION
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE = 'datetime'
ORDER BY TABLE_NAME, ORDINAL_POSITION;
```

应有 77 列，精度均为 6；另用 D 原回执的最小复现确认同一 now 入队和领取成功。

已有 DATETIME(0) 数据在历史写入时丢失的小数秒无法通过升级恢复。正常交付不执行降级；降级回 DATETIME(0) 会再次损失精度。

## 5. 可直接发给 D 的回复稿

```text
D：已按验收回执修复时间精度，交接包升级为 0.1.1，迁移 head 0005。
模型和迁移统一覆盖全部 77 个时间列，包含任务、租约和会话。
样例新增三项微秒边界检查，all 现在是 21 项，d/e 各 13 项。
请使用原样新脚本复验 MySQL，不要截断微秒；同时验证已有 0004 测试库升级。
C 已通过 9 项自动化测试和新版本地样例，C 另用独立 MySQL 8.4.11 验证了新空库 21 项及已有 0004 库升级。
你的 Windows 接收环境结果仍需回传。
请反馈版本、head、21 项报告，以及已有库升级后的列精度和数据保留情况。
H01/H02/H05 的 D 侧确认已记录；H04 的接口补充进入后续优先级，尚未纳入这次精度修复。
```

此回复稿和 ZIP 已准备，未代用户发送。

## 6. 第二轮复核结论

ZIP 摘要逐项核对通过；源码与 wheel 内 Python 代码一致。发现并修正了交接说明对未打包项目总表的链接，新增真实 MySQL 证据。测试只使用合成数据和独立测试实例，没有访问现有业务库。已有数据的历史舍入不会通过升级恢复；升级保留测试原值，新输入准确保留微秒。
