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
            for filename in ("models.py", "db_manager.py", "server.py"):
                shutil.copyfile(PROJECT / filename, root / filename)
            parameters = StdioServerParameters(
                command=sys.executable, args=[str(root / "server.py")],
                cwd=str(PROJECT.parent), env={"PYTHONUTF8": "1"},
            )
            async with asyncio.timeout(60):
                with (root / "stderr.log").open("w", encoding="utf-8") as error_log:
                    async with stdio_client(parameters, errlog=error_log) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertEqual({tool.name for tool in tools.tools}, {
                                "propose_goal_and_check_prerequisites", "submit_probe_answers_and_create_pipeline",
                                "update_task_status", "get_workspace_overview", "upsert_standalone_knowledge",
                                "trigger_manual_backup_and_export",
                            })
                            pipeline_schema = next(tool.inputSchema for tool in tools.tools
                                                   if tool.name == "submit_probe_answers_and_create_pipeline")
                            self.assertIn("ProbeAnswer", json.dumps(pipeline_schema))
                            self.assertEqual(len((await session.list_resources()).resources), 2)
                            self.assertEqual(len((await session.list_prompts()).prompts), 1)
                            prompt = await session.get_prompt("mentor_system_prompt")
                            self.assertIn("25–90", str(prompt.messages))

                            async def call(name, arguments):
                                result = await session.call_tool(name, arguments)
                                self.assertFalse(result.isError, result)
                                return json.loads(result.content[0].text)

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
                            self.assertEqual(len(overview["tasks"]), 3)
                            self.assertEqual(overview["knowledge_statistics"]["MASTERED"], 1)
                            knowledge = await call("upsert_standalone_knowledge", {
                                "name": "概率", "domain": "Vision", "prerequisite_concept": "计数", "parent_concept": "数学"})
                            self.assertEqual(knowledge["status"], "KNOWLEDGE_UPDATED")
                            exported = await call("trigger_manual_backup_and_export", {})
                            self.assertEqual(Path(exported["database_path"]), root / "local_data" / "agent_memory.db")
                            self.assertTrue(Path(exported["backup_path"]).is_file())
                            for uri, expected in (("vault://dashboard/tasks", "# 工作任务仪表盘"),
                                                  ("vault://dashboard/knowledge_graph", "mindmap")):
                                resource = await session.read_resource(AnyUrl(uri))
                                self.assertIn(expected, resource.contents[0].text)


if __name__ == "__main__":
    unittest.main()
