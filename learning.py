"""Persistent, evidence-gated learning for ordinary knowledge questions.

The host teaches and grades; this local service stores actual answers, enforces
the sequence, and returns the next action. It does not run an LLM or wake it.
"""
from __future__ import annotations

import json
import unicodedata
import uuid
from typing import Literal

from pydantic import Field

if __package__:
    from .models import StrictInput, Name, Description, SessionId
else:
    from models import StrictInput, Name, Description, SessionId

KINDS = ("EXPLAIN", "APPLY", "TRANSFER")
LEARNING_INSTRUCTIONS = (
    "For EVERY learning or knowledge-explanation request (including 'explain X', 'why', "
    "'讲解一下', '为什么', '教我'), proactively call start_learning_session before teaching; "
    "do not wait for the user to request a task, quiz, or mastery check. "
    "For an ongoing learning reply or session resume, call get_learning_followup. "
    "Teach briefly, call set_learning_question, ask ONE concrete question, and WAIT for "
    "the user's real answer. Call record_learning_answer with that actual answer and "
    "criterion-by-criterion assessment; never answer for the learner. If incorrect or "
    "assisted, explain the specific gap and ask a NEW question. Continue EXPLAIN -> APPLY "
    "-> TRANSFER until independent evidence passes, then ask for confirmation and call "
    "confirm_learning_mastery only on the user's real confirmation. 'I understand', "
    "self-ratings, assistant explanations, and accepted drafts are not mastery evidence. "
    "Use domain-appropriate exercises, not mandatory coding or a large project. "
    "Respect explicit requests for a quick answer/no quiz, a pause, or a topic change; "
    "do not force quizzes on non-learning requests. No unsolicited background reminders. "
    "Formal project scheduling still requires propose_goal_and_check_prerequisites."
)

LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_sessions (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','AWAITING_ANSWER','READY_FOR_CONFIRMATION','MASTERED','PAUSED')),
    user_confirmation TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_open_node ON learning_sessions(node_id)
    WHERE status <> 'MASTERED';
CREATE TABLE IF NOT EXISTS learning_checks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES learning_sessions(id),
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('EXPLAIN','APPLY','TRANSFER')),
    question TEXT NOT NULL,
    question_key TEXT NOT NULL,
    rubric_json TEXT NOT NULL,
    user_answer TEXT,
    assessment_json TEXT,
    passed INTEGER CHECK(passed IN (0,1)),
    assisted INTEGER CHECK(assisted IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id,request_id),
    UNIQUE(session_id,question_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_pending ON learning_checks(session_id)
    WHERE user_answer IS NULL;
"""


class LearningStart(StrictInput):
    concept: Name
    domain: Name


class LearningLookup(StrictInput):
    session_id: SessionId | None = None
    domain: Name | None = None


class LearningQuestion(StrictInput):
    session_id: SessionId
    request_id: Name
    kind: Literal["EXPLAIN", "APPLY", "TRANSFER"]
    question: Description
    rubric: list[Name] = Field(min_length=1, max_length=10)


class LearningAnswer(StrictInput):
    question_id: SessionId
    user_answer: Description
    criteria_met: list[bool] = Field(min_length=1, max_length=10)
    feedback: Description
    assisted: bool


class LearningConfirm(StrictInput):
    session_id: SessionId
    user_confirmed: bool
    user_confirmation: Description


class LearningControl(StrictInput):
    session_id: SessionId
    action: Literal["PAUSE", "RESUME"]


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


class LearningMixin:
    def _learning_session(self, connection, session_id):
        row = connection.execute(
            "SELECT s.*,n.name AS concept,n.domain FROM learning_sessions s "
            "JOIN knowledge_nodes n ON n.id=s.node_id WHERE s.id=?", (session_id,)).fetchone()
        if row is None:
            raise self.learning_error_class("LEARNING_NOT_FOUND", "Use an existing learning session")
        return row

    def _learning_result(self, connection, session_id):
        session = dict(self._learning_session(connection, session_id))
        checks = [dict(r) for r in connection.execute(
            "SELECT * FROM learning_checks WHERE session_id=? ORDER BY rowid", (session_id,))]
        latest = {c["kind"]: c for c in checks if c["user_answer"] is not None}
        missing = [k for k in KINDS if k not in latest or not latest[k]["passed"] or latest[k]["assisted"]]
        pending = next((c for c in checks if c["user_answer"] is None), None)
        last = next((c for c in reversed(checks) if c["user_answer"] is not None), None)
        action = "EXPLAIN_AND_ASK"
        if session["status"] == "PAUSED":
            action = "PAUSED"
        elif session["status"] == "MASTERED":
            action = "DONE"
        elif pending:
            action = "WAIT_FOR_ANSWER"
        elif not missing:
            action = "ASK_CONFIRMATION"
        elif last and (not last["passed"] or last["assisted"]):
            action = "REMEDIATE_AND_ASK"
        session.update(next_action=action, required_kind=missing[0] if missing else None,
                       missing_evidence=missing, attempts=len(checks))
        if pending:
            session["pending_question"] = {"id": pending["id"], "kind": pending["kind"],
                                           "question": pending["question"], "rubric": json.loads(pending["rubric_json"])}
        if last:
            session["last_assessment"] = {"question": last["question"], "user_answer": last["user_answer"],
                                          **json.loads(last["assessment_json"])}
        return {"status": "LEARNING_FOLLOWUP", "session": session,
                "directive": LEARNING_INSTRUCTIONS,
                "assessment_boundary": "Host-reported answers and grading; not independently verified by the server"}

    def start_learning_session(self, **arguments):
        request = LearningStart.model_validate(arguments)
        with self._write() as connection:
            node = self._ensure_node(connection, request.concept, request.domain)
            previous = connection.execute(
                "SELECT id FROM learning_sessions WHERE node_id=? ORDER BY rowid DESC LIMIT 1", (node["id"],)).fetchone()
            # Idempotent start/resume, including a completed or explicitly paused session.
            if previous:
                result = self._learning_result(connection, previous["id"])
            else:
                session_id = uuid.uuid4().hex
                connection.execute("INSERT INTO learning_sessions(id,node_id,status) VALUES(?,?,'ACTIVE')", (session_id, node["id"]))
                connection.execute("UPDATE knowledge_nodes SET status='LEARNING',updated_at=CURRENT_TIMESTAMP "
                                   "WHERE id=? AND status='WISHLIST'", (node["id"],))
                result = self._learning_result(connection, session_id)
        return result

    def get_learning_followup(self, **arguments):
        request = LearningLookup.model_validate(arguments)
        with self._connection() as connection:
            connection.execute("BEGIN")
            if request.session_id:
                return self._learning_result(connection, request.session_id)
            rows = connection.execute(
                "SELECT s.id,n.domain FROM learning_sessions s JOIN knowledge_nodes n ON n.id=s.node_id "
                "WHERE s.status <> 'MASTERED' ORDER BY s.rowid DESC").fetchall()
            sessions = [self._learning_result(connection, r["id"])["session"] for r in rows
                        if request.domain is None or normalized(request.domain) == normalized(r["domain"])]
            return {"status": "LEARNING_FOLLOWUP", "sessions": sessions,
                    "directive": "Resume only the user's current topic; keep other sessions pending. " + LEARNING_INSTRUCTIONS}

    def set_learning_question(self, **arguments):
        request = LearningQuestion.model_validate(arguments)
        if len(set(map(normalized, request.rubric))) != len(request.rubric):
            raise self.learning_error_class("DUPLICATE_CRITERIA", "Use distinct grading criteria")
        with self._write() as connection:
            session = self._learning_session(connection, request.session_id)
            prior = connection.execute("SELECT * FROM learning_checks WHERE session_id=? AND request_id=?",
                                       (request.session_id, request.request_id)).fetchone()
            if prior:
                if (prior["question"], prior["kind"], json.loads(prior["rubric_json"])) != (request.question, request.kind, request.rubric):
                    raise self.learning_error_class("QUESTION_RETRY_CONFLICT", "Reuse request_id only for identical retries")
                return self._learning_result(connection, request.session_id)
            state = self._learning_result(connection, request.session_id)["session"]
            if session["status"] != "ACTIVE":
                raise self.learning_error_class("LEARNING_NOT_ACTIVE", "Wait for the pending answer/confirmation or explicitly resume")
            if request.kind != state["required_kind"]:
                raise self.learning_error_class("EVIDENCE_ORDER", "Ask the next required question type", required_kind=state["required_kind"])
            question_key = normalized(request.question)
            if connection.execute("SELECT 1 FROM learning_checks WHERE session_id=? AND question_key=?", (request.session_id, question_key)).fetchone():
                raise self.learning_error_class("NEW_QUESTION_REQUIRED", "Use a new question to test independent understanding")
            connection.execute("INSERT INTO learning_checks(id,session_id,request_id,kind,question,question_key,rubric_json) VALUES(?,?,?,?,?,?,?)",
                               (uuid.uuid4().hex, request.session_id, request.request_id, request.kind,
                                request.question, question_key, json.dumps(request.rubric, ensure_ascii=False)))
            connection.execute("UPDATE learning_sessions SET status='AWAITING_ANSWER',updated_at=CURRENT_TIMESTAMP WHERE id=?", (request.session_id,))
            result = self._learning_result(connection, request.session_id)
        return result

    def record_learning_answer(self, **arguments):
        request = LearningAnswer.model_validate(arguments)
        with self._write() as connection:
            question = connection.execute("SELECT * FROM learning_checks WHERE id=?", (request.question_id,)).fetchone()
            if question is None:
                raise self.learning_error_class("QUESTION_NOT_FOUND", "Use the pending question id")
            assessment = {"criteria_met": request.criteria_met, "feedback": request.feedback,
                          "assisted": request.assisted, "passed": all(request.criteria_met) and not request.assisted}
            payload = json.dumps(assessment, ensure_ascii=False)
            if question["user_answer"] is not None:
                if (question["user_answer"], question["assessment_json"]) != (request.user_answer, payload):
                    raise self.learning_error_class("ANSWER_IMMUTABLE", "Recorded answers cannot be replaced; ask a new question")
                return self._learning_result(connection, question["session_id"])
            session = self._learning_session(connection, question["session_id"])
            if session["status"] != "AWAITING_ANSWER":
                raise self.learning_error_class("LEARNING_NOT_ACTIVE", "Resume the learning session before recording an answer")
            if len(request.criteria_met) != len(json.loads(question["rubric_json"])):
                raise self.learning_error_class("RUBRIC_MISMATCH", "Assess every stored criterion exactly once")
            # Generic acknowledgements are not explanations, applications, or transfer evidence.
            acknowledgement = re_acknowledgement(request.user_answer)
            if acknowledgement:
                raise self.learning_error_class("SUBSTANTIVE_ANSWER_REQUIRED", "Ask for the learner's reasoning or worked answer")
            connection.execute("UPDATE learning_checks SET user_answer=?,assessment_json=?,passed=?,assisted=? WHERE id=?",
                               (request.user_answer, payload, int(assessment["passed"]), int(request.assisted), request.question_id))
            state = self._learning_result(connection, question["session_id"])["session"]
            status = "READY_FOR_CONFIRMATION" if not state["missing_evidence"] else "ACTIVE"
            connection.execute("UPDATE learning_sessions SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, question["session_id"]))
            count = len(KINDS) - len(state["missing_evidence"])
            connection.execute("UPDATE knowledge_nodes SET status='LEARNING',mastery_score=MAX(mastery_score,?),updated_at=CURRENT_TIMESTAMP "
                               "WHERE id=? AND status <> 'MASTERED'", (min(90, 40 + count * 15), session["node_id"]))
            result = self._learning_result(connection, question["session_id"])
        return result

    def confirm_learning_mastery(self, **arguments):
        request = LearningConfirm.model_validate(arguments)
        if not request.user_confirmed or re_acknowledgement(request.user_confirmation, confirmation=True):
            raise self.learning_error_class("USER_CONFIRMATION_REQUIRED", "Record the user's actual affirmative assessment confirmation")
        with self._write() as connection:
            session = self._learning_session(connection, request.session_id)
            state = self._learning_result(connection, request.session_id)["session"]
            if state["missing_evidence"] or session["status"] not in ("READY_FOR_CONFIRMATION", "MASTERED"):
                raise self.learning_error_class("LEARNING_EVIDENCE_REQUIRED", "Require independent EXPLAIN, APPLY and TRANSFER evidence before confirmation")
            if session["status"] != "MASTERED":
                connection.execute("UPDATE learning_sessions SET status='MASTERED',user_confirmation=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                   (request.user_confirmation, request.session_id))
                connection.execute("UPDATE knowledge_nodes SET status='MASTERED',mastery_score=100,updated_at=CURRENT_TIMESTAMP WHERE id=?", (session["node_id"],))
            result = self._learning_result(connection, request.session_id)
        return result

    def control_learning_session(self, **arguments):
        request = LearningControl.model_validate(arguments)
        with self._write() as connection:
            session = self._learning_session(connection, request.session_id)
            if session["status"] == "MASTERED":
                raise self.learning_error_class("LEARNING_COMPLETED", "This learning session is already complete")
            if request.action == "PAUSE":
                status = "PAUSED"
            else:
                state = self._learning_result(connection, request.session_id)["session"]
                status = ("AWAITING_ANSWER" if "pending_question" in state else
                          "READY_FOR_CONFIRMATION" if not state["missing_evidence"] else "ACTIVE")
            connection.execute("UPDATE learning_sessions SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, request.session_id))
            result = self._learning_result(connection, request.session_id)
        return result


def re_acknowledgement(text, confirmation=False):
    value = normalized(text).strip("。.!！?？ ")
    if confirmation:
        return value in {"不", "否", "没有", "不确认", "未掌握", "no", "false"}
    return value in {"懂了", "学会了", "我懂了", "我学会了", "明白了", "知道了", "是", "好", "好的",
                     "ok", "yes", "understood", "i understand", "got it", "a", "b", "c", "d"}
