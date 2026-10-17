# 29号真实CSV输入补充

本包复用C2026-10-12-v2既有资料，未新增联网采集。CSV是C整理的一行规范数据，不冒充官网CSV；PDF为官方证据原件。完整摘要、URL、发布时间及定位在input-manifest.json。

## 执行顺序（D的独立联调环境）

1. 使用已安装C0.2.1a2环境，将补充包按原目录合并解压到午间交接包根目录（同一层的data_foundation/），只增加补充文件，不替换原wheel。
2. 默认无数据库写入，仅校验本地文件：

```powershell
python data_foundation/examples/configure_registered_csv_rehearsal.py data_foundation/acceptance/C-DE-receiving-followup-2026-10-17/registered-csv-input
```

3. 明确选择已迁移到0007、已有C目录的专用测试库及独立文件目录。通过当前进程设置C_REGISTERED_CSV_TEST_DATABASE_URL（仅tourism_flow_test或cde_readiness_*_test）和C_REGISTERED_CSV_TEST_DATA_ROOT。不要在文档/回执中写口令，不修改业务库tourism_flow。然后使用同一命令追加--apply。
4. 配置脚本输出csv_asset_id/provenance_asset_id/source_id，不输出口令和内部原件路径；它不执行作业。用登录/CSRF后的29接口提交：

```json
{"source_ids":["fe47bf03-2cd2-5c89-a944-4cbcf5d38cd7"],"dry_run":false,"reason":"既有真实资料CSV联调"}
```

5. C消费进程的DATABASE_URL/DATA_ROOT必须指向步骤3相同的测试库/文件目录，然后运行data-foundation run-ingestion-once --job-id <29返回的UUID>。D的Worker不得抢领ingestion。
6. 用30读回succeeded、run_ids和counts，再查询合肥机场2025-01=1,111,644及官方PDF溯源。已有相同观测时应skipped=1，不产生修订；空观测测试库首次可imported=1。重复同一29请求应返回同一job，再消费已完成job返回null。

可先dry_run=true跑预览，job.imported_count必须为0；这与result_refs.counts中的预览值不同。回传脱敏29请求/响应、消费结果、30响应、观测与原件SHA。空source_ids导致failed只证明错误路径，不能代替本次成功验收。
