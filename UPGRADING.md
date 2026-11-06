# 升级到 v2.0.0

1. 停止旧版 MCP 连接或完全退出宿主，避免新旧服务同时操作同一工作区。
2. 另存原目录及 `local_data/`。保留原目录中的个人 `mcp_config.json`、`codex_config.toml` 和环境配置。
3. 使用发布包中的源码、测试和文档覆盖原程序文件；不要删除或覆盖 `local_data/`。
4. 使用原 Python 虚拟环境；需要时执行 `python -m pip install -r requirements.txt`。
5. 将 `HOST_LEARNING_POLICY.md` 中的规则加入宿主系统指令。Codex 可放入用户目录的 `.codex/AGENTS.md`，
   保留其中原有规则，并按实际客户端名称调整 MCP 名称。不要用示例文档替换整份现有指令。
6. 重新连接 MCP 或重新启动宿主。新进程将先备份旧数据库，再迁移至 schema v4。
7. 确认发现 25 个工具，包括 `start_learning_session` 和 `record_learning_answer`。
8. 试用“讲解一下戴维南定理”：应有简短讲解和一道待回答问题；回答后应评价并继续当前节点。

GitHub 发布版本 v2.0.0 与内部数据库 schema v4 独立编号。

## 回退

关闭全部连接该工作区的 MCP 进程，配套恢复升级前的源码和数据库快照，再启动原版本。
旧版程序不支持 schema v4，不能仅恢复代码而保留已迁移的数据库。
`validate_backup_and_rehearse` 只在隔离目录演练，不会直接覆盖正在使用的数据。

## 验证

在项目目录运行 `python -m unittest discover -s tests -v`。测试使用隔离临时数据库。
