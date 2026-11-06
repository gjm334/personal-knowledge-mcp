"""Proactive work produces concrete drafts without claiming the user learned the material."""

import hashlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from db_manager import DatabaseManager, SCHEMA, VaultSyncError, WorkspaceError


class ProactiveWorkTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / "local_data").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT / "local_data")
        self.addCleanup(self.temporary.cleanup)
        self.db = DatabaseManager(Path(self.temporary.name))
        self.context = {"goal_title": "Prepare a square experiment", "domain": "Math",
                        "project_summary": "Implement a minimal square function and test it",
                        "recent_progress": "The goal and acceptance criteria are known",
                        "sources": [{"reference": "selected-requirements.md", "excerpt": "Return x*x; test positive, negative and zero inputs.\n"}]}

    def schedule(self, choice="C", with_context=True):
        self.db.propose_goal_and_check_prerequisites(goal_title=self.context["goal_title"], domain="Math", required_concepts=["Squaring"])
        pipeline = self.db.submit_probe_answers_and_create_pipeline(
            goal_title=self.context["goal_title"], domain="Math",
            probe_answers=[{"concept_name": "Squaring", "domain": "Math", "choice": choice, "summary": "Multiplying a number by itself"}],
            tasks_pipeline=[{"title": "Implement square", "task_type": "PROJECT", "definition_of_done": "Verify positive, negative and zero inputs", "linked_concepts": ["Squaring"]}], edges=[])
        if with_context:
            self.db.record_project_context(**self.context)
        return pipeline["tasks"]

    def begin(self, choice="C"):
        tasks = self.schedule(choice)
        work = self.db.begin_proactive_work(task_id=tasks[0]["id"], request_id="initial")
        return tasks, work["session"]["id"]

    def submit(self, session_id, outcome="PASSED"):
        self.db.save_work_artifact(session_id=session_id, relative_path="notes/square.md", content="# Square\n\nMultiply the input by itself.\n")
        return self.db.finish_proactive_work(session_id=session_id, summary="Square note ready for review",
                                            checks=[{"name": "Definition review", "outcome": outcome, "evidence": "Checked definition against selected context"}])

    def assert_code(self, code, function, **arguments):
        with self.assertRaises(WorkspaceError) as captured:
            function(**arguments)
        self.assertEqual(captured.exception.code, code)

    def test_proposal_and_actual_context_are_required(self):
        self.assert_code("PROPOSAL_REQUIRED", self.db.record_project_context, **self.context)
        tasks = self.schedule(with_context=False)
        self.assertEqual(self.db.get_proactive_work_brief()["status"], "CONTEXT_REQUIRED")
        self.assert_code("CONTEXT_REQUIRED", self.db.begin_proactive_work, task_id=tasks[0]["id"], request_id="x")

    def test_brief_selects_deliverable_using_progress_and_task_type(self):
        tasks = self.schedule("B")
        brief = self.db.get_proactive_work_brief(domain="Math")
        self.assertEqual(brief["status"], "WORK_AVAILABLE")
        self.assertEqual(brief["candidates"][0]["artifact_type"], "DOCUMENT")
        self.assertEqual(brief["candidates"][0]["task_id"], tasks[0]["id"])
        self.assertEqual(len(brief["blocked_tasks"]), 2)
        self.assertNotIn("excerpt", str(brief))
        self.db.update_task_status(task_id=tasks[0]["id"], new_status="DONE")
        self.assertEqual(self.db.get_proactive_work_brief()["candidates"][0]["artifact_type"], "EXPERIMENT")
        self.assertEqual(self.db.get_proactive_work_brief(domain="Other")["status"], "NO_ACTION")

    def test_identical_context_and_begin_retries_are_idempotent(self):
        tasks, session_id = self.begin()
        context = self.db.record_project_context(**self.context)
        self.assertFalse(context["changed"])
        self.assertEqual(context["revision"], 1)
        replay = self.db.begin_proactive_work(task_id=tasks[0]["id"], request_id="initial")
        self.assertEqual(replay["session"]["id"], session_id)
        self.assertEqual(replay["status"], "IDEMPOTENT_REPLAY")
        self.assert_code("REQUEST_CONFLICT", self.db.begin_proactive_work, task_id=tasks[0]["id"], request_id="initial", artifact_type="DOCUMENT")

    def test_dependencies_and_explicit_task_and_project_blockers_are_enforced(self):
        tasks = self.schedule("B")
        self.assert_code("DEPENDENCY_NOT_DONE", self.db.begin_proactive_work, task_id=tasks[1]["id"], request_id="early")
        self.db.update_task_status(task_id=tasks[0]["id"], new_status="BLOCKED", notes="Need a dataset")
        self.assert_code("TASK_NOT_ACTIONABLE", self.db.begin_proactive_work, task_id=tasks[0]["id"], request_id="blocked")
        self.db.update_task_status(task_id=tasks[0]["id"], new_status="TODO")
        self.db.record_project_context(**{**self.context, "blockers": ["Missing input specification"]})
        self.assert_code("PROJECT_BLOCKED", self.db.begin_proactive_work, task_id=tasks[0]["id"], request_id="project-blocked")
        self.assertEqual(self.db.get_proactive_work_brief()["candidates"], [])

    def test_real_python_artifact_is_executable_and_round_trips_exactly(self):
        tasks, session_id = self.begin()
        content = "\ndef square(x):\n    return x * x\n\nassert square(3) == 9\nassert square(-2) == 4\nassert square(0) == 0\nprint('3 checks passed')\n"
        saved = self.db.save_work_artifact(session_id=session_id, relative_path="experiments/square.py", content=content)
        artifact = Path(saved["path"])
        self.assertTrue(artifact.is_relative_to(self.db.data_dir / "drafts"))
        self.assertEqual(artifact.read_text(encoding="utf-8"), content)
        self.assertEqual(saved["sha256"], hashlib.sha256(content.encode()).hexdigest())
        executed = subprocess.run([sys.executable, str(artifact)], capture_output=True, text=True, timeout=10, check=True)
        self.assertIn("3 checks passed", executed.stdout)
        result = self.db.finish_proactive_work(session_id=session_id, summary="Minimal square experiment verified",
                                               checks=[{"name": "Python assertions", "outcome": "PASSED", "evidence": executed.stdout.strip()}])
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        inspected = self.db.get_work_session(session_id=session_id, include_artifact_content=True)["session"]
        self.assertEqual(inspected["artifacts"][0]["content"], content)
        self.assertEqual(inspected["context_snapshot"]["sources"], self.context["sources"])
        self.assertEqual(self.db.get_workspace_overview()["tasks"][0]["status"], "TODO")

    def test_review_does_not_complete_learning_or_upgrade_knowledge(self):
        tasks, session_id = self.begin("B")
        self.submit(session_id)
        self.db.review_proactive_work(session_id=session_id, decision="ACCEPT")
        overview = self.db.get_workspace_overview()
        self.assertEqual(overview["tasks"][0]["status"], "TODO")
        self.assertEqual(overview["knowledge_nodes"][0]["mastery_score"], 50)
        self.assertEqual(self.db.get_proactive_work_brief()["candidates"], [])
        replay = self.db.review_proactive_work(session_id=session_id, decision="ACCEPT")
        self.assertTrue(replay["idempotent_replay"])

    def test_finish_requires_artifacts_and_honest_check_records(self):
        _, session_id = self.begin()
        checks = [{"name": "Check", "outcome": "PASSED", "evidence": "Observed output"}]
        self.assert_code("ARTIFACT_REQUIRED", self.db.finish_proactive_work, session_id=session_id, summary="Ready", checks=checks)
        self.db.save_work_artifact(session_id=session_id, relative_path="draft.md", content="An actual note")
        checks[0]["outcome"] = "FAILED"
        self.assert_code("CHECKS_FAILED", self.db.finish_proactive_work, session_id=session_id, summary="Ready", checks=checks)
        checks[0]["outcome"] = "NOT_RUN"
        checks[0]["evidence"] = "Requires the user's dataset, which is unavailable"
        result = self.db.finish_proactive_work(session_id=session_id, summary="Draft with an unverified dataset check", checks=checks)
        self.assertTrue(result["has_unrun_checks"])
        self.assertTrue(self.db.finish_proactive_work(session_id=session_id, summary="Draft with an unverified dataset check", checks=checks)["idempotent_replay"])

    def test_user_revision_reopens_draft_and_clears_outdated_checks(self):
        _, session_id = self.begin()
        self.assert_code("REVIEW_NOT_READY", self.db.review_proactive_work, session_id=session_id, decision="ACCEPT")
        self.submit(session_id)
        self.assert_code("FEEDBACK_REQUIRED", self.db.review_proactive_work, session_id=session_id, decision="REVISE")
        review = self.db.review_proactive_work(session_id=session_id, decision="REVISE", feedback="Add a boundary case")
        self.assertEqual(review["session"]["checks"], [])
        self.assertEqual(review["status"], "RUNNING")
        replay = self.db.review_proactive_work(session_id=session_id, decision="REVISE", feedback="Add a boundary case")
        self.assertTrue(replay["idempotent_replay"])
        self.db.save_work_artifact(session_id=session_id, relative_path="notes/square.md", content="Square of zero is zero.\n")
        self.assertEqual(len(self.db.get_work_session(session_id=session_id)["session"]["artifacts"]), 1)

    def test_submitted_artifacts_are_immutable_and_pending_review_is_visible(self):
        _, session_id = self.begin()
        self.submit(session_id)
        self.assert_code("WORK_NOT_RUNNING", self.db.save_work_artifact, session_id=session_id, relative_path="overwrite.py", content="print(1)")
        brief = self.db.get_proactive_work_brief()
        self.assertEqual(brief["candidates"], [])
        self.assertEqual(brief["pending_work"][0]["status"], "READY_FOR_REVIEW")
        self.assertIn("notes/square.md", self.db.read_vault("active_work"))

    def test_rejection_does_not_generate_repeated_unsolicited_work(self):
        _, session_id = self.begin()
        self.submit(session_id)
        self.db.review_proactive_work(session_id=session_id, decision="REJECT", feedback="This approach is not useful")
        self.assertEqual(self.db.get_proactive_work_brief()["candidates"], [])

    def test_changed_context_invalidates_work_and_preserves_its_original_sources(self):
        tasks, session_id = self.begin()
        self.submit(session_id)
        updated = {**self.context, "recent_progress": "Now account for integer overflow", "sources": []}
        self.db.record_project_context(**updated)
        old = self.db.get_work_session(session_id=session_id, include_artifact_content=True)["session"]
        self.assertEqual(old["status"], "STALE")
        self.assertEqual(old["context_snapshot"]["sources"], self.context["sources"])
        self.assert_code("WORK_NOT_RUNNING", self.db.save_work_artifact, session_id=session_id, relative_path="draft.md", content="Stale")
        fresh = self.db.begin_proactive_work(task_id=tasks[0]["id"], request_id="new-context")
        self.assertEqual(fresh["session"]["context_revision"], 2)

    def test_changed_task_notes_invalidate_the_previous_work(self):
        tasks, session_id = self.begin()
        self.db.update_task_status(task_id=tasks[0]["id"], new_status="TODO", notes="Add an overflow boundary")
        self.assert_code("WORK_STALE", self.db.save_work_artifact, session_id=session_id, relative_path="draft.md", content="Old draft")
        fresh = self.db.begin_proactive_work(task_id=tasks[0]["id"], request_id="changed-task")
        self.assertNotEqual(fresh["session"]["id"], session_id)

    def test_concurrent_claims_have_only_one_owner(self):
        tasks = self.schedule()
        other = DatabaseManager(self.db.data_dir)
        def claim(item):
            index, manager = item
            try:
                return manager.begin_proactive_work(task_id=tasks[0]["id"], request_id=f"claim-{index}")["status"]
            except WorkspaceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, enumerate((self.db, other))))
        self.assertCountEqual(results, ["WORK_STARTED", "WORK_ALREADY_ACTIVE"])

    def test_expired_work_can_be_reclaimed_without_losing_artifacts(self):
        tasks, session_id = self.begin()
        self.db.save_work_artifact(session_id=session_id, relative_path="draft.md", content="Retain this draft")
        future = self.db._utc_now() + timedelta(hours=2)
        with patch.object(self.db, "_utc_now", return_value=future):
            self.assert_code("WORK_EXPIRED", self.db.save_work_artifact, session_id=session_id, relative_path="draft.md", content="Too late")
            self.assertTrue(self.db.get_proactive_work_brief()["candidates"])
            new = self.db.begin_proactive_work(task_id=tasks[0]["id"], request_id="reclaim")
            self.assertNotEqual(new["session"]["id"], session_id)
        self.assertEqual(self.db.get_work_session(session_id=session_id)["session"]["status"], "EXPIRED")
        self.assertEqual(len(self.db.get_work_session(session_id=session_id)["session"]["artifacts"]), 1)

    def test_artifact_paths_reject_traversal_reserved_names_and_binary_extensions(self):
        _, session_id = self.begin()
        for path in ("../outside.py", "/outside.py", "C:\\outside.py", "sub//x.py", "sub/../../x.py", "CON.txt", "AUX.py", "run.exe", "sub/.secret.py"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self.db.save_work_artifact(session_id=session_id, relative_path=path, content="print(1)")
        self.assertEqual(self.db.get_work_session(session_id=session_id)["session"]["artifacts"], [])

    def test_committed_artifact_export_failure_is_recoverable(self):
        _, session_id = self.begin()
        with patch.object(self.db, "_atomic_text", side_effect=PermissionError("Locked file")):
            with self.assertLogs("db_manager", level="ERROR"), self.assertRaises(VaultSyncError):
                self.db.save_work_artifact(session_id=session_id, relative_path="draft.md", content="A committed draft\n")
        self.db.trigger_manual_backup_and_export()
        artifact = self.db.get_work_session(session_id=session_id)["session"]["artifacts"][0]
        self.assertEqual(Path(artifact["path"]).read_text(encoding="utf-8"), "A committed draft\n")

    def test_case_only_alias_cannot_overwrite_another_artifact_on_windows(self):
        _, session_id = self.begin()
        self.db.save_work_artifact(session_id=session_id, relative_path="notes/A.md", content="Original")
        self.assert_code("ARTIFACT_PATH_CONFLICT", self.db.save_work_artifact,
                         session_id=session_id, relative_path="notes/a.md", content="Collision")
        artifacts = self.db.get_work_session(session_id=session_id, include_artifact_content=True)["session"]["artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["content"], "Original")

    def test_file_directory_collision_is_rejected_before_database_commit(self):
        _, session_id = self.begin()
        self.db.save_work_artifact(session_id=session_id, relative_path="folder.py/child.md", content="Nested draft")
        self.assert_code("ARTIFACT_PATH_CONFLICT", self.db.save_work_artifact,
                         session_id=session_id, relative_path="folder.py", content="Conflicting file")
        self.db.trigger_manual_backup_and_export()
        self.assertEqual(len(self.db.get_work_session(session_id=session_id)["session"]["artifacts"]), 1)

    def test_v1_migration_preserves_legacy_data_and_takes_a_pre_migration_snapshot(self):
        legacy = Path(self.temporary.name) / "legacy"
        legacy.mkdir()
        schema = re.sub(r"CREATE TABLE IF NOT EXISTS project_contexts .*?(?=CREATE INDEX IF NOT EXISTS idx_tasks_prerequisite)", "", SCHEMA, flags=re.S)
        schema = schema.replace("PRAGMA user_version=4", "PRAGMA user_version=1")
        with sqlite3.connect(legacy / "agent_memory.db") as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript(schema)
            connection.execute("INSERT INTO tasks(id,title,task_type,definition_of_done,notes) VALUES(42,'Legacy task','STUDY','Review notes','Keep this note')")
        connection.close()
        upgraded = DatabaseManager(legacy)
        self.assertEqual(upgraded.get_workspace_overview()["tasks"][0]["notes"], "Keep this note")
        with upgraded._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
        versions = []
        for backup in upgraded.backups_dir.glob("*.db"):
            with sqlite3.connect(backup) as connection:
                connection.row_factory = sqlite3.Row
                versions.append(connection.execute("PRAGMA user_version").fetchone()[0])
            connection.close()
        self.assertIn(1, versions)


if __name__ == "__main__":
    unittest.main()
