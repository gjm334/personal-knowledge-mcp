"""Official FastMCP stdio server. No network listener or cloud storage is used."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

if __package__:
    from .db_manager import DatabaseManager, VaultSyncError, WorkspaceError
    from .models import KnowledgeEdgeInput, PipelineTaskInput, ProbeAnswer
else:
    from db_manager import DatabaseManager, VaultSyncError, WorkspaceError
    from models import KnowledgeEdgeInput, PipelineTaskInput, ProbeAnswer


_database: DatabaseManager | None = None
_database_lock = threading.Lock()


def get_database() -> DatabaseManager:
    global _database
    with _database_lock:
        if _database is None:
            _database = DatabaseManager()
        return _database


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Create today's local snapshot and refresh the vault before serving requests."""
    get_database()
    yield {}


mcp = FastMCP(
    "Personal-Work-Knowledge-Mentor",
    lifespan=lifespan,
    instructions=(
        "Before scheduling any project or learning goal, call propose_goal_and_check_prerequisites. "
        "If PROBE_REQUIRED, present its numbered A/B/C/D questions and wait for the user's answers. "
        "Use mentor_system_prompt for the full mentoring workflow. Never invent probe answers."
    ),
)


def _call(method: str, **arguments: Any) -> str:
    try:
        result = getattr(get_database(), method)(**arguments)
    except ValidationError as exc:
        result = {"status": "ERROR", "code": "VALIDATION_ERROR",
                  "message": "Invalid input", "errors": exc.errors(include_url=False, include_context=False, include_input=False)}
    except WorkspaceError as exc:
        result = {"status": "ERROR", "code": exc.code, "message": str(exc), **exc.details}
    except VaultSyncError as exc:
        result = {"status": "ERROR", "code": "VAULT_SYNC_FAILED", "database_committed": True,
                  "message": str(exc), "recovery_tool": "trigger_manual_backup_and_export"}
    except sqlite3.Error:
        logging.exception("Local database operation failed")
        result = {"status": "ERROR", "code": "DATABASE_ERROR",
                  "message": "Database operation failed; inspect server stderr for details"}
    except OSError:
        logging.exception("Local filesystem operation failed")
        result = {"status": "ERROR", "code": "FILESYSTEM_ERROR",
                  "message": "Check local_data permissions and available disk space; inspect server stderr"}
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def propose_goal_and_check_prerequisites(goal_title: str, domain: str, required_concepts: list[str]) -> str:
    """Mandatory first step for every goal. Return numbered A/B/C/D probes; wait for real user answers."""
    return _call("propose_goal_and_check_prerequisites", goal_title=goal_title,
                 domain=domain, required_concepts=required_concepts)


@mcp.tool()
def submit_probe_answers_and_create_pipeline(
    goal_title: str,
    domain: str,
    probe_answers: list[ProbeAnswer],
    tasks_pipeline: list[PipelineTaskInput],
    edges: list[KnowledgeEdgeInput],
) -> str:
    """Schedule a previously proposed goal using actual probe answers.

    Every task must link proposed concepts. End with one PROJECT linked to all prerequisites.
    For A/B answers provide STUDY -> PRACTICE preparation; missing preparation is inserted
    automatically. C/D need no preparation. Identical retries return the existing schedule.
    """
    return _call("submit_probe_answers_and_create_pipeline", goal_title=goal_title,
                 domain=domain, probe_answers=probe_answers, tasks_pipeline=tasks_pipeline, edges=edges)


@mcp.tool()
def update_task_status(task_id: int, new_status: str, notes: str = "") -> str:
    """Set TODO/DOING/BLOCKED/DONE/DROPPED; enforce dependencies and upgrade linked knowledge on DONE."""
    return _call("update_task_status", task_id=task_id, new_status=new_status, notes=notes)


@mcp.tool()
def get_workspace_overview(domain: str | None = None, status_filter: str | None = None) -> str:
    """Read tasks, readiness, knowledge statistics and both Mermaid diagrams, optionally filtered."""
    return _call("get_workspace_overview", domain=domain, status_filter=status_filter)


@mcp.tool()
def upsert_standalone_knowledge(
    name: str,
    domain: str,
    status: str = "LEARNING",
    summary: str = "",
    parent_concept: str | None = None,
    prerequisite_concept: str | None = None,
) -> str:
    """Record user-declared knowledge (WISHLIST/LEARNING/MASTERED) and optional parent/prerequisite edges.

    Use this for explicit user knowledge notes, never to bypass a goal's multiple-choice probe.
    """
    return _call("upsert_standalone_knowledge", name=name, domain=domain, status=status,
                 summary=summary, parent_concept=parent_concept, prerequisite_concept=prerequisite_concept)


@mcp.tool()
def trigger_manual_backup_and_export() -> str:
    """Create a unique timestamped SQLite snapshot and refresh both Markdown files; return absolute paths."""
    return _call("trigger_manual_backup_and_export")


@mcp.resource("vault://dashboard/tasks")
def tasks_dashboard() -> str:
    """Human-readable local task dashboard."""
    return get_database().read_vault("tasks")


@mcp.resource("vault://dashboard/knowledge_graph")
def knowledge_dashboard() -> str:
    """Local Mermaid relationship graph, domain mindmap and knowledge tables."""
    return get_database().read_vault("knowledge_graph")


@mcp.prompt()
def mentor_system_prompt() -> str:
    """Technical mentor instructions for prerequisite probing and learning through practice."""
    return """你是主动、严谨的个人工作管理与知识图谱技术导师。

1. 用户提出项目或学习目标时，先拆解达成目标真正需要的前置概念，再调用
   propose_goal_and_check_prerequisites。required_concepts 必须覆盖所有准备任务与最终项目用到的概念。
   任何复杂目标都不得跳过该步骤；不得仅因用户使用了专业术语就推断其已掌握知识。
2. 若返回 PROBE_REQUIRED，立即逐条展示服务端给出的编号和概念，并为每条完整展示：
   [A] 完全陌生 / 零基础
   [B] 了解理论概念，但未动手实现过
   [C] 熟练掌握，可直接代码实战
   [D] 暂不深入，仅作为黑盒调用
   提示用户可回答“1B 2A”。保持编号与概念映射，等待真实回答；不得猜测或替用户选择。
   回答含糊或缺项时只追问缺项。将每个答案转为 ProbeAnswer，summary 写准确的一句技术定义。
3. 收到回答后调用 submit_probe_answers_and_create_pipeline。对于 A/B 概念，安排
   STUDY（理论、假设、推导）-> PRACTICE（可运行玩具实验、基线和边界验证）-> PROJECT。
   C 可直接实战；D 尊重用户黑盒选择，明确接口、输入输出及使用边界，不强制理论任务。
   全部 MASTERED 时仍先提案，随后可用空 probe_answers 创建项目。
4. 每个任务使用动词加对象的标题，给出可检验的 definition_of_done，建议单次 25–90 分钟。
   对超过 90 分钟的工作拆分准备任务，避免笼统的“学习某领域”。最终只放一个 PROJECT，
   关联所有前置概念；所有任务必须关联提案中的概念。服务端会补齐缺失的 A/B 学习实践步骤。
5. 严格按 prerequisite_task_id 顺序推进，前置未 DONE 时不得将后续任务标为 DOING 或 DONE。
   用户确认达到验收标准后才调用 update_task_status(..., 'DONE')。展示新解锁任务。
   STUDY 完成升为 LEARNING（至少 60 分），PRACTICE/PROJECT 完成升为 MASTERED（100 分）。
   掌握度是自述与完成记录的工作模型，不代表独立测评认证；撤销任务完成不会自动抹除已学知识。
6. 知识状态改变后，在聊天中原样展示返回的 Mermaid 图代码块，解释本次新增或升级的概念。
   domain mindmap 用于领域概览，graph TD 用于关系和状态。优先推荐当前可开始且优先级高的任务。
7. 遇到 ERROR 时解释具体原因并修正输入；不得宣称操作成功。database_committed=true 时数据库
   已提交，调用 trigger_manual_backup_and_export 修复导出；相同排期输入重试不会重复创建任务。
   BLOCKED 任务在前置完成后仅成为可开始，不会自动变为 DOING；DROPPED 前置仍阻塞后续任务。
8. 数据仅由本服务在本地 local_data 保存，不调用外部服务。MCP 宿主能读取返回数据，
   宿主 LLM 的部署与隐私策略由用户选择。Markdown 是数据库的只读镜像；修改请用工具。
   记录日常概念可用 upsert_standalone_knowledge，但不得用它伪造已掌握状态绕过用户探测。
"""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    mcp.run(transport="stdio")
