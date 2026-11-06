# Personal Work Management & Knowledge Graph Mentor MCP

**第二版：v2.0.0** · 数据结构版本 schema v4（两者独立编号）。

[更新说明](CHANGELOG.md) · [升级指南](UPGRADING.md) · [学习调用规则](HOST_LEARNING_POLICY.md)

Python 3.11+、SQLite、Pydantic v2、官方 MCP SDK `FastMCP` 实现的本地技术导师。
服务通过 **stdio** 工作，无 HTTP 监听、云存储、遥测或外部模型调用。安装依赖需要网络；安装完成后服务可离线运行。

现在支持在项目工作会话中主动准备成果：根据真实进展选择可推进任务，组织代码、实验或文档草稿，保存验证证据，再交给用户审阅。
完整操作顺序见 [主动工作流程](PROACTIVE_WORKFLOW.md)。

## 知识问答自动跟进（schema v4）

“讲解一下戴维南定理”“为什么这样”“教我 X”等学习问题直接调用 `start_learning_session`，
无须用户再申请任务、指定测验，或先填写正式项目的 A/B/C/D 自评。
服务指令、工具说明和 `mentor_system_prompt` 同时声明此入口。

流程：简短讲解 → 存一道题并等待 → 记录用户实际作答和逐条评价 → 针对错误补讲并换题 →
独立解释（EXPLAIN）→ 独立应用（APPLY）→ 变式/边界迁移（TRANSFER）→ 主动请求确认 → 记录掌握。
表情式“懂了”、自评 C、助手生成的解答或用提示完成的答案均不算独立掌握证据。
电路用算题、画图、解释验证；不强制写代码或安排大型项目。

- `start_learning_session(concept, domain)`：创建或接续本概念学习节点，重复调用不重复建任务。
- `get_learning_followup(session_id?, domain?)`：恢复待答题、上次反馈和缺失证据；只跟进当前话题。
- `set_learning_question`：先持久化一道题及评分标准，再向用户展示并等待，一次只允许一道待答题。
- `record_learning_answer`：保存真实回答、逐项评价与提示标记；错误或有辅助则返回补讲/新题指令。
- `confirm_learning_mastery`：三个验证阶段均独立通过后，记录助手主动询问所得的真实确认。
- `control_learning_session`：用户要求时暂停/恢复，保留待答题；话题改变时不强迫继续。

进度写入 SQLite 并在任务仪表盘展示，重连后能接续。新表会在首次新版本启动时自动迁移，
迁移前保留旧数据库快照；不修改现有任务与历史学习证据。
正式项目的 C 选项现只记 LEARNING/70，表示用户自评，不能自行升级 MASTERED。

**宿主接入要求**：MCP 服务只能在被调用时返回下一步，不能自己观察聊天、调用模型或唤醒宿主。
为避免宿主把普通问答绕过 MCP，Codex 全局 `AGENTS.md` 应加入 `HOST_LEARNING_POLICY.md` 的规则；
其他客户端需把同样内容放进系统指令。刷新 MCP 连接后才会加载新工具与初始化指令。
评分和用户确认的真实性由宿主负责，服务强制证据结构与状态门槛，不能独立认证人类掌握程度。
用户明确要求简答、不测验或暂停时尊重选择；不创建后台轮询或提醒。

## 本次五项改进（schema v3）

- 知识记录支持局部更新：省略 `status` 或 `summary` 会保留原值，`summary=""` 可清空笔记。仅补笔记不会降低掌握度。
- 提案递归查找所有 `PREREQUISITE_FOR` 祖先，按基础先后顺序探测、去重，并保留跨领域前置的原领域。前置变动后须重新提案。
- 任务完成记录学习或实现证据，保持 `LEARNING`；黑盒调用记录 `USE`。只有已完成关联实践/项目、提供实现与原理解释证据并获用户确认后，才用 `verify_knowledge_mastery` 记录掌握。
- `edit_task` 修改标题、优先级、时长、验收标准及备注；`split_task` 拆分未开始任务并重连依赖；`replan_tasks` 调整顺序，保护已完成前缀、学习顺序和最终项目。变更会使未完成草稿过期。
- 常驻服务每 30 秒检查跨日备份，写入前也检查；本地变更记录包括修改前后值，可分页查询。恢复演练会校验完整性、外键、结构、记录数和草稿哈希，并在临时目录迁移、重建导出。

v1/v2 升级前自动备份，现有数据保留。历史记录从 v3 安装时开始，无法补造此前记录。
恢复校验使用 `validate_backup_and_rehearse`，不会替换正在使用的数据库；实际回退需停用服务并配套恢复代码与数据库。

## 主动参与工作

在宿主进入或恢复项目工作、记录进展以及完成阶段任务时，调用 `get_proactive_work_brief`。
有合适任务后，宿主直接准备可审核的实际成果：

```text
实际项目上下文 → 选择可推进任务 → 领取工作会话
             → 准备代码 / 实验 / 文档 → 保存与验证 → 用户审阅与修订
```

- `record_project_context`：记录完整当前快照，包括目的、进展、卡点和选定的真实源材料片段。
- `get_proactive_work_brief`：选出当前能推进的任务，显示待审阅成果、缺失上下文和卡点。
- `begin_proactive_work`：领取一小时工作会话，避免并发重复开工，返回任务验收标准和实际上下文。
- `save_work_artifact`：保存代码、实验或文档到 `local_data/drafts/`，返回本地路径与 SHA-256。
- `finish_proactive_work`：记录真实检查结果，提交草稿供用户审阅；未执行的检查明确标为 `NOT_RUN`。
- `review_proactive_work`：记录用户的接受、修改或拒绝决定，修改后继续同一工作会话。
- `get_work_session`：查询成果、验证记录、反馈以及生成时的上下文快照。

MCP 管理上下文、任务和交付状态，宿主 LLM 及其获授权的工具负责生成、检查和执行。
服务本身不会任意读取源文件、执行生成代码或定时唤醒宿主。源材料引用只是引用，正文须由宿主显式提供。
此次增加的是工作会话内的主动协作；独立后台触发需另行配置宿主能力。
[MCP 官方架构说明](https://modelcontextprotocol.io/specification/2025-11-25/architecture)将协调与模型集成交给宿主。

接受草稿与完成学习任务分别记录。接受草稿不会自动修改任务状态或知识掌握度。
前置未完成、任务显式阻塞、项目存在卡点时禁止开工；上下文变化会使未完成或待审阅草稿过期。
同一上下文下已接受/拒绝的成果不再自动重复推荐。审阅决定与检查结果来自宿主记录，服务无法独立证明人类审阅或测试真实性。

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
├── drafts/
│   └── <工作会话ID>/
└── vault/
    ├── 01_Dashboard_Tasks.md
    ├── 02_Knowledge_Graph.md
    └── 03_Active_Work.md
```

首次启动即初始化数据库、创建当天快照、生成 Markdown。当天重复启动保留第一份有效快照；日期采用服务器本地时区。
服务跨日常驻时，每 30 秒检查是否需要当天快照，写入前也检查；损坏的当天快照保留为 `.corrupt-*` 文件并重建。
手动备份每次产生不同文件名。备份使用 SQLite online backup API，得到独立一致的数据库文件，不直接复制正在写入的数据库。
快照先写临时文件、通过完整性与外键校验，再原子发布，避免读到半份备份。保留策略由用户管理，本服务不删除历史备份。

可将 `local_data/vault` 直接作为 Obsidian vault 打开，或在支持 Mermaid 的 VS Code Markdown 预览中查看。
Markdown 和 drafts 文件是数据库的派生镜像，手工修改会在下次同步时被覆盖；宿主修改草稿后应调用 `save_work_artifact` 保存最新正文。
文件分别原子替换，更新时间可用于判断是否来自同次导出。审阅通过的草稿保持只读；需要后续修订时先记录用户的修改决定。
思维导图需要查看器支持 Mermaid mindmap；关系图内置三种状态颜色，思维导图按领域归类并显示状态和分数。
领域过滤只显示该领域的节点，以及两端均在该领域中的边。

本服务不发送网络请求。MCP 客户端/宿主会收到工具和资源内容；若要求整个使用链条都留在本机，应选择本地部署的宿主模型和客户端。

## 强制工作流

1. 宿主拆解用户目标，调用 `propose_goal_and_check_prerequisites`。服务持久化提案，递归查询图谱中的前置概念，缺失的显式概念创建为 `WISHLIST`。最多 100 个前置，超出时需拆分目标。
2. 返回 `PROBE_REQUIRED` 时，宿主逐条展示编号和全部四个选项，等待用户实际回答，如 `1B 2A`：

   | 选项 | 含义 | 状态 | 分数 |
   | --- | --- | --- | ---: |
   | A | 完全陌生 / 零基础 | WISHLIST | 0 |
   | B | 了解理论概念，但尚不能独立应用 | LEARNING | 50 |
   | C | 自评能独立应用与解释，待验证 | LEARNING | 70 |
   | D | 暂不深入，仅作为黑盒调用 | LEARNING | 30 |

3. 宿主将编号映射回概念，调用 `submit_probe_answers_and_create_pipeline`。服务拒绝没有提案、缺项、重复答案、错误领域或计划引用未提案概念的请求。
4. A/B 概念必须具有已关联的 `STUDY -> PRACTICE -> PROJECT` 路径。若宿主缺少完整学习实践对，服务自动在其任务前补入对应的一对 45 分钟任务，并给出推导、最小实验和验证标准。宿主已提供完整且顺序正确的任务对时予以保留。
5. 所有步骤通过 `prerequisite_task_id` 串联。最后必须是唯一的 `PROJECT`，关联全部前置概念；每个任务至少关联一个提案概念。C/D 不强制插入学习任务。全部已掌握时仍需提案，但允许空 `probe_answers`。
6. 使用 `update_task_status` 推进。前置不是 `DONE` 时，后续任务不能成为 `DOING` 或 `DONE`；应用逻辑和 SQLite trigger 双层校验。
7. `STUDY` 完成记录 `LEARNING`（至少 60 分）；`PRACTICE`/`PROJECT` 记录实现证据（至少 80/85 分）；D 类黑盒使用只记录 `USE`（至少 50 分）。不会自动标成 `MASTERED`。已有掌握状态不被任务完成降低。返回新解锁任务与更新后的 Mermaid 图；概览附最近 20 条概念证据，完整记录保存在数据库与变更历史中。

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
| `update_task_status` | 更新状态，校验前置，记录学习和实现证据 |
| `get_workspace_overview` | 按领域和任务状态筛选，返回任务、统计及两种图 |
| `upsert_standalone_knowledge` | 记录概念、自述状态、父概念及前置概念 |
| `trigger_manual_backup_and_export` | 手动快照和 Markdown 导出，返回绝对路径 |
| `verify_knowledge_mastery` | 记录用户确认的实现与解释证据，升级掌握度 |
| `edit_task` | 局部修改任务，支持清空备注 |
| `split_task` | 原子拆分未开始任务并重连依赖；原 ID 留在第一步 |
| `replan_tasks` | 提交该目标所有任务 ID 的新顺序，保持完成前缀和学习顺序 |
| `get_change_history` | 按游标、实体类型和主键查询修改前后值 |
| `validate_backup_and_rehearse` | 校验指定备份或最新备份，隔离演练迁移与导出恢复 |

共 25 个工具：12 个管理工具、7 个主动工作工具及 6 个互动学习工具。
资源：`vault://dashboard/tasks`、`vault://dashboard/knowledge_graph`、`vault://dashboard/active_work`。
提示词：`mentor_system_prompt`、`proactive_work_prompt`。服务器初始化指令同时包含探测要求和主动准备交付物的流程。

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
备份是 SQLite 数据库，包含提案、任务、知识、关系、源材料快照和草稿正文；启动时可重建 Markdown 和 drafts。
数据库版本从 1 升级到 2 时，会先创建升级前快照，再以增量建表保留原有任务和知识。

## 验证

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

当前测试命令和结果见 VALIDATION.md。测试覆盖持久化探测、A/B 自动任务、C/D 映射、依赖与反向撤销限制、事务回滚、重试幂等、并发提交、
SQLite 备份完整性、Markdown 转义、Mermaid 安全标签、领域筛选以及导出失败恢复。
新增覆盖实际 Python 草稿执行、用户审阅/修订、并发领取、工作过期、源上下文快照、路径越界与大小写冲突、导出恢复和 v1 数据保留升级。
端到端测试用官方 `ClientSession` 启动真实 stdio 子进程，检查 25 个工具、3 个资源、2 个提示词和完整工作流，
并从不同工作目录启动以验证路径锚定。测试数据在项目 `local_data` 下的临时目录中隔离，正常结束后清理。
另使用 Mermaid 11.17.2 官方解析器检查空图及特殊字符标签，共 4 份关系图/思维导图通过；这些前端验证依赖不属于服务器运行依赖。

`models.py`、`learning.py`、`db_manager.py`、`server.py` 和 `requirements.txt` 均为完整实现，无占位实现。
