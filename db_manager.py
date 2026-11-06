"""Local transactional storage, prerequisite enforcement, backups and vault export."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

if __package__:
    from .models import (
        GoalInput, KnowledgeInput, OverviewInput, PipelineInput, PipelineTaskInput, TaskUpdateInput,
    )
else:
    from models import (
        GoalInput, KnowledgeInput, OverviewInput, PipelineInput, PipelineTaskInput, TaskUpdateInput,
    )


LOGGER = logging.getLogger(__name__)
CHOICES = {
    "A": {"label": "完全陌生 / 零基础", "status": "WISHLIST", "score": 0},
    "B": {"label": "了解理论概念，但未动手实现过", "status": "LEARNING", "score": 50},
    "C": {"label": "熟练掌握，可直接代码实战", "status": "MASTERED", "score": 100},
    "D": {"label": "暂不深入，仅作为黑盒调用", "status": "LEARNING", "score": 30},
}
CLASS_DEFS = (
    "classDef mastered fill:#d4edda,stroke:#28a745,color:#155724,stroke-width:2px;",
    "classDef learning fill:#fff3cd,stroke:#ffc107,color:#856404,stroke-width:2px;",
    "classDef wishlist fill:#f8f9fa,stroke:#6c757d,color:#6c757d,stroke-dasharray: 5 5;",
)

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
    task_type TEXT NOT NULL CHECK(task_type IN ('STUDY','PRACTICE','PROJECT')),
    priority TEXT NOT NULL DEFAULT 'P2' CHECK(priority IN ('P0','P1','P2','P3')),
    status TEXT NOT NULL DEFAULT 'TODO'
        CHECK(status IN ('TODO','DOING','BLOCKED','DONE','DROPPED')),
    estimated_minutes INTEGER NOT NULL DEFAULT 45 CHECK(estimated_minutes BETWEEN 15 AND 180),
    definition_of_done TEXT NOT NULL,
    prerequisite_task_id INTEGER REFERENCES tasks(id),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'WISHLIST'
        CHECK(status IN ('WISHLIST','LEARNING','MASTERED')),
    mastery_score INTEGER NOT NULL DEFAULT 0 CHECK(mastery_score BETWEEN 0 AND 100),
    summary TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK((status = 'MASTERED' AND mastery_score = 100) OR
          (status <> 'MASTERED' AND mastery_score < 100))
);
CREATE TABLE IF NOT EXISTS knowledge_edges (
    source_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    target_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    relation TEXT NOT NULL CHECK(relation IN ('PREREQUISITE_FOR','SUBTOPIC_OF','APPLIES_TO')),
    PRIMARY KEY(source_id,target_id,relation),
    CHECK(source_id <> target_id)
);
CREATE TABLE IF NOT EXISTS task_knowledge_links (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    node_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    PRIMARY KEY(task_id,node_id)
);
CREATE TABLE IF NOT EXISTS goal_proposals (
    id TEXT PRIMARY KEY,
    goal_title TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PROBE_REQUIRED','READY_TO_SCHEDULE','SCHEDULED')),
    pipeline_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS goal_requirements (
    goal_id TEXT NOT NULL REFERENCES goal_proposals(id),
    node_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    position INTEGER NOT NULL,
    probe_required INTEGER NOT NULL CHECK(probe_required IN (0,1)),
    PRIMARY KEY(goal_id,node_id),
    UNIQUE(goal_id,position)
);
CREATE TABLE IF NOT EXISTS pipeline_tasks (
    goal_id TEXT NOT NULL REFERENCES goal_proposals(id),
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
    position INTEGER NOT NULL,
    PRIMARY KEY(goal_id,position)
);
CREATE TABLE IF NOT EXISTS probe_responses (
    goal_id TEXT NOT NULL REFERENCES goal_proposals(id),
    node_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    choice TEXT NOT NULL CHECK(choice IN ('A','B','C','D')),
    summary TEXT NOT NULL,
    PRIMARY KEY(goal_id,node_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_prerequisite ON tasks(prerequisite_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_node ON task_knowledge_links(node_id);
CREATE INDEX IF NOT EXISTS idx_nodes_domain ON knowledge_nodes(domain);
CREATE TRIGGER IF NOT EXISTS tasks_dependency_insert
BEFORE INSERT ON tasks
WHEN NEW.status IN ('DOING','DONE') AND NEW.prerequisite_task_id IS NOT NULL
 AND COALESCE((SELECT status FROM tasks WHERE id=NEW.prerequisite_task_id),'') <> 'DONE'
BEGIN SELECT RAISE(ABORT,'Prerequisite task must be DONE'); END;
CREATE TRIGGER IF NOT EXISTS tasks_dependency_update
BEFORE UPDATE OF status,prerequisite_task_id ON tasks
WHEN NEW.status IN ('DOING','DONE') AND NEW.prerequisite_task_id IS NOT NULL
 AND COALESCE((SELECT status FROM tasks WHERE id=NEW.prerequisite_task_id),'') <> 'DONE'
BEGIN SELECT RAISE(ABORT,'Prerequisite task must be DONE'); END;
CREATE TRIGGER IF NOT EXISTS tasks_protect_completed_dependency
BEFORE UPDATE OF status ON tasks
WHEN OLD.status='DONE' AND NEW.status<>'DONE'
 AND EXISTS(SELECT 1 FROM tasks WHERE prerequisite_task_id=OLD.id AND status IN ('DOING','DONE'))
BEGIN SELECT RAISE(ABORT,'Active or completed dependent tasks prevent reopening'); END;
PRAGMA user_version=1;
COMMIT;
"""


class WorkspaceError(ValueError):
    """An actionable, structured domain error returned to the host LLM."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class VaultSyncError(OSError):
    """The database is committed; a subsequent export needs to be retried."""


def canonical(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def concept_id(name: str) -> str:
    """Unicode lowercase slug; hash punctuation to avoid C++/C# collisions."""
    normalized = canonical(name)
    if not normalized:
        raise WorkspaceError("INVALID_NAME", "Concept names cannot be blank")
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE).strip("-_")
    if slug != normalized:
        suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return f"{slug or 'concept'}-{suffix}"
    return slug


def mermaid_label(value: str) -> str:
    text = " ".join(value.split())
    special = set('&<>"\'`[]{}()|/\\:#;')
    return "".join(f"#{ord(char)};" if char in special else char for char in text)


def md(value: Any) -> str:
    escaped = html.escape(str(value), quote=True)
    for old, new in (("|", "&#124;"), ("`", "&#96;"), ("[", "&#91;"),
                     ("]", "&#93;"), ("*", "&#42;"), ("_", "&#95;"),
                     ("\\", "&#92;")):
        escaped = escaped.replace(old, new)
    return escaped.replace("\r", " ").replace("\n", "<br>")


class DatabaseManager:
    """One local workspace. Each transaction owns and closes its connection."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or Path(__file__).resolve().parent / "local_data").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "agent_memory.db"
        self.backups_dir = self.data_dir / "backups"
        self.vault_dir = self.data_dir / "vault"
        self.backups_dir.mkdir(exist_ok=True)
        self.vault_dir.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise WorkspaceError("SCHEMA_VERSION", f"Unsupported database version: {version}")
            connection.executescript(SCHEMA)
        self.backup_database()
        self.sync_to_markdown_vault()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            with sqlite3.connect(self.db_path, timeout=30) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("PRAGMA synchronous=FULL")
                yield connection
        finally:
            # sqlite3's context manager commits/rolls back but does not close.
            if connection is not None:
                connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
            try:
                self.sync_to_markdown_vault()
            except (OSError, sqlite3.Error) as exc:
                LOGGER.exception("Database committed, vault export failed")
                raise VaultSyncError("Database committed; retry trigger_manual_backup_and_export") from exc

    @staticmethod
    def _goal_id(title: str, domain: str) -> str:
        payload = json.dumps([canonical(title), canonical(domain)], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _node(connection: sqlite3.Connection, name: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM knowledge_nodes WHERE id=?", (concept_id(name),)).fetchone()

    def _ensure_node(self, connection: sqlite3.Connection, name: str, domain: str) -> sqlite3.Row:
        node = self._node(connection, name)
        if node and canonical(node["domain"]) != canonical(domain):
            raise WorkspaceError(
                "DOMAIN_CONFLICT", f"Concept {name!r} already belongs to {node['domain']!r}",
                hint="Use the existing domain or qualify the concept name for a distinct meaning",
            )
        if not node:
            connection.execute(
                "INSERT INTO knowledge_nodes(id,name,domain) VALUES(?,?,?)",
                (concept_id(name), name.strip(), domain.strip()),
            )
            node = self._node(connection, name)
        return node

    def propose_goal_and_check_prerequisites(self, **arguments: Any) -> dict[str, Any]:
        request = GoalInput.model_validate(arguments)
        goal_id = self._goal_id(request.goal_title, request.domain)
        with self._write() as connection:
            nodes = {concept_id(name): self._ensure_node(connection, name, request.domain)
                     for name in request.required_concepts}
            existing = connection.execute("SELECT * FROM goal_proposals WHERE id=?", (goal_id,)).fetchone()
            if existing:
                previous = {row[0] for row in connection.execute(
                    "SELECT node_id FROM goal_requirements WHERE goal_id=?", (goal_id,))}
                if previous != set(nodes):
                    raise WorkspaceError("GOAL_CONFLICT", "This goal already has different prerequisites; use a distinct goal title")
                if existing["status"] == "SCHEDULED":
                    return {"status": "ALREADY_SCHEDULED", "goal_id": goal_id,
                            "tasks": self._goal_tasks(connection, goal_id)}
            else:
                probe_needed = any(node["status"] != "MASTERED" for node in nodes.values())
                connection.execute(
                    "INSERT INTO goal_proposals(id,goal_title,domain,status) VALUES(?,?,?,?)",
                    (goal_id, request.goal_title, request.domain,
                     "PROBE_REQUIRED" if probe_needed else "READY_TO_SCHEDULE"),
                )
                connection.executemany(
                    "INSERT INTO goal_requirements(goal_id,node_id,position,probe_required) VALUES(?,?,?,?)",
                    [(goal_id, node["id"], position, int(node["status"] != "MASTERED"))
                     for position, node in enumerate(nodes.values(), 1)],
                )
            connection.execute(
                "UPDATE goal_requirements SET probe_required=CASE WHEN "
                "(SELECT status FROM knowledge_nodes WHERE id=goal_requirements.node_id)='MASTERED' "
                "THEN 0 ELSE 1 END WHERE goal_id=?", (goal_id,),
            )
            required = connection.execute(
                "SELECT n.*,r.probe_required FROM goal_requirements r JOIN knowledge_nodes n "
                "ON n.id=r.node_id WHERE r.goal_id=? ORDER BY r.position", (goal_id,),
            ).fetchall()
            questions = [
                {"number": index, "concept_name": node["name"], "domain": node["domain"],
                 "question": f"你对「{node['name']}」的掌握程度是？",
                 "options": [{"choice": key, **value} for key, value in CHOICES.items()]}
                for index, node in enumerate((n for n in required if n["probe_required"]), 1)
            ]
            connection.execute("UPDATE goal_proposals SET status=? WHERE id=?",
                               ("PROBE_REQUIRED" if questions else "READY_TO_SCHEDULE", goal_id))
            return {
                "status": "PROBE_REQUIRED" if questions else "READY_TO_SCHEDULE",
                "goal_id": goal_id, "goal_title": request.goal_title, "domain": request.domain,
                "mastered_concepts": [n["name"] for n in required if n["status"] == "MASTERED"],
                "questions": questions,
                "directive": (
                    "立即按编号向用户逐项展示 A/B/C/D 选项，等待用户回答（例如 1B 2A）。"
                    "禁止创建或声称已创建任务。收到回答后，保持编号与概念对应，调用 "
                    "submit_probe_answers_and_create_pipeline；A/B 必须先 STUDY 再 PRACTICE，最后 PROJECT。"
                    if questions else
                    "全部前置概念已掌握。调用 submit_probe_answers_and_create_pipeline 创建最终 PROJECT；"
                    "probe_answers 可为空，项目必须关联所有前置概念。"
                ),
            }

    def submit_probe_answers_and_create_pipeline(self, **arguments: Any) -> dict[str, Any]:
        request = PipelineInput.model_validate(arguments)
        goal_id = self._goal_id(request.goal_title, request.domain)
        fingerprint = hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()
        with self._write() as connection:
            goal = connection.execute("SELECT * FROM goal_proposals WHERE id=?", (goal_id,)).fetchone()
            if not goal:
                raise WorkspaceError("PROPOSAL_REQUIRED", "Call propose_goal_and_check_prerequisites before scheduling")
            if goal["status"] == "SCHEDULED":
                if goal["pipeline_fingerprint"] != fingerprint:
                    raise WorkspaceError("ALREADY_SCHEDULED", "Goal already scheduled with different inputs; use a new goal title")
                tasks = self._goal_tasks(connection, goal_id)
                replay = True
            else:
                requirements = connection.execute(
                    "SELECT n.*,r.probe_required FROM goal_requirements r JOIN knowledge_nodes n "
                    "ON n.id=r.node_id WHERE r.goal_id=? ORDER BY r.position", (goal_id,),
                ).fetchall()
                answers = {}
                required_ids = {row["id"] for row in requirements}
                for answer in request.probe_answers:
                    node_id = concept_id(answer.concept_name)
                    if canonical(answer.domain) != canonical(request.domain) or node_id not in required_ids:
                        raise WorkspaceError("UNEXPECTED_ANSWER", f"Answer for {answer.concept_name!r} does not match the proposal")
                    if node_id in answers:
                        raise WorkspaceError("DUPLICATE_ANSWER", f"Duplicate answer for {answer.concept_name!r}")
                    answers[node_id] = answer
                missing = [n["name"] for n in requirements if n["probe_required"] and n["id"] not in answers]
                # Recheck current state: a previously mastered prerequisite can be downgraded.
                missing += [n["name"] for n in requirements
                            if not n["probe_required"] and n["status"] != "MASTERED" and n["id"] not in answers]
                if missing:
                    raise WorkspaceError("PROBE_REQUIRED", "User answers are required before scheduling", concepts=missing)
                tasks_input = list(request.tasks_pipeline)
                if tasks_input[-1].task_type != "PROJECT":
                    raise WorkspaceError("PROJECT_REQUIRED", "The final pipeline task must be PROJECT")
                if any(task.task_type == "PROJECT" for task in tasks_input[:-1]):
                    raise WorkspaceError("PIPELINE_ORDER", "Place the single final PROJECT after all preparation tasks")
                task_links = [{concept_id(name) for name in task.linked_concepts} for task in tasks_input]
                if not required_ids.issubset(task_links[-1]):
                    raise WorkspaceError("PROJECT_LINKS_REQUIRED", "Final PROJECT must link every required concept")
                all_links = set().union(*task_links)
                if not all_links.issubset(required_ids):
                    raise WorkspaceError("UNGATED_CONCEPT", "Propose all pipeline concepts before scheduling")
                if any(not links for links in task_links):
                    raise WorkspaceError("TASK_LINKS_REQUIRED", "Every task must link at least one proposed concept")
                preparation = []
                for node_id, answer in answers.items():
                    if answer.choice in ("A", "B"):
                        studies = [i for i, task in enumerate(tasks_input[:-1])
                                   if task.task_type == "STUDY" and node_id in task_links[i]]
                        practices = [i for i, task in enumerate(tasks_input[:-1])
                                     if task.task_type == "PRACTICE" and node_id in task_links[i]]
                        if not studies or not practices or min(practices) < min(studies):
                            preparation += [
                                PipelineTaskInput(
                                    title=f"梳理 {answer.concept_name} 的原理与推导"[:300], task_type="STUDY",
                                    definition_of_done=f"用自己的话解释“{answer.summary}”；列出三个关键假设，手算一个最小例子并记录推导。"[:5000],
                                    linked_concepts=[answer.concept_name],
                                ),
                                PipelineTaskInput(
                                    title=f"实现并验证 {answer.concept_name} 的最小实验"[:300], task_type="PRACTICE",
                                    definition_of_done="提交可独立运行的最小示例；验证一个正常输入与一个边界输入，记录预期和实际结果及差异原因。",
                                    linked_concepts=[answer.concept_name],
                                ),
                            ]
                tasks_input = preparation + tasks_input
                task_links = [{concept_id(name) for name in task.linked_concepts} for task in tasks_input]
                for node_id, answer in answers.items():
                    mapping = CHOICES[answer.choice]
                    connection.execute(
                        "UPDATE knowledge_nodes SET status=?,mastery_score=?,summary=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (mapping["status"], mapping["score"], answer.summary, node_id),
                    )
                    connection.execute(
                        "INSERT INTO probe_responses(goal_id,node_id,choice,summary) VALUES(?,?,?,?)",
                        (goal_id, node_id, answer.choice, answer.summary),
                    )
                for edge in request.edges:
                    self._insert_edge(connection, edge.source_concept, edge.target_concept, edge.relation)
                previous_id = None
                for position, task in enumerate(tasks_input, 1):
                    cursor = connection.execute(
                        "INSERT INTO tasks(title,task_type,priority,estimated_minutes,definition_of_done,prerequisite_task_id) "
                        "VALUES(?,?,?,?,?,?)",
                        (task.title, task.task_type, task.priority, task.estimated_minutes,
                         task.definition_of_done, previous_id),
                    )
                    previous_id = cursor.lastrowid
                    connection.execute("INSERT INTO pipeline_tasks VALUES(?,?,?)", (goal_id, previous_id, position))
                    connection.executemany(
                        "INSERT INTO task_knowledge_links(task_id,node_id) VALUES(?,?)",
                        [(previous_id, node_id) for node_id in sorted(task_links[position - 1])],
                    )
                connection.execute(
                    "UPDATE goal_proposals SET status='SCHEDULED',pipeline_fingerprint=? WHERE id=?", (fingerprint, goal_id),
                )
                tasks = self._goal_tasks(connection, goal_id)
                replay = False
        return {"status": "PIPELINE_CREATED", "goal_id": goal_id, "idempotent_replay": replay,
                "tasks": tasks, "mermaid": self.generate_mermaid_graph()}

    def _insert_edge(self, connection: sqlite3.Connection, source: str, target: str, relation: str) -> None:
        source_node, target_node = self._node(connection, source), self._node(connection, target)
        if not source_node or not target_node:
            raise WorkspaceError("UNKNOWN_EDGE_CONCEPT", "Edge endpoints must already exist", source=source, target=target)
        if source_node["id"] == target_node["id"]:
            raise WorkspaceError("SELF_EDGE", "A concept cannot be its own dependency or parent")
        if relation in ("PREREQUISITE_FOR", "SUBTOPIC_OF"):
            cycle = connection.execute(
                "WITH RECURSIVE reachable(id) AS (SELECT target_id FROM knowledge_edges WHERE source_id=? AND relation=? "
                "UNION SELECT e.target_id FROM knowledge_edges e JOIN reachable r ON e.source_id=r.id WHERE e.relation=?) "
                "SELECT 1 FROM reachable WHERE id=? LIMIT 1",
                (target_node["id"], relation, relation, source_node["id"]),
            ).fetchone()
            if cycle:
                raise WorkspaceError("KNOWLEDGE_CYCLE", f"{relation} must not form a cycle")
        connection.execute("INSERT OR IGNORE INTO knowledge_edges VALUES(?,?,?)",
                           (source_node["id"], target_node["id"], relation))

    def update_task_status(self, **arguments: Any) -> dict[str, Any]:
        request = TaskUpdateInput.model_validate(arguments)
        with self._write() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (request.task_id,)).fetchone()
            if not task:
                raise WorkspaceError("TASK_NOT_FOUND", f"Task #{request.task_id} does not exist")
            if request.new_status in ("DOING", "DONE") and task["prerequisite_task_id"] is not None:
                prerequisite = connection.execute("SELECT * FROM tasks WHERE id=?", (task["prerequisite_task_id"],)).fetchone()
                if prerequisite["status"] != "DONE":
                    raise WorkspaceError("DEPENDENCY_NOT_DONE", f"Finish task #{prerequisite['id']}: {prerequisite['title']} first",
                                         prerequisite_task_id=prerequisite["id"], prerequisite_status=prerequisite["status"])
            if task["status"] == "DONE" and request.new_status != "DONE":
                dependents = connection.execute(
                    "SELECT id FROM tasks WHERE prerequisite_task_id=? AND status IN ('DOING','DONE')", (request.task_id,),
                ).fetchall()
                if dependents:
                    raise WorkspaceError("ACTIVE_DEPENDENTS", "Reset dependent tasks before reopening their prerequisite",
                                         task_ids=[row["id"] for row in dependents])
            connection.execute(
                "UPDATE tasks SET status=?,notes=CASE WHEN ?='' THEN notes ELSE ? END,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (request.new_status, request.notes, request.notes, request.task_id),
            )
            if request.new_status == "DONE":
                if task["task_type"] == "STUDY":
                    connection.execute(
                        "UPDATE knowledge_nodes SET status='LEARNING',mastery_score=MAX(mastery_score,60),updated_at=CURRENT_TIMESTAMP "
                        "WHERE status<>'MASTERED' AND id IN (SELECT node_id FROM task_knowledge_links WHERE task_id=?)", (request.task_id,),
                    )
                else:
                    connection.execute(
                        "UPDATE knowledge_nodes SET status='MASTERED',mastery_score=100,updated_at=CURRENT_TIMESTAMP "
                        "WHERE id IN (SELECT node_id FROM task_knowledge_links WHERE task_id=?)", (request.task_id,),
                    )
            unlocked = []
            if request.new_status == "DONE" and task["status"] != "DONE":
                unlocked = self._tasks(connection, "t.prerequisite_task_id=? AND t.status IN ('TODO','BLOCKED')", [request.task_id])
            updated = self._tasks(connection, "t.id=?", [request.task_id])[0]
            nodes = [dict(row) for row in connection.execute(
                "SELECT n.* FROM knowledge_nodes n JOIN task_knowledge_links l ON n.id=l.node_id WHERE l.task_id=? ORDER BY n.name",
                (request.task_id,),
            )]
        return {"status": "TASK_UPDATED", "task": updated, "newly_unlocked_tasks": unlocked,
                "updated_knowledge": nodes, "mermaid": self.generate_mermaid_graph()}

    def upsert_standalone_knowledge(self, **arguments: Any) -> dict[str, Any]:
        request = KnowledgeInput.model_validate(arguments)
        with self._write() as connection:
            node = self._ensure_node(connection, request.name, request.domain)
            score = {"WISHLIST": 0, "LEARNING": 50, "MASTERED": 100}[request.status]
            connection.execute(
                "UPDATE knowledge_nodes SET status=?,mastery_score=?,summary=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (request.status, score, request.summary, node["id"]),
            )
            if request.parent_concept:
                self._ensure_node(connection, request.parent_concept, request.domain)
                self._insert_edge(connection, request.name, request.parent_concept, "SUBTOPIC_OF")
            if request.prerequisite_concept:
                self._ensure_node(connection, request.prerequisite_concept, request.domain)
                self._insert_edge(connection, request.prerequisite_concept, request.name, "PREREQUISITE_FOR")
            result = dict(self._node(connection, request.name))
        return {"status": "KNOWLEDGE_UPDATED", "node": result, "mermaid": self.generate_mermaid_graph()}

    @staticmethod
    def _tasks(connection: sqlite3.Connection, where: str = "1=1", parameters: list[Any] | None = None) -> list[dict[str, Any]]:
        # 'where' is assembled solely from internal SQL fragments, never client text.
        rows = connection.execute(
            "SELECT t.*,p.status AS prerequisite_status FROM tasks t "
            "LEFT JOIN tasks p ON p.id=t.prerequisite_task_id WHERE " + where +
            " ORDER BY CASE t.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,t.id",
            parameters or [],
        ).fetchall()
        links: dict[int, list[str]] = {}
        for link in connection.execute(
            "SELECT l.task_id,n.name FROM task_knowledge_links l JOIN knowledge_nodes n ON n.id=l.node_id ORDER BY n.name"
        ):
            links.setdefault(link["task_id"], []).append(link["name"])
        result = []
        for row in rows:
            task = dict(row)
            task["linked_concepts"] = links.get(row["id"], [])
            task["ready_to_start"] = row["status"] in ("TODO", "BLOCKED") and (
                row["prerequisite_task_id"] is None or row["prerequisite_status"] == "DONE")
            result.append(task)
        return result

    def _goal_tasks(self, connection: sqlite3.Connection, goal_id: str) -> list[dict[str, Any]]:
        tasks = self._tasks(connection, "t.id IN (SELECT task_id FROM pipeline_tasks WHERE goal_id=?)", [goal_id])
        return sorted(tasks, key=lambda task: task["id"])

    def get_workspace_overview(self, domain: str | None = None, status_filter: str | None = None) -> dict[str, Any]:
        request = OverviewInput(domain=domain, status_filter=status_filter)
        with self._connection() as connection:
            connection.execute("BEGIN")
            where, parameters = [], []
            if request.status_filter:
                where.append("t.status=?")
                parameters.append(request.status_filter)
            if request.domain:
                goal_ids = [row["id"] for row in connection.execute("SELECT id,domain FROM goal_proposals")
                            if canonical(row["domain"]) == canonical(request.domain)]
                if goal_ids:
                    placeholders = ",".join("?" for _ in goal_ids)
                    where.append(f"t.id IN (SELECT task_id FROM pipeline_tasks WHERE goal_id IN ({placeholders}))")
                    parameters.extend(goal_ids)
                else:
                    where.append("0=1")
            tasks = self._tasks(connection, " AND ".join(where) or "1=1", parameters)
            nodes, edges = self._graph_data(connection, request.domain)
            statistics = {status: sum(n["status"] == status for n in nodes)
                          for status in ("WISHLIST", "LEARNING", "MASTERED")}
            return {"status": "OK", "tasks": tasks, "knowledge_nodes": nodes,
                    "knowledge_statistics": {"total": len(nodes), **statistics},
                    "mermaid": self._render_graph(nodes, edges),
                    "vault_paths": self.vault_paths()}

    def vault_paths(self) -> dict[str, str]:
        return {"tasks": str(self.vault_dir / "01_Dashboard_Tasks.md"),
                "knowledge_graph": str(self.vault_dir / "02_Knowledge_Graph.md")}

    def backup_database(self, manual: bool = False) -> Path:
        now = datetime.now().astimezone()
        suffix = now.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8] if manual else now.strftime("%Y%m%d")
        target = self.backups_dir / f"agent_memory_{suffix}.db"
        if target.exists() and not manual:
            return target
        temporary = self.backups_dir / f".snapshot-{uuid.uuid4().hex}.db"
        try:
            with self._lock, self._connection() as source:
                destination = None
                try:
                    with sqlite3.connect(temporary, timeout=30) as destination:
                        destination.row_factory = sqlite3.Row
                        source.backup(destination)
                finally:
                    if destination is not None:
                        destination.close()
            # Windows rename is atomic and refuses to overwrite an existing target.
            # POSIX rename overwrites, so use no-clobber hard-link publication there.
            try:
                if os.name == "nt":
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
            except FileExistsError:
                if manual:
                    raise
        finally:
            temporary.unlink(missing_ok=True)
        return target.resolve()

    def trigger_manual_backup_and_export(self) -> dict[str, Any]:
        backup = self.backup_database(manual=True)
        self.sync_to_markdown_vault()
        return {"status": "BACKUP_CREATED", "database_path": str(self.db_path),
                "backup_path": str(backup), "vault_paths": self.vault_paths()}

    @staticmethod
    def _graph_data(connection: sqlite3.Connection, domain: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nodes = [dict(row) for row in connection.execute("SELECT * FROM knowledge_nodes ORDER BY domain,name")
                 if domain is None or canonical(row["domain"]) == canonical(domain)]
        ids = {node["id"] for node in nodes}
        edges = [dict(row) for row in connection.execute("SELECT * FROM knowledge_edges ORDER BY source_id,target_id,relation")
                 if row["source_id"] in ids and row["target_id"] in ids]
        return nodes, edges

    @staticmethod
    def _render_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
        identifiers = {node["id"]: "n" + hashlib.sha256(node["id"].encode("utf-8")).hexdigest() for node in nodes}
        graph = ["```mermaid", "graph TD"]
        if not nodes:
            graph.append('  empty["暂无知识节点"]')
        for node in nodes:
            label = mermaid_label(f"{node['name']} · {node['status']} · {node['mastery_score']}%")
            graph.append(f'  {identifiers[node["id"]]}["{label}"]:::{node["status"].lower()}')
        for edge in edges:
            graph.append(f'  {identifiers[edge["source_id"]]} -->|{edge["relation"]}| {identifiers[edge["target_id"]]}')
        graph += ["  " + definition for definition in CLASS_DEFS]
        graph += ["```", "", "```mermaid", "mindmap", '  root(("知识图谱"))']
        domains = sorted({node["domain"] for node in nodes})
        if not domains:
            graph.append('    empty["暂无知识节点"]')
        for index, domain in enumerate(domains):
            graph.append(f'    domain{index}["{mermaid_label(domain)}"]')
            for node in nodes:
                if node["domain"] == domain:
                    label = mermaid_label(f"{node['name']} · {node['status']} · {node['mastery_score']}%")
                    graph.append(f'      {identifiers[node["id"]]}["{label}"]')
        graph.append("```")
        return "\n".join(graph)

    def generate_mermaid_graph(self, domain: str | None = None) -> str:
        request = OverviewInput(domain=domain)
        with self._connection() as connection:
            connection.execute("BEGIN")
            return self._render_graph(*self._graph_data(connection, request.domain))

    def sync_to_markdown_vault(self) -> dict[str, str]:
        with self._lock, self._connection() as connection:
            # Serialize export with writers across processes, preventing stale snapshots.
            connection.execute("BEGIN IMMEDIATE")
            tasks = self._tasks(connection)
            nodes, edges = self._graph_data(connection, None)
            revision = datetime.now().astimezone().isoformat(timespec="seconds")
            dashboard = ["# 工作任务仪表盘", "", f"更新时间：{revision}", ""]
            for status in ("DOING", "TODO", "BLOCKED", "DONE", "DROPPED"):
                dashboard += [f"## {status}", ""]
                selected = [task for task in tasks if task["status"] == status]
                if not selected:
                    dashboard += ["暂无任务。", ""]
                for task in selected:
                    dashboard += [
                        f"### #{task['id']} [{task['task_type']}] {md(task['title'])}", "",
                        f"- 优先级：{task['priority']} · 预计：{task['estimated_minutes']} 分钟",
                        f"- 前置任务：{'#' + str(task['prerequisite_task_id']) if task['prerequisite_task_id'] else '无'}",
                        f"- 可开始：{'是' if task['ready_to_start'] else '否'}",
                        f"- 验收标准：{md(task['definition_of_done'])}",
                        f"- 关联概念：{', '.join(md(name) for name in task['linked_concepts']) or '无'}",
                        f"- 备注：{md(task['notes']) or '无'}", "",
                    ]
            knowledge = ["# 知识图谱", "", f"更新时间：{revision}", "", self._render_graph(nodes, edges), "",
                         "关系图展示概念关系；思维导图按领域归类。绿色＝掌握，黄色＝学习中，灰色＝愿望清单。", ""]
            for domain in sorted({node["domain"] for node in nodes}):
                knowledge += [f"## {md(domain)}", "", "| 概念 | 领域 | 状态 | 掌握度 | 摘要 |",
                              "| --- | --- | --- | ---: | --- |"]
                for node in nodes:
                    if node["domain"] == domain:
                        knowledge.append("| " + " | ".join(md(node[key]) for key in
                                         ("name", "domain", "status", "mastery_score", "summary")) + " |")
                knowledge.append("")
            paths = self.vault_paths()
            # Each file is atomically replaced; interrupted multi-file exports heal on startup.
            for key, lines in (("tasks", dashboard), ("knowledge_graph", knowledge)):
                self._atomic_text(Path(paths[key]), "\n".join(lines) + "\n")
            return paths

    @staticmethod
    def _atomic_text(target: Path, content: str) -> None:
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def read_vault(self, key: str) -> str:
        if key not in self.vault_paths():
            raise WorkspaceError("UNKNOWN_RESOURCE", "Unknown vault resource")
        self.sync_to_markdown_vault()
        return Path(self.vault_paths()[key]).read_text(encoding="utf-8")
