# C执行的主线HTTP与Worker参考验证

2026-10-17。C0.2.1a2/E0.6.0；D基线24eec2d33d6186bacdaafe08e33808c28dcdeb5e。C正式wheel不变，未修改D/E原仓库、业务库或独立爬虫。

| 证据 | 结果 | 说明 |
|---|---|---|
| run-1/report.json | 16/17，失败 | D原快照，30号datetime转换TypeError |
| run-2-candidate/report.json | 16/17，失败 | 一次性副本仅修时间后，继续发现可选analysis_id被直接下标 |
| run-3-final-candidate/report.json | 17/17，通过 | 一次性副本应用完整D-30-job-readback.patch；不是D已接收/部署 |
| proposed-D-patch-tests.xml | 7通过 | 直接对原文件应用拟议补丁后测试datetime、ISO、UTC naive、None及跨日 |
| worker-reference/tests.xml | 5通过 | SQLite，E/C调用、原子完成、不足、溯源及等价时区身份 |
| worker-reference/mysql-tests.xml | 5通过 | 同组参考调用的真实MySQL验证 |
| D-source-fingerprints.json | 59文件核对 | 原快照与Git提交一致，最终候选仅jobs.py不同 |

HTTP使用实际D路由和C真实MySQL认证/会话，测试客户端以ASGI方式调用，无监听API端口；未mock认证、CSRF、C仓储或E数值回答。已有83条真实观测、CSV/PDF复用C原资料，CSV重复导入正确skipped=1，无新增或修订；新登录账号明确为合成隔离测试账号。

视觉Worker参考使用合成输入/counter和E原生处理函数，未运行真实模型权重或解码获准录像，不能据此关闭真实视频、精度、连续录像预测。预测用例实际调用E函数并得到data_insufficient，不伪造ready。

报告移除密码、会话cookie、CSRF、lease token和连接串。run-*/raw为本机受管理副本，不入Git/补充包；MySQL关闭后保留数据文件。安装版本和纯C表分类另见runtime-versions.json及各report。

封存主线补充ZIP后，另解压到工程外与旧C完整包合并：25个补充文件摘要逐项一致，参考调用与补丁回归12项再次通过。portable-reference.json与portable-reference-tests.xml为封存后的单独补充证据，未倒改ZIP；这次可携带性检查没有重跑或冒充D接收方HTTP。
