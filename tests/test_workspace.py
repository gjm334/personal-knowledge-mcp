"""Behavioral tests: persistent gates, atomic changes, dependency invariants and recovery."""

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from db_manager import DatabaseManager, VaultSyncError, WorkspaceError, concept_id
from models import PipelineTaskInput, ProbeAnswer


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / "local_data").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT / "local_data")
        self.addCleanup(self.temporary.cleanup)
        self.db = DatabaseManager(Path(self.temporary.name))

    def propose(self, names=None, title="Build a prototype", domain="Vision"):
        return self.db.propose_goal_and_check_prerequisites(
            goal_title=title, domain=domain, required_concepts=names or ["Histogram"])

    def payload(self, choice="A", title="Build a prototype", domain="Vision", name="Histogram"):
        return {
            "goal_title": title, "domain": domain,
            "probe_answers": [{"concept_name": name, "domain": domain, "choice": choice,
                               "summary": "A frequency distribution of observations"}],
            "tasks_pipeline": [{"title": "Implement prototype", "task_type": "PROJECT",
                                "definition_of_done": "Run on two inputs and compare outputs",
                                "linked_concepts": [name]}], "edges": [],
        }

    def schedule(self, choice="A"):
        self.propose()
        return self.db.submit_probe_answers_and_create_pipeline(**self.payload(choice))

    def assert_code(self, code, function, **arguments):
        with self.assertRaises(WorkspaceError) as captured:
            function(**arguments)
        self.assertEqual(captured.exception.code, code)

    def test_startup_paths_daily_backup_and_empty_vault(self):
        backup = self.db.backup_database()
        self.assertTrue(backup.is_absolute())
        original = backup.read_bytes()
        self.db.upsert_standalone_knowledge(name="New", domain="Notes")
        DatabaseManager(self.db.data_dir)
        self.assertEqual(backup.read_bytes(), original)
        for path in self.db.vault_paths().values():
            self.assertTrue(Path(path).is_file())

    def test_probe_has_real_numbered_options_and_creates_no_tasks(self):
        result = self.propose(["Histogram", "CDF"])
        self.assertEqual(result["status"], "PROBE_REQUIRED")
        self.assertEqual([question["number"] for question in result["questions"]], [1, 2])
        self.assertEqual([option["score"] for option in result["questions"][0]["options"]], [0, 50, 100, 30])
        self.assertEqual(self.db.get_workspace_overview()["tasks"], [])
        self.assertIn("Histogram", Path(self.db.vault_paths()["knowledge_graph"]).read_text(encoding="utf-8"))

    def test_proposal_is_required_and_persists_across_restart(self):
        self.assert_code("PROPOSAL_REQUIRED", self.db.submit_probe_answers_and_create_pipeline, **self.payload())
        self.propose()
        self.db = DatabaseManager(self.db.data_dir)
        payload = self.payload()
        payload["probe_answers"] = []
        self.assert_code("PROBE_REQUIRED", self.db.submit_probe_answers_and_create_pipeline, **payload)
        self.assertEqual(len(self.db.get_workspace_overview()["tasks"]), 0)

    def test_a_and_b_automatically_insert_study_then_practice(self):
        for choice in ("A", "B"):
            with self.subTest(choice=choice):
                title = "Goal " + choice
                self.propose(title=title)
                result = self.db.submit_probe_answers_and_create_pipeline(**self.payload(choice, title=title))
                tasks = result["tasks"]
                self.assertEqual([t["task_type"] for t in tasks], ["STUDY", "PRACTICE", "PROJECT"])
                self.assertIsNone(tasks[0]["prerequisite_task_id"])
                self.assertEqual(tasks[1]["prerequisite_task_id"], tasks[0]["id"])
                self.assertEqual(tasks[2]["prerequisite_task_id"], tasks[1]["id"])
                self.assertTrue(all(t["linked_concepts"] == ["Histogram"] for t in tasks))

    def test_complete_host_preparation_is_preserved(self):
        self.propose()
        payload = self.payload("B")
        payload["tasks_pipeline"] = [
            {"title": "Derive CDF", "task_type": "STUDY", "definition_of_done": "Hand calculate CDF",
             "linked_concepts": ["Histogram"]},
            {"title": "Test CDF", "task_type": "PRACTICE", "definition_of_done": "Compare two test cases",
             "linked_concepts": ["Histogram"]},
            *payload["tasks_pipeline"],
        ]
        result = self.db.submit_probe_answers_and_create_pipeline(**payload)
        self.assertEqual(len(result["tasks"]), 3)
        self.assertEqual(result["tasks"][0]["title"], "Derive CDF")

    def test_c_and_d_map_correctly_without_forced_theory(self):
        for choice, status, score in (("C", "MASTERED", 100), ("D", "LEARNING", 30)):
            with self.subTest(choice=choice):
                title = "Goal " + choice
                self.propose(title=title)
                result = self.db.submit_probe_answers_and_create_pipeline(**self.payload(choice, title=title))
                self.assertEqual(len(result["tasks"]), 1)
                node = self.db.get_workspace_overview()["knowledge_nodes"][0]
                self.assertEqual((node["status"], node["mastery_score"]), (status, score))

    def test_mastered_path_still_needs_proposal(self):
        self.db.upsert_standalone_knowledge(name="Histogram", domain="Vision", status="MASTERED")
        self.assertEqual(self.propose()["status"], "READY_TO_SCHEDULE")
        payload = self.payload()
        payload["probe_answers"] = []
        self.assertEqual(len(self.db.submit_probe_answers_and_create_pipeline(**payload)["tasks"]), 1)

    def test_all_missing_answers_are_required(self):
        self.propose(["Histogram", "CDF"])
        self.assert_code("PROBE_REQUIRED", self.db.submit_probe_answers_and_create_pipeline, **self.payload())

    def test_mastery_downgrade_requires_a_new_probe_on_existing_proposal(self):
        self.db.upsert_standalone_knowledge(name="Histogram", domain="Vision", status="MASTERED")
        self.assertEqual(self.propose()["status"], "READY_TO_SCHEDULE")
        self.db.upsert_standalone_knowledge(name="Histogram", domain="Vision", status="LEARNING")
        reproposed = self.propose()
        self.assertEqual(reproposed["status"], "PROBE_REQUIRED")
        self.assertEqual(reproposed["questions"][0]["concept_name"], "Histogram")

    def test_reproposal_rechecks_knowledge_mastered_since_original_probe(self):
        self.assertEqual(self.propose()["status"], "PROBE_REQUIRED")
        self.db.upsert_standalone_knowledge(name="Histogram", domain="Vision", status="MASTERED")
        self.assertEqual(self.propose()["status"], "READY_TO_SCHEDULE")
        payload = self.payload()
        payload["probe_answers"] = []
        self.assertEqual(len(self.db.submit_probe_answers_and_create_pipeline(**payload)["tasks"]), 1)

    def test_duplicate_and_foreign_answers_are_rejected(self):
        self.propose()
        payload = self.payload()
        payload["probe_answers"] *= 2
        self.assert_code("DUPLICATE_ANSWER", self.db.submit_probe_answers_and_create_pipeline, **payload)
        payload = self.payload(domain="Other")
        payload["domain"] = "Vision"
        self.assert_code("UNEXPECTED_ANSWER", self.db.submit_probe_answers_and_create_pipeline, **payload)

    def test_unprobed_extra_task_concepts_and_unlinked_project_are_blocked(self):
        self.propose()
        payload = self.payload()
        payload["tasks_pipeline"][0]["linked_concepts"].append("Hidden prerequisite")
        self.assert_code("UNGATED_CONCEPT", self.db.submit_probe_answers_and_create_pipeline, **payload)
        payload["tasks_pipeline"][0]["linked_concepts"] = []
        self.assert_code("PROJECT_LINKS_REQUIRED", self.db.submit_probe_answers_and_create_pipeline, **payload)

    def test_edges_and_node_changes_rollback_together(self):
        self.propose()
        payload = self.payload("C")
        payload["edges"] = [{"source_concept": "Histogram", "target_concept": "Missing", "relation": "APPLIES_TO"}]
        self.assert_code("UNKNOWN_EDGE_CONCEPT", self.db.submit_probe_answers_and_create_pipeline, **payload)
        overview = self.db.get_workspace_overview()
        self.assertEqual(overview["knowledge_nodes"][0]["status"], "WISHLIST")
        self.assertEqual(overview["tasks"], [])

    def test_dependencies_block_doing_done_and_sql_bypass(self):
        tasks = self.schedule()["tasks"]
        for status in ("DOING", "DONE"):
            self.assert_code("DEPENDENCY_NOT_DONE", self.db.update_task_status,
                             task_id=tasks[1]["id"], new_status=status)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db._connection() as connection:
                connection.execute("UPDATE tasks SET status='DONE' WHERE id=?", (tasks[1]["id"],))

    def test_completion_upgrades_knowledge_and_unlocks_next_task(self):
        tasks = self.schedule("B")["tasks"]
        result = self.db.update_task_status(task_id=tasks[0]["id"], new_status="DONE", notes="Derived example")
        self.assertEqual(result["updated_knowledge"][0]["mastery_score"], 60)
        self.assertEqual(result["newly_unlocked_tasks"][0]["id"], tasks[1]["id"])
        result = self.db.update_task_status(task_id=tasks[1]["id"], new_status="DONE")
        self.assertEqual(result["updated_knowledge"][0]["status"], "MASTERED")
        self.assertIn("MASTERED", Path(self.db.vault_paths()["knowledge_graph"]).read_text(encoding="utf-8"))
        self.db.update_task_status(task_id=tasks[2]["id"], new_status="DONE")
        self.assertEqual(self.db.get_workspace_overview(status_filter="DONE")["tasks"][-1]["status"], "DONE")

    def test_completed_dependencies_cannot_be_reopened_under_active_tasks(self):
        tasks = self.schedule()["tasks"]
        self.db.update_task_status(task_id=tasks[0]["id"], new_status="DONE")
        self.db.update_task_status(task_id=tasks[1]["id"], new_status="DOING")
        self.assert_code("ACTIVE_DEPENDENTS", self.db.update_task_status, task_id=tasks[0]["id"], new_status="TODO")

    def test_identical_retries_are_idempotent_and_changed_payload_is_rejected(self):
        first = self.schedule()
        second = self.db.submit_probe_answers_and_create_pipeline(**self.payload())
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual([t["id"] for t in first["tasks"]], [t["id"] for t in second["tasks"]])
        self.assertEqual(self.propose()["status"], "ALREADY_SCHEDULED")
        self.assert_code("ALREADY_SCHEDULED", self.db.submit_probe_answers_and_create_pipeline, **self.payload("C"))

    def test_parallel_retry_creates_only_one_schedule(self):
        self.propose()
        second_manager = DatabaseManager(self.db.data_dir)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda manager: manager.submit_probe_answers_and_create_pipeline(**self.payload()),
                                    [self.db, second_manager]))
        self.assertEqual(len(self.db.get_workspace_overview()["tasks"]), 3)
        self.assertEqual(sum(r["idempotent_replay"] for r in results), 1)

    def test_backup_is_valid_independent_and_unique(self):
        self.schedule()
        first = self.db.trigger_manual_backup_and_export()
        second = self.db.trigger_manual_backup_and_export()
        self.assertNotEqual(first["backup_path"], second["backup_path"])
        with sqlite3.connect(first["backup_path"]) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT count(*) FROM tasks").fetchone()[0], 3)
        connection.close()

    def test_mermaid_and_markdown_escape_adversarial_labels(self):
        name = 'C++ / 方法 (v2) [x] "quoted" <script>\n```mermaid'
        result = self.db.upsert_standalone_knowledge(name=name, domain="R&D / (Test)", summary="A | B <img> `code`")
        graph = result["mermaid"]
        self.assertEqual(graph.count("```mermaid"), 2)
        self.assertIn("graph TD", graph)
        self.assertIn("mindmap", graph)
        self.assertNotIn("<script>", graph)
        self.assertNotEqual(concept_id("C++"), concept_id("C#"))
        vault = Path(self.db.vault_paths()["knowledge_graph"]).read_text(encoding="utf-8")
        self.assertIn("&#124;", vault)
        self.assertNotIn("<img>", vault)

    def test_domain_filter_and_graph_endpoints(self):
        self.schedule()
        self.db.upsert_standalone_knowledge(name="SQL", domain="Storage")
        overview = self.db.get_workspace_overview(domain="VISION")
        self.assertEqual(len(overview["tasks"]), 3)
        self.assertEqual(overview["knowledge_statistics"]["total"], 1)
        self.assertEqual(self.db.get_workspace_overview(domain="Missing")["tasks"], [])
        self.assertNotIn("SQL", self.db.generate_mermaid_graph(domain="Vision"))

    def test_standalone_parent_prerequisite_and_cycles(self):
        self.db.upsert_standalone_knowledge(name="CDF", domain="Vision", parent_concept="Histogram", prerequisite_concept="Probability")
        self.assertIn("SUBTOPIC_OF", self.db.generate_mermaid_graph())
        self.assertIn("PREREQUISITE_FOR", self.db.generate_mermaid_graph())
        self.assert_code("KNOWLEDGE_CYCLE", self.db.upsert_standalone_knowledge,
                         name="Histogram", domain="Vision", parent_concept="CDF")

    def test_vault_failure_preserves_committed_data_and_can_recover(self):
        self.propose()
        with patch.object(self.db, "_atomic_text", side_effect=PermissionError("Locked file")):
            with self.assertLogs("db_manager", level="ERROR"):
                with self.assertRaises(VaultSyncError):
                    self.db.submit_probe_answers_and_create_pipeline(**self.payload())
        replay = self.db.submit_probe_answers_and_create_pipeline(**self.payload())
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(replay["tasks"]), 3)
        self.assertTrue(Path(self.db.trigger_manual_backup_and_export()["backup_path"]).exists())

    def test_strict_validation_rejects_coercion_bounds_and_unknown_fields(self):
        with self.assertRaises(ValidationError):
            ProbeAnswer(concept_name="X", domain="D", choice="a", summary="A definition")
        for minutes in (14, 181, "45", True):
            with self.subTest(minutes=minutes), self.assertRaises(ValidationError):
                PipelineTaskInput(title="Test", task_type="STUDY", estimated_minutes=minutes,
                                  definition_of_done="Evidence", linked_concepts=["X"])
        with self.assertRaises(ValidationError):
            self.db.update_task_status(task_id=1, new_status="INVALID")
        with self.assertRaises(ValidationError):
            self.db.upsert_standalone_knowledge(name=" ", domain="D", unexpected=True)

    def test_notes_are_preserved_and_dropped_dependencies_stay_blocking(self):
        tasks = self.schedule()["tasks"]
        self.db.update_task_status(task_id=tasks[0]["id"], new_status="DOING", notes="Working")
        result = self.db.update_task_status(task_id=tasks[0]["id"], new_status="DROPPED")
        self.assertEqual(result["task"]["notes"], "Working")
        self.assert_code("DEPENDENCY_NOT_DONE", self.db.update_task_status, task_id=tasks[1]["id"], new_status="DOING")
        self.assertIn("## DROPPED", self.db.read_vault("tasks"))


if __name__ == "__main__":
    unittest.main()
