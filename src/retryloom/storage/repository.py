"""Repository operations for canonical sessions."""
import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from retryloom.models import Session, FailureEvent
from retryloom.replay.models import CheckResult, Evaluation, ReplayCase, ReplayReport, ReplayRun, RunResult


def load_session(connection: sqlite3.Connection, session_id: str) -> Session:
    """Restore imported event order without reparsing or rewriting source history."""
    from retryloom.models import SessionEvent
    cursor = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"session not found: {session_id}")
    values = dict(zip((column[0] for column in cursor.description), row))
    cursor = connection.execute("SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,))
    columns = [column[0] for column in cursor.description]
    events = []
    for row in cursor:
        event = dict(zip(columns, row))
        event["metadata"] = json.loads(event.pop("metadata_json"))
        events.append(SessionEvent.model_validate({key: event[key] for key in SessionEvent.model_fields}))
    return Session.model_validate({**values, "events": events})


def load_failure(connection: sqlite3.Connection, failure_id: str) -> FailureEvent:
    """Read the recorded detector evidence belonging to a frozen reference."""
    cursor = connection.execute("SELECT * FROM failures WHERE id=?", (failure_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"failure not found: {failure_id}")
    values = dict(zip((column[0] for column in cursor.description), row))
    values["signals"] = json.loads(values.pop("signals_json"))
    values["evidence"] = json.loads(values.pop("evidence_json"))
    return FailureEvent.model_validate(values)

def save_session(connection: sqlite3.Connection, session: Session) -> None:
    connection.execute("""INSERT OR REPLACE INTO sessions (id, agent, agent_version, model, repository, base_commit, started_at, ended_at, task, result, token_input, token_output, tool_call_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (session.id, session.agent, session.agent_version, session.model, session.repository, session.base_commit, session.started_at.isoformat(), session.ended_at.isoformat() if session.ended_at else None, session.task, session.result, session.token_input, session.token_output, session.tool_call_count))
    connection.execute("DELETE FROM session_events WHERE session_id = ?", (session.id,))
    connection.executemany("INSERT INTO session_events (session_id, type, timestamp, command, exit_code, file_path, content, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(session.id, event.type, event.timestamp.isoformat() if event.timestamp else None, event.command, event.exit_code, event.file_path, event.content, json.dumps(event.metadata, sort_keys=True)) for event in session.events])
    connection.commit()


def save_failure(connection: sqlite3.Connection, failure: FailureEvent) -> None:
    with connection:
        connection.execute(
            "INSERT INTO failures VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET score=excluded.score, confidence=excluded.confidence, "
            "signals_json=excluded.signals_json, summary=excluded.summary, evidence_json=excluded.evidence_json",
            (failure.id, failure.session_id, failure.score, failure.confidence,
             json.dumps(failure.signals), failure.summary, json.dumps(failure.evidence)),
        )


def save_replay(connection: sqlite3.Connection, case: ReplayCase, report: ReplayReport) -> None:
    """Keep outputs local and persist available metrics without inventing values."""
    def insert(table: str, values: dict) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))

    with connection:
        connection.execute(
            "INSERT INTO replay_cases (id,repository,base_commit,task,runs,lesson_id,lesson_hash,"
            "timeout_seconds,agent,agent_version,model,model_settings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET repository=excluded.repository,base_commit=excluded.base_commit,"
            "task=excluded.task,runs=excluded.runs,lesson_id=excluded.lesson_id,lesson_hash=excluded.lesson_hash,"
            "timeout_seconds=excluded.timeout_seconds,agent=excluded.agent,agent_version=excluded.agent_version,"
            "model=excluded.model,model_settings_json=excluded.model_settings_json",
            (case.id, str(case.repository), report.metadata.get("resolved_base_commit", case.base_commit),
             case.task, case.runs, case.lesson_id, case.lesson_hash, case.timeout_seconds, case.agent,
             case.agent_version, case.model, json.dumps(case.model_settings, sort_keys=True)),
        )
        connection.execute("DELETE FROM replay_checks WHERE replay_case_id=?", (case.id,))
        for check in case.checks:
            insert("replay_checks", {"replay_case_id": case.id, "name": check.name,
                                   "command": json.dumps(check.command), "required": int(check.required),
                                   "timeout_seconds": check.timeout_seconds})
        insert("replay_reports", {"id": report.id, "replay_case_id": case.id,
                                 "status": report.status, "reason": report.reason,
                                 "warnings_json": json.dumps(report.warnings),
                                 "metadata_json": json.dumps(report.metadata, sort_keys=True),
                                 "created_at": datetime.now(timezone.utc).isoformat()})
        for run in report.runs:
            values = run.result.model_dump()
            for key in ("success", "timed_out", "output_truncated", "efficiency_usable"):
                values[key] = int(values[key])
            values.update(id=run.id, replay_case_id=case.id, report_id=report.id,
                          condition=run.condition, run_number=run.run_number, status=run.status,
                          statuses_json=json.dumps(run.statuses), seed=run.seed,
                          metadata_json=json.dumps(run.metadata, sort_keys=True))
            insert("replay_runs", values)
            for number, check in enumerate(run.checks, 1):
                values = check.model_dump(exclude={"command"})
                for key in ("required", "success", "timed_out", "output_truncated"):
                    values[key] = int(values[key])
                values.update(replay_run_id=run.id, check_number=number,
                              command_json=json.dumps(check.command))
                insert("replay_check_results", values)


def load_replay_report(connection: sqlite3.Connection, report_id: str) -> ReplayReport:
    """Reconstruct frozen evidence without consulting mutable case/check definitions."""
    def records(sql: str, parameters: tuple) -> list[dict]:
        cursor = connection.execute(sql, parameters)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    reports = records("SELECT * FROM replay_reports WHERE id=?", (report_id,))
    if not reports:
        raise ValueError(f"replay report not found: {report_id}")
    row = reports[0]
    runs = []
    for stored in records("SELECT * FROM replay_runs WHERE report_id=? ORDER BY rowid", (report_id,)):
        results = {name: stored[name] for name in RunResult.model_fields}
        checks = []
        for check in records("SELECT * FROM replay_check_results WHERE replay_run_id=? "
                             "ORDER BY check_number", (stored["id"],)):
            values = {name: check[name] for name in CheckResult.model_fields if name != "command"}
            values["command"] = json.loads(check["command_json"])
            checks.append(CheckResult(**values))
        runs.append(ReplayRun(id=stored["id"], condition=stored["condition"],
                              run_number=stored["run_number"], result=RunResult(**results),
                              checks=checks, status=stored["status"],
                              statuses=json.loads(stored["statuses_json"]), seed=stored["seed"],
                              metadata=json.loads(stored["metadata_json"])))
    return ReplayReport(id=row["id"], case_id=row["replay_case_id"], status=row["status"],
                        reason=row["reason"], warnings=json.loads(row["warnings_json"]),
                        metadata=json.loads(row["metadata_json"]), runs=runs)


def save_evaluation(connection: sqlite3.Connection, report: ReplayReport,
                    evaluation: Evaluation) -> int:
    """Append a linked evaluation; leave every replay and lesson record untouched."""
    with connection:
        stored = load_replay_report(connection, report.id)
        if stored.model_dump() != report.model_dump():
            raise ValueError("evaluation report differs from persisted replay evidence")
        if (evaluation.evidence.get("report_id") != report.id or
                evaluation.evidence.get("case_id") != report.case_id or
                evaluation.evidence.get("manifest_hash") != report.metadata.get("manifest_hash")):
            raise ValueError("evaluation linkage differs from replay report")
        from retryloom.replay.evaluator import evaluate
        if evaluation.model_dump() != evaluate(stored).model_dump():
            raise ValueError("evaluation differs from deterministic recorded-evidence result")
        report_json = json.dumps(stored.model_dump(mode="json"), sort_keys=True, allow_nan=False)
        report_hash = hashlib.sha256(report_json.encode()).hexdigest()
        cursor = connection.execute(
            "INSERT INTO evaluations (replay_case_id,status,control_success,reflex_success,reason,"
            "report_id,condition,control_median_tokens,reflex_median_tokens,token_reduction_ratio,"
            "required_check_regression,evidence_json,manifest_hash,report_hash,evaluation_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (report.case_id, evaluation.status, evaluation.control_success_count,
             evaluation.reflex_success_count, evaluation.reason, report.id, evaluation.condition,
             evaluation.control_median_tokens, evaluation.reflex_median_tokens,
             evaluation.token_reduction_ratio, int(evaluation.required_check_regression),
             json.dumps(evaluation.evidence, sort_keys=True, allow_nan=False),
             report.metadata.get("manifest_hash"), report_hash, evaluation.model_dump_json(),
             datetime.now(timezone.utc).isoformat()),
        )
        return int(cursor.lastrowid)


# Phase8 managed lessons use immutable snapshots. Legacy Phase6 fixtures remain
# readable by the replay engine, but cannot enter the managed preparation API.
def _lesson_row(connection: sqlite3.Connection, lesson_id: str) -> dict:
    cursor = connection.execute(
        "SELECT lessons.*,lesson_freezes.lesson_json,lesson_freezes.canonical_fingerprint,"
        "lesson_freezes.historical_evidence_fingerprint FROM lessons "
        "LEFT JOIN lesson_freezes ON lesson_freezes.lesson_id=lessons.id WHERE lessons.id=?", (lesson_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"lesson not found: {lesson_id}")
    return dict(zip((column[0] for column in cursor.description), row))


def _stored_audit(connection: sqlite3.Connection, lesson_id: str) -> dict:
    cursor = connection.execute("SELECT * FROM lesson_audits WHERE lesson_id=?", (lesson_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("managed lesson audit is missing")
    return dict(zip((column[0] for column in cursor.description), row))


def _approved_resolution(connection: sqlite3.Connection, lesson, audit_row: dict) -> bool:
    cursor = connection.execute(
        "SELECT reviewer,reason,approved,canonical_fingerprint,evidence_fingerprint,audit_fingerprint "
        "FROM lesson_audit_resolutions WHERE lesson_id=? ORDER BY id DESC LIMIT 1", (lesson.id,))
    row = cursor.fetchone()
    return bool(row and row[0].strip() and row[1].strip() and row[2] == 1 and
                row[3] == lesson.canonical_fingerprint and
                row[4] == lesson.historical_evidence_fingerprint and
                row[5] == audit_row["audit_fingerprint"])


def load_lesson(connection: sqlite3.Connection, lesson_id: str):
    """Validate all duplicated frozen fields and historical reference/hash data."""
    return _load_frozen_lesson(connection, lesson_id)


def _load_frozen_lesson(connection: sqlite3.Connection, lesson_id: str, *, check_status: bool = True):
    """Verify immutable evidence; lifecycle readers separately verify status proof."""
    from retryloom.models import Lesson, LessonAudit
    from retryloom.analysis.lessons import fingerprint, lesson_fingerprint
    from retryloom.replay.injection import FrozenLesson
    row = _lesson_row(connection, lesson_id)
    if not row["lesson_json"]:
        raise ValueError("legacy lesson has no frozen context/audit; explicit migration review required")
    lesson = Lesson.model_validate_json(row["lesson_json"])
    if lesson.canonical_fingerprint != lesson_fingerprint(lesson):
        raise ValueError("frozen lesson context fingerprint mismatch")
    if (not lesson.lesson_hash or lesson.id != lesson_id or
            row["canonical_fingerprint"] != lesson.canonical_fingerprint or
            row["historical_evidence_fingerprint"] != lesson.historical_evidence_fingerprint or
            row["scope"] != lesson.scope or json.loads(row["trigger_json"]) != lesson.trigger.model_dump() or
            row["rule"] != lesson.rule or row["lesson_hash"] != lesson.lesson_hash or
            json.loads(row["evidence_sessions_json"]) != list(lesson.evidence_sessions) or
            row["confidence"] != lesson.confidence or (check_status and row["status"] != lesson.status)):
        raise ValueError("stored lesson differs from frozen context")
    FrozenLesson(lesson.rule, lesson.lesson_hash).verify()
    stored = _stored_audit(connection, lesson_id)
    audit = LessonAudit.model_validate_json(stored["audit_json"])
    if audit != lesson.audit or fingerprint(audit.model_dump(mode="json")) != stored["audit_fingerprint"]:
        raise ValueError("stored lesson audit differs from frozen audit")
    snapshot = json.loads(stored["evidence_json"])
    if fingerprint(snapshot) != lesson.historical_evidence_fingerprint:
        raise ValueError("stored historical evidence fingerprint mismatch")
    from retryloom.models import LessonEvidenceRef
    selected = tuple(LessonEvidenceRef(**{key: item[key] for key in LessonEvidenceRef.model_fields})
                     for item in snapshot)
    if selected != lesson.evidence_refs or tuple(ref.evidence_hash for ref in selected) != audit.evidence_hashes:
        raise ValueError("stored historical references differ from frozen references")
    sessions = tuple(row[0] for row in connection.execute(
        "SELECT session_id FROM lesson_evidence WHERE lesson_id=? ORDER BY session_id", (lesson_id,)))
    if sessions != lesson.evidence_sessions:
        raise ValueError("stored lesson evidence associations changed")
    resolved = audit.status == "SUSPECT" and _approved_resolution(connection, lesson, stored)
    expected_flag = audit.status != "ACCEPT" and not resolved
    if row["leak_suspect"] != int(expected_flag):
        raise ValueError("stored leakage flag lacks a valid current audit resolution")
    return lesson


def verify_managed_lesson(connection: sqlite3.Connection, lesson_id: str):
    """Phase6 compatibility hook: validate managed rows, leave legacy rows alone."""
    row = _lesson_row(connection, lesson_id)
    if row["lesson_json"] is None:
        # Missing snapshots must not silently turn a managed lesson into legacy.
        if row["canonical_fingerprint"] is not None or connection.execute(
                "SELECT 1 FROM lesson_audits WHERE lesson_id=?", (lesson_id,)).fetchone():
            raise ValueError("managed lesson snapshot is missing")
        return None
    return load_lesson(connection, lesson_id)


def save_lesson(connection: sqlite3.Connection, lesson, evidence):
    """Audit and freeze once. Public saves never promote or overwrite a lesson."""
    from retryloom.analysis.lessons import _freeze_audited, evidence_snapshot
    from retryloom.analysis.leakage import audit_lesson
    _verify_selected_history(connection, evidence)
    frozen = _freeze_audited(lesson, evidence)
    if audit_lesson(frozen, evidence) != frozen.audit:
        raise ValueError("frozen audit differs from current evidence")
    existing = connection.execute("SELECT 1 FROM lessons WHERE id=?", (frozen.id,)).fetchone()
    if existing:
        stored = load_lesson(connection, frozen.id)
        if stored != frozen:
            raise ValueError("cannot rewrite a frozen lesson")
        return stored
    snapshot = evidence_snapshot(evidence)
    if len(snapshot) != len(frozen.evidence_refs):
        raise ValueError("cannot persist a lesson without complete usable evidence")
    from retryloom.analysis.lessons import fingerprint
    with connection:
        connection.execute(
            "INSERT INTO lessons(id,scope,trigger_json,rule,evidence_sessions_json,confidence,status,"
            "lesson_hash,leak_suspect) VALUES (?,?,?,?,?,?,?,?,?)",
            (frozen.id, frozen.scope, frozen.trigger.model_dump_json(), frozen.rule,
             json.dumps(frozen.evidence_sessions), frozen.confidence, frozen.status, frozen.lesson_hash,
             int(frozen.leak_suspect)))
        connection.execute(
            "INSERT INTO lesson_freezes(lesson_id,lesson_json,canonical_fingerprint,historical_evidence_fingerprint) "
            "VALUES (?,?,?,?)", (frozen.id, frozen.model_dump_json(), frozen.canonical_fingerprint,
                                  frozen.historical_evidence_fingerprint))
        connection.executemany("INSERT INTO lesson_evidence(lesson_id,session_id) VALUES (?,?)",
                               [(frozen.id, session) for session in frozen.evidence_sessions])
        connection.execute("INSERT INTO lesson_audits(lesson_id,audit_json,evidence_json,audit_fingerprint) "
                           "VALUES (?,?,?,?)", (frozen.id, frozen.audit.model_dump_json(),
                                                json.dumps(snapshot, sort_keys=True),
                                                fingerprint(frozen.audit.model_dump(mode="json"))))
    return frozen


def _verify_current_evidence(lesson, evidence) -> None:
    from retryloom.analysis.lessons import evidence_snapshot, fingerprint
    from retryloom.analysis.leakage import audit_lesson
    if fingerprint(evidence_snapshot(evidence)) != lesson.historical_evidence_fingerprint:
        raise ValueError("current evidence differs from frozen historical evidence")
    if audit_lesson(lesson, evidence) != lesson.audit:
        raise ValueError("current audit differs from frozen audit")


def prepare_lesson_for_replay(connection: sqlite3.Connection, lesson_id: str, evidence):
    from retryloom.replay.injection import FrozenLesson
    lesson = load_lesson(connection, lesson_id)
    _verify_current_evidence(lesson, evidence)
    if lesson.status == "rejected" or lesson.audit.status == "REJECT":
        raise ValueError("rejected lesson cannot be prepared for replay")
    if lesson.audit.status == "SUSPECT" and not _approved_resolution(
            connection, lesson, _stored_audit(connection, lesson_id)):
        raise ValueError("suspect lesson requires explicit manual audit resolution")
    return FrozenLesson(lesson.rule, lesson.lesson_hash)


def resolve_lesson_audit(connection: sqlite3.Connection, lesson_id: str, evidence, *,
                         reviewer: str, reason: str, approved: bool):
    """Record human review bound to the exact current content/audit/evidence."""
    if not reviewer.strip() or not reason.strip():
        raise ValueError("explicit reviewer and reason are required")
    lesson = load_lesson(connection, lesson_id)
    _verify_current_evidence(lesson, evidence)
    if lesson.audit.status == "REJECT":
        raise ValueError("a REJECT audit cannot be overridden")
    if lesson.audit.status != "SUSPECT":
        raise ValueError("only SUSPECT findings require manual resolution")
    stored = _stored_audit(connection, lesson_id)
    with connection:
        connection.execute(
            "INSERT INTO lesson_audit_resolutions(lesson_id,reviewer,reason,approved,canonical_fingerprint,"
            "evidence_fingerprint,audit_fingerprint,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (lesson.id, reviewer.strip(), reason.strip(), int(approved), lesson.canonical_fingerprint,
             lesson.historical_evidence_fingerprint, stored["audit_fingerprint"],
             datetime.now(timezone.utc).isoformat()))
        connection.execute("UPDATE lessons SET leak_suspect=? WHERE id=?", (int(not approved), lesson.id))
    return load_lesson(connection, lesson_id)


def _verify_selected_history(connection: sqlite3.Connection, evidence) -> None:
    """Validate selected refs against saved canonical events and FailureEvents.

    This check happens before creation; historical snapshots stay immutable when
    a session is later reimported. Raw history never enters the lesson snapshot.
    """
    from retryloom.models import SessionEvent
    from retryloom.analysis.lessons import evidence_items, usable
    from retryloom.analysis.corrections import extract_corrections
    items = evidence_items(evidence)
    if not items:
        raise ValueError("complete selected correction evidence is required")
    for item in items:
        if not usable(item):
            raise ValueError("incomplete selected correction evidence")
        cursor = connection.execute("SELECT * FROM sessions WHERE id=?", (item.session_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("selected evidence session is not stored")
        session_data = dict(zip((column[0] for column in cursor.description), row))
        events = []
        cursor = connection.execute("SELECT * FROM session_events WHERE session_id=? ORDER BY id", (item.session_id,))
        columns = [column[0] for column in cursor.description]
        for row in cursor:
            event = dict(zip(columns, row))
            event["metadata"] = json.loads(event.pop("metadata_json"))
            events.append(SessionEvent(**{key: event[key] for key in SessionEvent.model_fields}))
        session = Session(**session_data, events=events)
        if session.repository != item.repository or any(ref >= len(events) for ref in (
                item.failure_event_ref, item.correction_event_ref, item.success_event_ref)):
            raise ValueError("selected evidence repository/event reference differs from stored session")
        cursor = connection.execute("SELECT * FROM failures WHERE id=?", (item.failure_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("selected evidence failure is not stored")
        failure_data = dict(zip((column[0] for column in cursor.description), row))
        failure_data["signals"] = json.loads(failure_data.pop("signals_json"))
        failure_data["evidence"] = json.loads(failure_data.pop("evidence_json"))
        failure = FailureEvent(**failure_data)
        if failure.session_id != session.id:
            raise ValueError("selected failure does not belong to stored session")
        matches = [selected for selected in extract_corrections(session, failure)
                   if (selected.failure_type, selected.failure_event_ref,
                       selected.correction_event_ref, selected.success_event_ref) ==
                   (item.failure_type, item.failure_event_ref,
                    item.correction_event_ref, item.success_event_ref)]
        if not matches:
            raise ValueError("selected correction/success refs do not match stored failure history")
        selected = matches[0]
        if any(getattr(item, field) != getattr(selected, field) for field in
               ("correction_diff", "source_before", "success_proof", "final_patch")):
            raise ValueError("selected source/correction/proof differs from stored canonical events")
