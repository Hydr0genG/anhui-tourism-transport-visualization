# 第一阶段来源审计资料

入口：[执行报告](../../24-三步实施与第一步来源审计结果.md)、[44对象矩阵](./entity_matrix.md)、[来源配置](../../assets/expansion_source_audit.json)。

- `audit_result.json`：对象结论、机场行存在性与原件核验。
- `acquisition.json`：每个来源最近一次样本获取结果。
- `acquisition_history.json`：目录转正文过程中的旧请求结果，保留曾经成功和失败的事实。
- `originals/`：下载原件，按来源ID及内容摘要命名。
- `extracted/`：核验用文本，不能替代原件。

采集配置属于候选台账；样本可读不表示目标指标、口径、许可或连续性已经通过。所有来源关闭自动入库。本资料不含API密钥、账户信息或业务数据库配置。
