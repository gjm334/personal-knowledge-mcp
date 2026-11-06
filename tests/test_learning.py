"""Learning Q&A entry, correction, resumption, and evidence gates."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import ValidationError
from db_manager import DatabaseManager, WorkspaceError


class LearningTests(unittest.TestCase):
    def setUp(self):
        (Path(__file__).resolve().parents[1] / "local_data").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "local_data")
        self.addCleanup(self.tmp.cleanup)
        self.db = DatabaseManager(Path(self.tmp.name))
        self.sid = self.db.start_learning_session(concept="戴维南定理", domain="电路分析")["session"]["id"]

    def state(self):
        return self.db.get_learning_followup(session_id=self.sid)["session"]

    def error(self, code, method, **kwargs):
        with self.assertRaises(WorkspaceError) as caught:
            method(**kwargs)
        self.assertEqual(caught.exception.code, code)

    def question(self, kind="EXPLAIN", text="为何开路电压是等效电压？", request="q1"):
        return self.db.set_learning_question(session_id=self.sid, request_id=request, kind=kind,
                    question=text, rubric=["区分外部负载与内部网络", "给出适用的电路关系"])["session"]["pending_question"]["id"]

    def answer(self, qid, correct=True, assisted=False):
        return self.db.record_learning_answer(question_id=qid,
            user_answer="断开外部负载后端口电流为零，等效串联电阻压降为零，端口电压等于等效源电压。",
            criteria_met=[correct, correct], feedback="依据端口电流和压降逐项检查。", assisted=assisted)

    def complete_evidence(self):
        for i, kind in enumerate(("EXPLAIN", "APPLY", "TRANSFER")):
            qid = self.question(kind, f"独立题目 {i}：解释或计算并说明依据", f"q{i}")
            self.answer(qid)

    def test_plain_knowledge_question_starts_without_formal_goal_or_tasks(self):
        self.assertEqual(self.state()["required_kind"], "EXPLAIN")
        overview = self.db.get_workspace_overview(domain="电路分析")
        self.assertEqual(overview["tasks"], [])
        self.assertEqual(overview["knowledge_nodes"][0]["status"], "LEARNING")
        self.assertEqual(len(overview["learning_sessions"]), 1)

    def test_repeated_entry_and_concurrent_start_reuse_same_session(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.db.start_learning_session(concept="戴维南定理", domain="电路分析"), range(2)))
        self.assertEqual({r["session"]["id"] for r in results}, {self.sid})

    def test_one_pending_question_and_question_retries(self):
        qid = self.question()
        self.assertEqual(self.state()["next_action"], "WAIT_FOR_ANSWER")
        self.assertEqual(self.question(), qid)
        self.error("LEARNING_NOT_ACTIVE", self.db.set_learning_question, session_id=self.sid,
                   request_id="q2", kind="EXPLAIN", question="另一题", rubric=["原理"])
        self.error("QUESTION_RETRY_CONFLICT", self.db.set_learning_question, session_id=self.sid,
                   request_id="q1", kind="EXPLAIN", question="修改原题", rubric=["原理"])

    def test_incorrect_answer_demands_remediation_and_new_question(self):
        qid = self.question()
        self.answer(qid, correct=False)
        state = self.state()
        self.assertEqual(state["next_action"], "REMEDIATE_AND_ASK")
        self.assertEqual(state["required_kind"], "EXPLAIN")
        self.error("NEW_QUESTION_REQUIRED", self.db.set_learning_question, session_id=self.sid,
                   request_id="new", kind="EXPLAIN", question="为何开路电压是等效电压？", rubric=["原因"])
        self.answer(self.question(text="开路时串联等效电阻上有无压降，为什么？", request="q2"))
        self.assertEqual(self.state()["required_kind"], "APPLY")

    def test_assisted_success_does_not_count_as_independent_mastery(self):
        self.answer(self.question(), assisted=True)
        self.assertEqual(self.state()["required_kind"], "EXPLAIN")
        self.assertEqual(self.state()["next_action"], "REMEDIATE_AND_ASK")

    def test_self_rating_and_acknowledgement_cannot_pass(self):
        qid = self.question()
        self.error("SUBSTANTIVE_ANSWER_REQUIRED", self.db.record_learning_answer,
                   question_id=qid, user_answer="我学会了", criteria_met=[True, True], feedback="声明", assisted=False)
        self.assertEqual(self.state()["next_action"], "WAIT_FOR_ANSWER")

    def test_blank_answer_and_incomplete_rubric_rejected(self):
        qid = self.question()
        with self.assertRaises(ValidationError):
            self.db.record_learning_answer(question_id=qid, user_answer="  ", criteria_met=[True, True], feedback="检查", assisted=False)
        self.error("RUBRIC_MISMATCH", self.db.record_learning_answer,
                   question_id=qid, user_answer="端口电流为零", criteria_met=[True], feedback="未评完", assisted=False)

    def test_answer_retry_idempotent_and_evidence_immutable(self):
        qid = self.question()
        self.answer(qid)
        self.answer(qid)
        self.assertEqual(self.state()["attempts"], 1)
        self.error("ANSWER_IMMUTABLE", self.db.record_learning_answer,
                   question_id=qid, user_answer="换一段回答", criteria_met=[True, True], feedback="替换", assisted=False)

    def test_evidence_order_and_premature_confirmation_rejected(self):
        self.error("EVIDENCE_ORDER", self.db.set_learning_question, session_id=self.sid,
                   request_id="skip", kind="TRANSFER", question="直接换场景", rubric=["正确"])
        self.error("LEARNING_EVIDENCE_REQUIRED", self.db.confirm_learning_mastery,
                   session_id=self.sid, user_confirmed=True, user_confirmation="确认掌握")

    def test_three_independent_stages_then_real_confirmation(self):
        self.complete_evidence()
        self.assertEqual(self.state()["next_action"], "ASK_CONFIRMATION")
        node = self.db.get_workspace_overview()["knowledge_nodes"][0]
        self.assertEqual(node["status"], "LEARNING")
        self.error("USER_CONFIRMATION_REQUIRED", self.db.confirm_learning_mastery,
                   session_id=self.sid, user_confirmed=False, user_confirmation="还不确定")
        self.db.confirm_learning_mastery(session_id=self.sid, user_confirmed=True,
                   user_confirmation="确认，这三题是我独立完成并能解释的。")
        self.assertEqual(self.state()["next_action"], "DONE")
        self.assertEqual(self.db.get_workspace_overview()["knowledge_nodes"][0]["status"], "MASTERED")

    def test_pause_resume_and_restart_keep_exact_pending_question(self):
        qid = self.question()
        self.db.control_learning_session(session_id=self.sid, action="PAUSE")
        self.assertEqual(self.state()["next_action"], "PAUSED")
        self.error("LEARNING_NOT_ACTIVE", self.db.record_learning_answer,
                   question_id=qid, user_answer="实际作答", criteria_met=[True, True], feedback="检查", assisted=False)
        fresh = DatabaseManager(Path(self.tmp.name))
        self.assertEqual(fresh.start_learning_session(concept="戴维南定理", domain="电路分析")["session"]["next_action"], "PAUSED")
        fresh.control_learning_session(session_id=self.sid, action="RESUME")
        self.assertEqual(fresh.get_learning_followup(session_id=self.sid)["session"]["pending_question"]["id"], qid)

    def test_domains_and_unrelated_topic_do_not_merge_sessions(self):
        self.db.start_learning_session(concept="线性回归", domain="统计")
        results = self.db.get_learning_followup(domain="电路分析")["sessions"]
        self.assertEqual([r["id"] for r in results], [self.sid])

    def test_backup_restore_and_vault_include_pending_learning(self):
        self.question(text="解释 <script> 与 | 管道的差别")
        backup = self.db.backup_database(manual=True)
        result = self.db.validate_backup_and_rehearse(backup.name)
        self.assertEqual(result["status"], "BACKUP_VALID", result)
        self.assertIn("learning_checks", result["row_counts"])
        text = Path(self.db.vault_paths()["tasks"]).read_text(encoding="utf-8")
        self.assertIn("待回答", text)
        self.assertNotIn("<script>", text)

    def test_schema_v3_migration_preserves_knowledge_and_backups(self):
        legacy = Path(self.tmp.name) / "legacy"
        old = DatabaseManager(legacy)
        old.upsert_standalone_knowledge(name="欧姆定律", domain="电路分析", summary="保留原笔记")
        with old._connection() as connection:
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (tbl_name='learning_sessions' OR tbl_name='learning_checks')").fetchall():
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute("DROP TABLE learning_checks")
            connection.execute("DROP TABLE learning_sessions")
            connection.execute("PRAGMA user_version=3")
        fresh = DatabaseManager(legacy)
        self.assertEqual(fresh.get_workspace_overview()["knowledge_nodes"][0]["summary"], "保留原笔记")
        self.assertIn(3, [fresh._check_snapshot(path)["schema_version"] for path in fresh.backups_dir.glob("*.db")])


if __name__ == "__main__":
    unittest.main()
