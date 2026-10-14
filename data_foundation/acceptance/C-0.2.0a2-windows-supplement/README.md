# C 0.2.0a2 接收补充验证

2026-10-15 续执行。这里只记录自动化接收工具和 Windows CI 补充，不替代 E 的 Windows 接收或 D/E 正式知识链路。已发布 a2 wheel、ZIP 和摘要清单保持原字节，C 公开接口、模型和迁移不变。

## 本地实际结果

- 新增接收工具的 26 项单元测试通过，见 `receiving-tool-tests.txt`。
- macOS / Python 3.13.16 从正式 wheel 安装，17 个包内 Python/JSON 文件逐字节匹配；固定 ZIP 摘要和逐文件清单匹配。
- 仅从已校验 ZIP 复制外部测试和材料，不复制 C 包源码；69 项文件规则和 3 项原生发布/复验样例通过，见 `local-run-3/receiving.json`。
- 本地独立虚拟环境通过专用 .pth 复用已有开发环境的第三方依赖；C 本体独立安装且完整字节比对。Windows CI 会直接安装正式 wheel 的 dev/data 依赖，不复用本地 .pth。
- `local-run/` 保留初次接收环境导入失败；`local-run-2/` 保留隔离 Python 不加载 examples 目录导致样例失败。修正依赖路径及受信 external examples 搜索路径后重跑通过，不改正式 wheel，不删除失败记录。

## Windows 执行入口

[接收脚本](../../examples/verify_c020a2_receiving.py) 和 [GitHub Actions 工作流](../../../.github/workflows/c020a2-windows-receiving.yml)。以 GitHub 官方 [Python 测试与产物归档流程](https://docs.github.com/en/actions/tutorials/build-and-test-code/python) 为基础，使用标准 Windows runner、Python3.13、只读 contents 权限，不访问 MySQL，不发布新包。

安装原固定摘要 wheel 及其 dev/data 依赖后，运行：

```powershell
python -X utf8 data_foundation/examples/verify_c020a2_receiving.py `
  --delivery-root data_foundation `
  --work-dir C:/c020a2-supplement/harness `
  --report-dir C:/c020a2-supplement/result `
  --suite all --require-windows
```

工作目录必须为空，脚本不删除已有文件。`--require-windows` 在非 Windows 明确失败；默认 files 用原 ZIP 中的 69 项，all 用原 ZIP 中的 311 项，均不使用后来新增的仓库测试冒充原交付测试。

JUnit 必须包含原生 persist_file 重命名、目录拒绝、激活/读取测试；只允许 Windows 缺失 os.mkfifo 的指定 FIFO 用例跳过，且必须记录原因。任一失败、其他跳过、测试数量不符、包版本或字节不符均产生 failed 回执。子进程使用 `-I -X utf8` 并清除业务数据库环境变量，避免连接用户正式库。

## 状态边界

Windows CI 状态以实际 Actions 运行和结果 artifact 为准，结果另行记录；本文件创建时尚未取得 Windows CI 结果。

正式接收仍分别保持：`Windows_official_C_receiving=pending`、`formal_DE_knowledge_chain=pending`。工具输出明确 MySQL、E JSON 片段绑定、HTTP/Worker重启未验证。即使 Windows CI 通过，也需要 E 使用同一正式包与真实 E0.5.1 产物接收后回传。

2026-10-15 核查 E [默认分支](https://github.com/w3022843643/shumei_intelligent_analysis) 仍为 `1e5ab6501f681d518479487ad31b93993c0e1fe4`，pyproject 版本 0.5.0；`intelligent_analysis/knowledge/artifacts.py` 返回404，分支列表只有main，GitHub Release列表为空。尚未取得E0.5.1正式包/样本、D仓库地址或新的Windows接收回执，不据此恢复爬虫第二步。
