"""Regression coverage for partial updates, prerequisites, evidence, replanning and recovery."""

import hashlib
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from db_manager import DatabaseManager, WorkspaceError, concept_id, SCHEMA


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "local_data")
        self.addCleanup(self.temporary.cleanup)
        self.db = DatabaseManager(Path(self.temporary.name))

    def error(self, code, function, **kwargs):
        with self.assertRaises(WorkspaceError) as caught:
            function(**kwargs)
        self.assertEqual(caught.exception.code, code)

    @staticmethod
    def step(title, kind="STUDY", concepts=None):
        return {"title": title, "task_type": kind, "definition_of_done": "Check normal and boundary inputs",
                "linked_concepts": concepts or ["Core"]}

    def schedule(self, choice="B"):
        self.db.propose_goal_and_check_prerequisites(goal_title="Build", domain="Math", required_concepts=["Core"])
        return self.db.submit_probe_answers_and_create_pipeline(
            goal_title="Build", domain="Math", probe_answers=[{"concept_name": "Core", "domain": "Math", "choice": choice,
                                                                    "summary": "A core operation"}],
            tasks_pipeline=[self.step("Build", "PROJECT")], edges=[])["tasks"]

    def node(self, name="Core"):
        return next(n for n in self.db.get_workspace_overview()["knowledge_nodes"] if n["name"] == name)

    def test_note_only_status_only_and_explicit_clear_preserve_other_fields(self):
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", status="MASTERED", summary="Original")
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", summary="New note")
        self.assertEqual((self.node()["status"], self.node()["mastery_score"]), ("MASTERED", 100))
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", status="LEARNING")
        self.assertEqual(self.node()["summary"], "New note")
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", summary="")
        self.assertEqual(self.node()["summary"], "")

    def test_note_update_preserves_evidence_score_after_practice(self):
        tasks = self.schedule()
        for task in tasks[:2]:
            self.db.update_task_status(task_id=task["id"], new_status="DONE")
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", summary="Recorded a limitation")
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", status="LEARNING")
        self.assertEqual(self.node()["mastery_score"], 80)

    def test_recursive_diamond_prerequisites_are_probed_once_in_topological_order(self):
        for name, parent in (("Base", None), ("Left", "Base"), ("Right", "Base"), ("Top", "Left")):
            self.db.upsert_standalone_knowledge(name=name, domain="Math", prerequisite_concept=parent)
        with self.db._write() as connection:
            self.db._insert_edge(connection, "Right", "Top", "PREREQUISITE_FOR")
        proposal = self.db.propose_goal_and_check_prerequisites(goal_title="Graph", domain="Math", required_concepts=["Top"])
        names = [q["concept_name"] for q in proposal["questions"]]
        self.assertEqual(len(names), 4)
        self.assertEqual(names[0], "Base")
        self.assertEqual(names[-1], "Top")
        answers = [{"concept_name": name, "domain": "Math", "choice": "B", "summary": "A prerequisite"} for name in reversed(names)]
        tasks = self.db.submit_probe_answers_and_create_pipeline(goal_title="Graph", domain="Math", probe_answers=answers,
                    tasks_pipeline=[self.step("Graph", "PROJECT", names)], edges=[])["tasks"]
        self.assertEqual([t["linked_concepts"][0] for t in tasks[:-1:2]], names)

    def test_recursive_prerequisites_keep_their_original_domains(self):
        self.db.upsert_standalone_knowledge(name="Base", domain="Foundations")
        self.db.upsert_standalone_knowledge(name="Core", domain="Math")
        with self.db._write() as connection:
            self.db._insert_edge(connection, "Base", "Core", "PREREQUISITE_FOR")
        proposal = self.db.propose_goal_and_check_prerequisites(goal_title="Cross-domain", domain="Math", required_concepts=["Core"])
        self.assertEqual([q["domain"] for q in proposal["questions"]], ["Foundations", "Math"])
        answers = [{"concept_name": q["concept_name"], "domain": q["domain"], "choice": "D", "summary": "Use via an interface"} for q in proposal["questions"]]
        self.db.submit_probe_answers_and_create_pipeline(goal_title="Cross-domain", domain="Math", probe_answers=answers,
                    tasks_pipeline=[self.step("Build", "PROJECT", ["Base", "Core"])], edges=[])

    def test_new_prerequisite_requires_reproposal_and_a_real_answer(self):
        self.db.propose_goal_and_check_prerequisites(goal_title="Build", domain="Math", required_concepts=["Core"])
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", prerequisite_concept="Base")
        arguments = dict(goal_title="Build", domain="Math", probe_answers=[{"concept_name": "Core", "domain": "Math", "choice": "C", "summary": "Core"}],
                         tasks_pipeline=[self.step("Build", "PROJECT")], edges=[])
        self.error("PREREQUISITES_CHANGED", self.db.submit_probe_answers_and_create_pipeline, **arguments)
        proposal = self.db.propose_goal_and_check_prerequisites(goal_title="Build", domain="Math", required_concepts=["Core"])
        self.assertEqual([q["concept_name"] for q in proposal["questions"]], ["Base", "Core"])

    def test_black_box_completion_remains_use_and_explicit_assessment_is_required(self):
        task = self.schedule("D")[0]
        self.db.update_task_status(task_id=task["id"], new_status="DONE", notes="Called the API on two inputs")
        node = self.node()
        self.assertEqual((node["status"], node["mastery_score"], node["evidence"][0]["level"]), ("LEARNING", 50, "USE"))
        arguments = dict(name="Core", domain="Math", task_id=task["id"], implementation_evidence="Implemented the operation independently",
                         explanation_evidence="Explained assumptions, derivation and edge cases", user_confirmed=False)
        self.error("USER_CONFIRMATION_REQUIRED", self.db.verify_knowledge_mastery, **arguments)
        arguments["user_confirmed"] = True
        self.db.verify_knowledge_mastery(**arguments)
        self.db.verify_knowledge_mastery(**arguments)
        self.assertEqual(self.node()["status"], "MASTERED")
        self.assertEqual(len(self.node()["evidence"]), 2)

    def test_assessment_rejects_unfinished_unlinked_or_study_only_tasks(self):
        tasks = self.schedule()
        arguments = dict(name="Core", domain="Math", task_id=tasks[0]["id"], implementation_evidence="Example",
                         explanation_evidence="Explained example", user_confirmed=True)
        self.error("ASSESSMENT_TASK_REQUIRED", self.db.verify_knowledge_mastery, **arguments)
        self.db.update_task_status(task_id=tasks[0]["id"], new_status="DONE")
        self.error("ASSESSMENT_TASK_REQUIRED", self.db.verify_knowledge_mastery, **arguments)

    def test_completion_retries_do_not_duplicate_evidence(self):
        tasks = self.schedule()
        for task in tasks:
            self.db.update_task_status(task_id=task["id"], new_status="DONE")
            self.db.update_task_status(task_id=task["id"], new_status="DONE")
        self.assertEqual(self.node()["mastery_score"], 85)
        self.assertEqual(len(self.node()["evidence"]), 3)

    def test_edit_preserves_fields_clears_notes_and_invalidates_drafts(self):
        tasks = self.schedule()
        self.db.record_project_context(goal_title="Build", domain="Math", project_summary="Test editing")
        work = self.db.begin_proactive_work(task_id=tasks[0]["id"], request_id="edit")
        edited = self.db.edit_task(task_id=tasks[0]["id"], notes="Keep detail", priority="P0")["task"]
        self.assertEqual(edited["definition_of_done"], tasks[0]["definition_of_done"])
        self.assertEqual(self.db.get_work_session(session_id=work["session"]["id"])["session"]["status"], "STALE")
        self.assertEqual(self.db.edit_task(task_id=tasks[0]["id"], notes="")["task"]["notes"], "")

    def test_split_reconnects_successor_and_retains_original_task_identity(self):
        tasks = self.schedule()
        result = self.db.split_task(task_id=tasks[0]["id"], steps=[self.step("Derive"), self.step("Check derivation")])["tasks"]
        self.assertEqual([t["title"] for t in result[:2]], ["Derive", "Check derivation"])
        self.assertEqual(result[0]["id"], tasks[0]["id"])
        self.assertEqual(result[2]["id"], tasks[1]["id"])
        for previous, task in zip(result, result[1:]):
            self.assertEqual(task["prerequisite_task_id"], previous["id"])
        self.error("DEPENDENCY_NOT_DONE", self.db.update_task_status, task_id=tasks[1]["id"], new_status="DONE")

    def test_split_validation_rolls_back_tasks_links_and_history(self):
        tasks = self.schedule()
        before = self.db.get_change_history(limit=200)["events"]
        self.error("PIPELINE_ORDER", self.db.split_task, task_id=tasks[0]["id"],
                   steps=[self.step("Invalid project", "PROJECT"), self.step("Late study")])
        self.assertEqual(self.db.get_change_history(limit=200)["events"], before)
        self.assertEqual(len(self.db.get_workspace_overview()["tasks"]), len(tasks))

    def test_replan_allows_valid_reordering_and_blocks_learning_order_violations(self):
        tasks = self.schedule()
        expanded = self.db.split_task(task_id=tasks[0]["id"], steps=[self.step("First"), self.step("Second")])["tasks"]
        ids = [t["id"] for t in expanded]
        ids[:2] = reversed(ids[:2])
        planned = self.db.replan_tasks(goal_title="Build", domain="Math", task_ids=ids)["tasks"]
        self.assertEqual([t["id"] for t in planned], ids)
        bad = [ids[2], ids[0], ids[1], ids[3]]
        self.error("LEARNING_ORDER", self.db.replan_tasks, goal_title="Build", domain="Math", task_ids=bad)
        self.db.update_task_status(task_id=ids[0], new_status="DONE")
        self.error("TASK_LOCKED", self.db.replan_tasks, goal_title="Build", domain="Math", task_ids=[ids[1], ids[0], *ids[2:]])

    def test_completed_tasks_cannot_change_scope(self):
        tasks = self.schedule()
        self.db.update_task_status(task_id=tasks[0]["id"], new_status="DONE")
        self.error("TASK_LOCKED", self.db.edit_task, task_id=tasks[0]["id"], definition_of_done="Changed criteria")
        self.error("TASK_LOCKED", self.db.split_task, task_id=tasks[0]["id"], steps=[self.step("A"), self.step("B")])

    def test_history_captures_before_after_paginates_and_is_append_only(self):
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", summary="One")
        self.db.upsert_standalone_knowledge(name="Core", domain="Math", summary="Two")
        events, cursor = [], 0
        while True:
            result = self.db.get_change_history(after_id=cursor, limit=1, entity_type="knowledge_nodes", entity_key=concept_id("Core"))
            events.extend(result["events"])
            cursor = result["next_after_id"]
            if not result["has_more"]:
                break
        self.assertEqual(events[-1]["before"]["summary"], "One")
        self.assertEqual(events[-1]["after"]["summary"], "Two")
        with self.assertRaises(sqlite3.IntegrityError), self.db._connection() as connection:
            connection.execute("DELETE FROM change_events")

    def test_daily_backup_rolls_over_without_restart_and_preserves_previous_day(self):
        day = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with patch.object(self.db, "_backup_now", return_value=day):
            first = self.db.backup_database()
            digest = hashlib.sha256(first.read_bytes()).hexdigest()
        with patch.object(self.db, "_backup_now", return_value=day + timedelta(days=1)):
            self.db.upsert_standalone_knowledge(name="Core", domain="Math", summary="Next day")
            self.assertTrue((self.db.backups_dir / "agent_memory_20300102.db").is_file())
        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest)

    def test_idle_maintenance_performs_backups_and_stops_cleanly(self):
        reached = threading.Event()
        real_event = threading.Event()
        class QuickStop:
            def clear(inner):
                real_event.clear()
            def wait(inner, timeout):
                return real_event.wait(0.01)
            def set(inner):
                real_event.set()
        self.db._maintenance_stop = QuickStop()
        with patch.object(self.db, "backup_database", side_effect=lambda: reached.set()):
            self.db.start_maintenance()
            self.assertTrue(reached.wait(2))
            self.db.stop_maintenance()
        self.assertFalse(self.db._maintenance_thread.is_alive())

    def test_corrupt_daily_snapshot_is_preserved_and_replaced(self):
        target = self.db.backup_database()
        target.write_bytes(b"not a database")
        repaired = self.db.backup_database()
        self.assertEqual(repaired, target)
        self.assertEqual(self.db._check_snapshot(repaired)["integrity_check"], "ok")
        self.assertEqual(len(list(self.db.backups_dir.glob("*.corrupt-*"))), 1)

    def test_restore_rehearsal_verifies_drafts_without_modifying_active_data(self):
        tasks = self.schedule()
        self.db.record_project_context(goal_title="Build", domain="Math", project_summary="Backup test")
        session = self.db.begin_proactive_work(task_id=tasks[0]["id"], request_id="recovery")["session"]["id"]
        self.db.save_work_artifact(session_id=session, relative_path="notes.md", content="# Actual draft\n")
        backup = self.db.backup_database(manual=True)
        active_digest = hashlib.sha256(self.db.db_path.read_bytes()).hexdigest()
        backup_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        report = self.db.validate_backup_and_rehearse(backup.name)
        self.assertEqual(report["status"], "BACKUP_VALID", report)
        self.assertTrue(report["vault_files_verified"])
        self.assertEqual(hashlib.sha256(self.db.db_path.read_bytes()).hexdigest(), active_digest)
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), backup_digest)
        self.assertEqual(list(self.db.data_dir.glob("restore-check-*")), [])

    def test_restore_check_rejects_corruption_wrong_schema_and_outside_paths(self):
        corrupt = self.db.backups_dir / "agent_memory_20000101.db"
        corrupt.write_bytes(b"bad backup")
        self.assertEqual(self.db.validate_backup_and_rehearse(corrupt.name)["status"], "BACKUP_INVALID")
        corrupt.unlink()
        with sqlite3.connect(corrupt) as connection:
            connection.execute("PRAGMA user_version=3")
        connection.close()
        self.assertEqual(self.db.validate_backup_and_rehearse(corrupt.name)["status"], "BACKUP_INVALID")
        self.error("BACKUP_PATH", self.db.validate_backup_and_rehearse, backup_name="../agent_memory_20000101.db")

    def test_v2_migration_preserves_work_sources_drafts_and_existing_goal(self):
        tasks = self.schedule()
        self.db.record_project_context(goal_title="Build", domain="Math", project_summary="Legacy source context")
        session = self.db.begin_proactive_work(task_id=tasks[0]["id"], request_id="legacy")["session"]["id"]
        self.db.save_work_artifact(session_id=session, relative_path="legacy.md", content="Keep this exact draft\n")
        legacy = Path(self.temporary.name) / "legacy"
        legacy.mkdir()
        with self.db._connection() as source:
            destination = sqlite3.connect(legacy / "agent_memory.db")
            try:
                source.backup(destination)
                for (name,) in destination.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'audit_%' OR name LIKE 'history_no_%')").fetchall():
                    destination.execute(f'DROP TRIGGER "{name}"')
                for table in ("knowledge_evidence", "goal_root_concepts", "change_events"):
                    destination.execute(f"DROP TABLE {table}")
                destination.execute("PRAGMA user_version=2")
                destination.commit()
            finally:
                destination.close()
        migrated = DatabaseManager(legacy)
        retained = migrated.get_work_session(session_id=session, include_artifact_content=True)["session"]
        self.assertEqual(retained["artifacts"][0]["content"], "Keep this exact draft\n")
        self.assertEqual(retained["context_snapshot"]["project_summary"], "Legacy source context")
        self.assertEqual(migrated.propose_goal_and_check_prerequisites(goal_title="Build", domain="Math", required_concepts=["Core"])["status"], "ALREADY_SCHEDULED")
        versions = []
        for path in migrated.backups_dir.glob("*.db"):
            versions.append(migrated._check_snapshot(path)["schema_version"])
        self.assertIn(2, versions)

    def test_rehearsal_detects_draft_hash_mismatch(self):
        task = self.schedule()[0]
        self.db.record_project_context(goal_title="Build", domain="Math", project_summary="Hash test")
        session = self.db.begin_proactive_work(task_id=task["id"], request_id="hash")["session"]["id"]
        self.db.save_work_artifact(session_id=session, relative_path="draft.md", content="Actual draft")
        with self.db._connection() as connection:
            connection.execute("UPDATE work_artifacts SET sha256=?", ("0" * 64,))
        backup = self.db.backup_database(manual=True)
        result = self.db.validate_backup_and_rehearse(backup.name)
        self.assertEqual(result["status"], "BACKUP_INVALID")
        self.assertIn("checksum", result["reason"])


if __name__ == "__main__":
    unittest.main()
