# v0.1.0 安装包

本目录保存首轮交接所用的离线 wheel。安装后仍需单独提供 MySQL、SQLAlchemy、Alembic 和 PyMySQL 运行依赖；团队统一环境已包含这些依赖。

wheel 不包含仓库级 `alembic/` 迁移、`alembic.ini` 和 examples。接收方需保留完整配套文件后按[交接说明](../08-C到DE交接说明与实调样例.md)实调。本轮新增文档和外部样例未修改已发行 Python 包实现；完整设计中的缺失能力见[实装核对](../09-接口实装核对与缺口清单.md)。
