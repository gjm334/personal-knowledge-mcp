# v2.0.0 验证记录

日期：2026-11-06。发布前在独立 Git 工作副本验证。

- 执行命令：`python -m unittest discover -s tests -v`。
- 预期回归范围：80 项测试，包括 14 项互动学习验证；实际发布前结果追加在下方。
- 官方 MCP ClientSession 通过真实 stdio 子进程检查 25 个工具、3 个资源、2 个提示词、
  初始化调用规则、互动学习三个阶段、掌握确认和原有项目工作流。
- 覆盖错误补讲、辅助答案、自述/空回答拒绝、完整评分、幂等、并发、暂停恢复、领域隔离及 v1/v2/v3 迁移。
- 安装修复时使用真实旧数据库的隔离副本验证迁移，15 张既有表中的原记录保持不变；不发布该副本。
- 所有自动化作答均为隔离测试数据，没有将测试答案写入用户知识图谱，也未据此判断用户掌握。
- 发布文件使用白名单，仅含源码、测试和文档；排除数据库、备份、运行缓存、环境变量和个人连接配置。

服务保证状态/证据门槛；不能独立认证回答来源、宿主评分或宿主是否一定遵守调用指令。

发布副本实测：80 项测试全部通过（11.898 秒）；`pip check` 返回 No broken requirements found。

Fresh archive verification: extracted into a new directory without local_data; 80 tests passed in 12.441 seconds. Test fixtures now create their own scratch parent directory.
