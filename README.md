# Personal Work Management & Knowledge Graph Mentor MCP

Python 3.11+、SQLite、Pydantic v2、官方 MCP SDK `FastMCP` 实现的本地技术导师。
服务通过 **stdio** 工作，无 HTTP 监听、云存储、遥测或外部模型调用。安装依赖需要网络；安装完成后服务可离线运行。

## 安装与运行

需要 Python 3.11 或更高版本。在克隆后的项目目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

Linux/macOS 使用 `.venv/bin/python` 替代 Windows 的 `.venv\Scripts\python.exe`。
这是 MCP stdio 子进程入口，启动后等待客户端协议消息；客户端应负责启动和停止进程。

### 接入 Codex

Windows PowerShell（从项目根目录运行）：

```powershell
$mentorPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$mentorServer = (Resolve-Path '.\server.py').Path
codex mcp add personal-work-knowledge-mentor --env PYTHONUTF8=1 -- $mentorPython $mentorServer
```

Linux/macOS：

```bash
codex mcp add personal-work-knowledge-mentor --env PYTHONUTF8=1 -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

刷新客户端 MCP 连接后可直接提出学习或项目目标。其他 MCP 客户端使用同样的 Python 解释器及脚本绝对路径进行 STDIO 配置。
配置方式参见 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。

依赖范围为 `mcp[cli]>=1.2.0,<2`、`pydantic>=2.0.0,<3`，保留官方 1.x `FastMCP` API。
[官方 SDK 文档](https://py.sdk.modelcontextprotocol.io/v1/)说明 `<2` 上限可避免升级到更名后的 v2 API。
本次验证环境为 Python 3.11.8、mcp 1.30.0、pydantic 2.14.3。
`requirements.lock.txt` 记录本次 Windows 验证环境，包含 `pywin32`；跨平台安装使用 `requirements.txt`。

## 数据位置

路径由 `Path(__file__).resolve().parent` 锚定，而不是当前工作目录：

```text
local_data/
├── agent_memory.db
├── backups/
│   ├── agent_memory_YYYYMMDD.db
│   └── agent_memory_YYYYMMDD_HHMMSS_microseconds_unique.db
└── vault/
    ├── 01_Dashboard_Tasks.md
    └── 02_Knowledge_Graph.md
```

首次启动即初始化数据库、创建当天快照、生成 Markdown。当天重复启动保留第一份快照；日期采用服务器本地时区。
手动备份每次产生不同文件名。备份使用 SQLite online backup API，得到独立一致的数据库文件，不直接复制正在写入的数据库。
快照先写临时文件，再原子发布，避免读到半份备份。保留策略由用户管理，本服务不删除历史备份。

可将 `local_data/vault` 直接作为 Obsidian vault 打开，或在支持 Mermaid 的 VS Code Markdown 预览中查看。
Markdown 是数据库的派生镜像，手工修改会在下次同步时被覆盖。两份文件分别原子替换，更新时间可用于判断是否来自同次导出。
思维导图需要查看器支持 Mermaid mindmap；关系图内置三种状态颜色，思维导图按领域归类并显示状态和分数。
领域过滤只显示该领域的节点，以及两端均在该领域中的边。

本服务不发送网络请求。MCP 客户端/宿主会收到工具和资源内容；若要求整个使用链条都留在本机，应选择本地部署的宿主模型和客户端。

## 强制工作流

1. 宿主拆解用户目标，调用 `propose_goal_and_check_prerequisites`。服务持久化提案，查询前置概念，缺失概念创建为 `WISHLIST`。
2. 返回 `PROBE_REQUIRED` 时，宿主逐条展示编号和全部四个选项，等待用户实际回答，如 `1B 2A`：

   | 选项 | 含义 | 状态 | 分数 |
   | --- | --- | --- | ---: |
   | A | 完全陌生 / 零基础 | WISHLIST | 0 |
   | B | 了解理论概念，但未动手实现过 | LEARNING | 50 |
   | C | 熟练掌握，可直接代码实战 | MASTERED | 100 |
   | D | 暂不深入，仅作为黑盒调用 | LEARNING | 30 |

3. 宿主将编号映射回概念，调用 `submit_probe_answers_and_create_pipeline`。服务拒绝没有提案、缺项、重复答案、错误领域或计划引用未提案概念的请求。
4. A/B 概念必须具有已关联的 `STUDY -> PRACTICE -> PROJECT` 路径。若宿主缺少完整学习实践对，服务自动在其任务前补入对应的一对 45 分钟任务，并给出推导、最小实验和验证标准。宿主已提供完整且顺序正确的任务对时予以保留。
5. 所有步骤通过 `prerequisite_task_id` 串联。最后必须是唯一的 `PROJECT`，关联全部前置概念；每个任务至少关联一个提案概念。C/D 不强制插入学习任务。全部已掌握时仍需提案，但允许空 `probe_answers`。
6. 使用 `update_task_status` 推进。前置不是 `DONE` 时，后续任务不能成为 `DOING` 或 `DONE`；应用逻辑和 SQLite trigger 双层校验。
7. `STUDY` 完成将尚未掌握概念升为 `LEARNING`，分数至少 60；`PRACTICE`/`PROJECT` 完成升为 `MASTERED`、100 分。返回新解锁任务与更新后的 Mermaid 图。

宿主提示词建议每次 25–90 分钟，输入模型保留需求指定的 15–180 分钟硬边界。
`BLOCKED` 前置完成后仅变成可开始，状态仍需显式更新。`DROPPED` 前置仍会阻塞后续任务。
若有 `DOING` 或 `DONE` 的直接后续任务，不能撤销其前置的完成状态；应先从后往前重置。
撤销完成状态不会抹除已经学到的知识，需要时显式更新知识状态。

服务能强制“先提案再提交、答案完整、任务结构正确”，不能独立证明宿主提交的答案确实来自人类，也不能独立判断宿主是否穷尽了真实前置知识。
`mentor_system_prompt` 明确要求宿主不得伪造答案或借日常知识工具绕过探测。掌握度是用户自述及任务记录，不是独立认证。

## 工具、资源与提示词

| 名称 | 作用 |
| --- | --- |
| `propose_goal_and_check_prerequisites` | 提案与 A/B/C/D 探测指令 |
| `submit_probe_answers_and_create_pipeline` | 校验答案，补齐学习实践，事务内创建依赖链 |
| `update_task_status` | 更新状态，校验前置，自动升级知识 |
| `get_workspace_overview` | 按领域和任务状态筛选，返回任务、统计及两种图 |
| `upsert_standalone_knowledge` | 记录概念、自述状态、父概念及前置概念 |
| `trigger_manual_backup_and_export` | 手动快照和 Markdown 导出，返回绝对路径 |

资源：`vault://dashboard/tasks`、`vault://dashboard/knowledge_graph`。
提示词：`mentor_system_prompt`。客户端可将它加载为导师上下文；服务器初始化指令也包含探测要求。

提案示例：

```json
{
  "goal_title": "实现直方图均衡化原型",
  "domain": "计算机视觉",
  "required_concepts": ["直方图", "累积分布函数"]
}
```

假设用户实际回答 `1B 2A`，提交示例（省略的学习实践任务会自动补齐）：

```json
{
  "goal_title": "实现直方图均衡化原型",
  "domain": "计算机视觉",
  "probe_answers": [
    {
      "concept_name": "直方图",
      "domain": "计算机视觉",
      "choice": "B",
      "summary": "直方图统计离散区间内观测值的出现频率。"
    },
    {
      "concept_name": "累积分布函数",
      "domain": "计算机视觉",
      "choice": "A",
      "summary": "累积分布函数给出随机变量不超过指定取值的概率。"
    }
  ],
  "tasks_pipeline": [
    {
      "title": "实现并验证灰度直方图均衡化原型",
      "task_type": "PROJECT",
      "priority": "P1",
      "estimated_minutes": 60,
      "definition_of_done": "实现灰度映射，对常量图和低对比度图运行测试，记录输入输出直方图并解释边界处理。",
      "linked_concepts": ["直方图", "累积分布函数"]
    }
  ],
  "edges": [
    {
      "source_concept": "直方图",
      "target_concept": "累积分布函数",
      "relation": "PREREQUISITE_FOR"
    }
  ]
}
```

## 一致性与恢复

- 每次写操作使用独立连接和 `BEGIN IMMEDIATE` 事务，开启外键、30 秒锁等待与 `synchronous=FULL`。连接在正常或异常路径都关闭。
- 排期将答案、知识边、任务、关联和提案状态一起提交；任何校验失败全部回滚。
- 目标标题与领域规范化后确定提案身份。重复提交相同输入返回原任务 ID；同一提案的不同排期被拒绝。需要独立批次时使用不同目标标题。
- 概念 ID 使用 Unicode NFKC 与大小写规范化的 slug，特殊标点加摘要后缀，避免 `C++` 与 `C#` 碰撞。概念身份全局唯一；同名概念出现在不同领域时返回冲突，请使用已有领域或明确限定概念名称。
- `PREREQUISITE_FOR` 和 `SUBTOPIC_OF` 分别禁止形成环，`APPLIES_TO` 可有环；拒绝自环和不存在的边端点。
- 每次数据库写入提交后立即重建镜像；跨进程导出也使用数据库锁，避免旧快照覆盖新内容。单文件使用临时文件、flush/fsync 与原子替换。
- 数据库提交成功、Markdown 导出失败时，返回 `VAULT_SYNC_FAILED` 和 `database_committed: true`。修复磁盘或权限问题后调用 `trigger_manual_backup_and_export`；重启也会重建镜像。排期用相同参数重试不会重复创建。
- 日志写入 stderr，stdout 保留给 MCP 协议。启动时初始化/备份失败会阻止服务启动，避免静默丢失备份保障。

恢复备份时先关闭该工作区的所有 MCP 子进程，将现有数据库另存，然后把选定快照复制为 `local_data/agent_memory.db`，再启动服务重建镜像。
备份是 SQLite 数据库，包含提案、任务、知识和关系，无须单独恢复 Markdown。

## 验证

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

本次 26 项测试全部通过。测试覆盖持久化探测、A/B 自动任务、C/D 映射、依赖与反向撤销限制、事务回滚、重试幂等、并发提交、
SQLite 备份完整性、Markdown 转义、Mermaid 安全标签、领域筛选以及导出失败恢复。
端到端测试用官方 `ClientSession` 启动真实 stdio 子进程，检查 6 个工具、2 个资源、提示词和完整任务完成流程，
并从不同工作目录启动以验证路径锚定。测试数据在项目 `local_data` 下的临时目录中隔离，正常结束后清理。
另使用 Mermaid 11.17.2 官方解析器检查空图及特殊字符标签，共 4 份关系图/思维导图通过；这些前端验证依赖不属于服务器运行依赖。

`models.py`、`db_manager.py`、`server.py` 和 `requirements.txt` 均为完整实现，无占位实现。
