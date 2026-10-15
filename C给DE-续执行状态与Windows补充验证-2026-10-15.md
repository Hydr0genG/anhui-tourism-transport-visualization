# 致 D / E：C 0.2.0a2 续执行状态与 Windows 补充验证

> 后续更新：已从既有 D Gitee / E GitHub 地址取得正式材料，E 0.5.1、实际样本和 Windows 回执不再缺失；本页下方保留前一轮核查时的历史状态。请以 [C 接收 E051 与 D 正式 MySQL 切换交接](./C接收E051与D正式MySQL切换交接-2026-10-15.md) 为最新状态，勿再要求重复提供仓库地址。

日期：2026-10-15。承接 [正式修复包交接报告](./致D和E-C0.2.0a2正式修复包交接报告-2026-10-14.md)。本轮只推进已批准的 C 修复接收和知识联调准备；爬虫第二步、G18、视频人数存储、DM-Count 仍暂停或延期。

## 1. 新完成工作

C 增加 [固定发布包接收工具](./data_foundation/examples/verify_c020a2_receiving.py) 与 [Windows CI](./.github/workflows/c020a2-windows-receiving.yml)。工具先核对固定 wheel/ZIP/contents 摘要，再核对已安装 C 的版本和17个Python/JSON文件字节；仅从固定ZIP复制外部测试/资料，不复制包源码或使用候选补丁。接收子进程清除业务库环境变量，仅跑隔离SQLite，不连接MySQL。

工具26项单元测试通过，当前源码全量337项通过；原固定ZIP内仍为311项。本机另用正式wheel通过69项文件回归及3项发布/读取样例。原 a2 wheel、ZIP和清单SHA256均未改变，公开签名、迁移0006及31张业务表不变。

## 2. Windows 已发现并修正的交付问题

首轮 [CI 38594880762](https://github.com/Hydr0genG/anhui-tourism-transport-visualization/actions/runs/38594880762) 的wheel摘要及安装通过，但Git自动CRLF转换使外部 `contents.json` 摘要不同，测试尚未开始即被接收工具拒绝。该失败保留在 [记录](./data_foundation/acceptance/C-0.2.0a2-windows-supplement/windows-first-failure.json)，没有改写为通过。

本机以 `git -c core.autocrlf=true cat-file --filters` 重现：摘要从原 `a554fd...` 变为 `80043d...`。提交 [`eecb093`](https://github.com/Hydr0genG/anhui-tourism-transport-visualization/commit/eecb093a8004bab00f3ace1681d1832a92a9f2ae) 新增release清单/回执/ZIP/wheel的 `-text` 规则后，同样模拟仍得到原摘要。仅修正Git传输规则，未修改原交付内容，也未做规范化后哈希或放宽校验。

D/E 如果通过Git接收，请使用含该属性规则的新提交；若下载原ZIP或Git原始文件，仍以原固定SHA256核验。不在a2旧文件名下替换字节，也不需重发wheel。

## 3. Windows 补充回归结果

已通过 [CI 38595621767](https://github.com/Hydr0genG/anhui-tourism-transport-visualization/actions/runs/38595621767)：Windows Server 2022 / 10.0.20348 / Python3.13.16，原正式C wheel安装于site-packages，17个包文件与wheel逐字节一致。

| 实际检查 | 结果 |
|---|---|
| 原ZIP的311个回归用例 | 310通过，1个FIFO按Windows能力跳过，0失败 |
| FIFO跳过原因 | 平台不提供os.mkfifo；目录拒绝及原生重命名落盘测试已执行并通过 |
| 原生发布、重新读取、损坏拒绝样例 | 3项通过 |
| wheel、ZIP、清单与全部归档成员 | 固定摘要匹配 |

[原始receiving.json](./data_foundation/acceptance/C-0.2.0a2-windows-supplement/windows-run-2/receiving.json)、[JUnit](./data_foundation/acceptance/C-0.2.0a2-windows-supplement/windows-run-2/pytest.xml)、[日志](./data_foundation/acceptance/C-0.2.0a2-windows-supplement/windows-run-2/pytest.txt)和 [原始artifact ZIP](./data_foundation/acceptance/C-0.2.0a2-windows-supplement/windows-run-2-artifact.zip) 已保存。artifact ID为11908664734，ZIP SHA256为 `5c5c904400fde82fe38800cd07f6f7e28db5f42da933cc8346febb88ed6e9bd7`。

匹配E原失败解释器的Python3.13.13也已通过 [CI 38597078623](https://github.com/Hydr0genG/anhui-tourism-transport-visualization/actions/runs/38597078623)：同样310通过、1个FIFO跳过、0失败，3项原生发布/读取样例通过。[原始回执](./data_foundation/acceptance/C-0.2.0a2-windows-supplement/windows-run-3/receiving.json)、JUnit和日志已保存；[原始artifact ZIP](./data_foundation/acceptance/C-0.2.0a2-windows-supplement/windows-run-3-artifact.zip)的SHA256为 `bb5a34fb4eec12ebc36f994e42972d5dc51887e52e0a2e86e6570218e7cbe545`。Windows CI全部属于 **C独立存储补充**，不证明E真实JSON加载、D/MySQL作业、HTTP问答或E实机接收已通过。

## 4. 正式链路当前真实状态

| 放行项 | 当前状态 |
|---|---|
| C本机修复、独立MySQL、发布包和补充工具 | passed |
| C正式wheel的Windows3.13.16独立存储回归 | passed，含明确FIFO跳过 |
| C正式wheel的Windows3.13.13独立存储回归 | passed，匹配E原失败解释器，含明确FIFO跳过 |
| E使用同一包在自己Windows运行真实产物接收 | pending |
| E0.5.1正式发布包、提交、摘要及实际index/manifest | missing |
| D的C/MySQL jobs、续租恢复及事务顺序证明 | pending |
| 真公告HTTP引用、重启、503、内容绑定等联合场景 | pending |

本轮只读核查了 E [仓库](https://github.com/w3022843643/shumei_intelligent_analysis)：main为 `1e5ab6501f681d518479487ad31b93993c0e1fe4`；pyproject仍0.5.0；release目录仅有0.5.0交付件；`intelligent_analysis/knowledge/artifacts.py` 返回404；没有其他分支、标签或GitHub Release。没有修改E仓库，也没有把旧0.5.0或候选补丁包装成正式0.5.1。

D仓库地址/本机目录及新的Windows回执尚未提供。已请求这些缺失输入；不会在未知D代码上推测调用已适配或虚构HTTP成功响应。

## 5. 请 D/E 补齐后继续

E请提供0.5.1可安装包、提交与SHA256、实际 `index.json`/manifest，以及用上述正式C包执行原生persist_file→激活→新进程重新读取/E验证加载的Windows回执。请区分Windows/Python/MySQL版本、测试环境与正式联调环境。

D请提供仓库地址及实装提交，切换知识作业到C/MySQL，由D唯一领取、续租、恢复和完成；提交代码或日志证明“领取退出短事务→事务外构建/落盘→分批片段提交→同一短事务激活+完成”。问答接E验证snapshot_loader，与index_path互斥；失败503，无旧内存回退，HTTP继续过滤内部路径/产物字节/租约令牌。

之后使用已有真实公告 `0c8e2020-2e0a-5842-bce9-d9193cf81095`，版本 `sha256:29aa803a0fa3e009feed5ce3aa07330e`，适用2025-01-28至02-04，创建正式job与新索引版本，逐项回传原交接报告的历史/当前问答、API/Worker重启、坏摘要、损坏/缺失503、事务回滚、旧/新租约幂等与JSON/数据库不一致拒绝证据。已有C独立测试任务和null索引ID不作为正式联调ID。

只有正式链路通过后恢复全省爬虫第二步。下一步需要取得接收方正式包、样本与回执，才能开展D/E实际联调。
