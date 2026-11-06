"""Exercise the real MCP wire protocol in an isolated local workspace."""

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import AnyUrl


PROJECT = Path(__file__).resolve().parents[1]


class StdioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_tools_resources_prompt_and_local_anchor(self):
        (PROJECT / "local_data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT / "local_data") as directory:
            root = Path(directory)
            for filename in ("models.py", "db_manager.py", "server.py", "learning.py"):
                shutil.copyfile(PROJECT / filename, root / filename)
            parameters = StdioServerParameters(
                command=sys.executable, args=[str(root / "server.py")],
                cwd=str(PROJECT.parent), env={"PYTHONUTF8": "1"},
            )
            async with asyncio.timeout(60):
                with (root / "stderr.log").open("w", encoding="utf-8") as error_log:
                    async with stdio_client(parameters, errlog=error_log) as (read, write):
                        async with ClientSession(read, write) as session:
                            initialized = await session.initialize()
                            self.assertIn("EVERY learning", initialized.instructions)
                            tools = await session.list_tools()
                            self.assertEqual({tool.name for tool in tools.tools}, {
                                "start_learning_session", "get_learning_followup", "set_learning_question",
                                "record_learning_answer", "confirm_learning_mastery", "control_learning_session",
                                "propose_goal_and_check_prerequisites", "submit_probe_answers_and_create_pipeline",
                                "update_task_status", "get_workspace_overview", "upsert_standalone_knowledge",
                                "trigger_manual_backup_and_export",
                                "edit_task", "split_task", "replan_tasks", "verify_knowledge_mastery",
                                "get_change_history", "validate_backup_and_rehearse",
                                "record_project_context", "get_proactive_work_brief", "begin_proactive_work",
                                "save_work_artifact", "finish_proactive_work", "review_proactive_work", "get_work_session",
                            })
                            pipeline_schema = next(tool.inputSchema for tool in tools.tools
                                                   if tool.name == "submit_probe_answers_and_create_pipeline")
                            self.assertIn("ProbeAnswer", json.dumps(pipeline_schema))
                            self.assertEqual(len((await session.list_resources()).resources), 3)
                            self.assertEqual(len((await session.list_prompts()).prompts), 2)
                            prompt = await session.get_prompt("mentor_system_prompt")
                            self.assertIn("25–90", str(prompt.messages))

                            async def call(name, arguments):
                                result = await session.call_tool(name, arguments)
                                self.assertFalse(result.isError, result)
                                return json.loads(result.content[0].text)

                            entry = next(t for t in tools.tools if t.name == "start_learning_session")
                            self.assertIn("knowledge Q&A", entry.description)
                            learning = await call("start_learning_session", {"concept": "Thevenin", "domain": "Circuits"})
                            learning_id = learning["session"]["id"]
                            for i, kind in enumerate(("EXPLAIN", "APPLY", "TRANSFER")):
                                question = await call("set_learning_question", {"session_id": learning_id,
                                    "request_id": f"wire-{i}", "kind": kind,
                                    "question": f"Independent exercise {i}: explain the circuit relationship",
                                    "rubric": ["Correct circuit relationship"]})
                                self.assertEqual(question["session"]["next_action"], "WAIT_FOR_ANSWER")
                                assessed = await call("record_learning_answer", {
                                    "question_id": question["session"]["pending_question"]["id"],
                                    "user_answer": "The port current is zero so the series resistance drops no voltage.",
                                    "criteria_met": [True], "feedback": "The circuit relationship is correct.", "assisted": False})
                            self.assertEqual(assessed["session"]["next_action"], "ASK_CONFIRMATION")
                            confirmed = await call("confirm_learning_mastery", {"session_id": learning_id,
                                "user_confirmed": True, "user_confirmation": "These are my independent answers; confirmed."})
                            self.assertEqual(confirmed["session"]["next_action"], "DONE")
                            pending = await call("get_learning_followup", {"domain": "Circuits"})
                            self.assertEqual(pending["sessions"], [])

                            proposal = await call("propose_goal_and_check_prerequisites", {
                                "goal_title": "实现 CDF 原型", "domain": "Vision", "required_concepts": ["CDF"]})
                            self.assertEqual(proposal["status"], "PROBE_REQUIRED")
                            arguments = {
                                "goal_title": "实现 CDF 原型", "domain": "Vision",
                                "probe_answers": [{"concept_name": "CDF", "domain": "Vision", "choice": "B",
                                                   "summary": "累积分布函数给出不超过某值的概率"}],
                                "tasks_pipeline": [{"title": "实现 CDF 原型", "task_type": "PROJECT",
                                                    "definition_of_done": "验证归一化与单调性", "linked_concepts": ["CDF"]}],
                                "edges": [],
                            }
                            pipeline = await call("submit_probe_answers_and_create_pipeline", arguments)
                            self.assertEqual(pipeline["status"], "PIPELINE_CREATED")
                            tasks = pipeline["tasks"]
                            edited = await call("edit_task", {"task_id": tasks[0]["id"], "notes": "Split the derivation", "priority": "P1"})
                            self.assertEqual(edited["status"], "TASK_EDITED")
                            split = await call("split_task", {"task_id": tasks[0]["id"], "steps": [
                                {"title": "Define CDF", "task_type": "STUDY", "definition_of_done": "Explain cumulative mass", "linked_concepts": ["CDF"]},
                                {"title": "Derive CDF properties", "task_type": "STUDY", "definition_of_done": "Check monotonicity", "linked_concepts": ["CDF"]}]})
                            self.assertEqual(split["status"], "TASK_SPLIT")
                            task_ids = [task["id"] for task in split["tasks"]]
                            replanned = await call("replan_tasks", {"goal_title": "实现 CDF 原型", "domain": "Vision", "task_ids": task_ids})
                            self.assertEqual(replanned["status"], "PLAN_UPDATED")
                            tasks = replanned["tasks"]
                            context = await call("record_project_context", {
                                "goal_title": "实现 CDF 原型", "domain": "Vision",
                                "project_summary": "准备一个 CDF 教学原型", "recent_progress": "基础探测已完成",
                                "sources": [{"reference": "requirements.md", "excerpt": "验证 CDF 的归一化与单调性。"}],
                            })
                            self.assertEqual(context["status"], "CONTEXT_RECORDED")
                            brief = await call("get_proactive_work_brief", {"domain": "Vision"})
                            self.assertEqual(brief["candidates"][0]["task_id"], tasks[0]["id"])
                            work = await call("begin_proactive_work", {"task_id": tasks[0]["id"], "request_id": "stdio-test"})
                            work_id = work["session"]["id"]
                            artifact = await call("save_work_artifact", {"session_id": work_id, "relative_path": "notes/cdf.md",
                                                                      "content": "# CDF\n\nF(x) = P(X <= x).\n"})
                            self.assertTrue(Path(artifact["path"]).is_file())
                            finished = await call("finish_proactive_work", {"session_id": work_id, "summary": "CDF 定义说明草稿",
                                                                          "checks": [{"name": "Content inspection", "outcome": "PASSED", "evidence": "Artifact read back and definition checked"}]})
                            self.assertEqual(finished["status"], "READY_FOR_REVIEW")
                            inspected = await call("get_work_session", {"session_id": work_id, "include_artifact_content": True})
                            self.assertIn("F(x)", inspected["session"]["artifacts"][0]["content"])
                            reviewed = await call("review_proactive_work", {"session_id": work_id, "decision": "ACCEPT"})
                            self.assertEqual(reviewed["status"], "ACCEPTED")
                            proactive_prompt = await session.get_prompt("proactive_work_prompt")
                            self.assertIn("save_work_artifact", str(proactive_prompt.messages))
                            blocked = await call("update_task_status", {"task_id": tasks[2]["id"], "new_status": "DONE"})
                            self.assertEqual(blocked["code"], "DEPENDENCY_NOT_DONE")
                            invalid = await call("update_task_status", {"task_id": tasks[0]["id"], "new_status": "INVALID"})
                            self.assertEqual(invalid["code"], "VALIDATION_ERROR")
                            for task in tasks:
                                update = await call("update_task_status", {"task_id": task["id"], "new_status": "DONE"})
                                self.assertEqual(update["status"], "TASK_UPDATED")
                            replay = await call("submit_probe_answers_and_create_pipeline", arguments)
                            self.assertTrue(replay["idempotent_replay"])
                            overview = await call("get_workspace_overview", {"domain": "Vision", "status_filter": "DONE"})
                            self.assertEqual(len(overview["tasks"]), 4)
                            self.assertEqual(overview["knowledge_statistics"]["MASTERED"], 0)
                            verified = await call("verify_knowledge_mastery", {
                                "name": "CDF", "domain": "Vision", "task_id": tasks[-1]["id"],
                                "implementation_evidence": "Implemented the CDF and checked monotonicity and normalization",
                                "explanation_evidence": "Explained cumulative mass and boundary cases", "user_confirmed": True})
                            self.assertEqual(verified["status"], "MASTERY_RECORDED")
                            knowledge = await call("upsert_standalone_knowledge", {
                                "name": "概率", "domain": "Vision", "prerequisite_concept": "计数", "parent_concept": "数学"})
                            self.assertEqual(knowledge["status"], "KNOWLEDGE_UPDATED")
                            exported = await call("trigger_manual_backup_and_export", {})
                            self.assertEqual(Path(exported["database_path"]), root / "local_data" / "agent_memory.db")
                            self.assertTrue(Path(exported["backup_path"]).is_file())
                            checked = await call("validate_backup_and_rehearse", {"backup_name": Path(exported["backup_path"]).name})
                            self.assertEqual(checked["status"], "BACKUP_VALID", checked)
                            history = await call("get_change_history", {"entity_type": "knowledge_nodes"})
                            self.assertTrue(history["events"])
                            for uri, expected in (("vault://dashboard/tasks", "# 工作任务仪表盘"),
                                                  ("vault://dashboard/knowledge_graph", "mindmap"),
                                                  ("vault://dashboard/active_work", "notes/cdf.md")):
                                resource = await session.read_resource(AnyUrl(uri))
                                self.assertIn(expected, resource.contents[0].text)


if __name__ == "__main__":
    unittest.main()
