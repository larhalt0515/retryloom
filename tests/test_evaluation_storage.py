"""Evaluation persistence preserves replay evidence and legacy evaluations."""
import hashlib
import json
import sqlite3

import pytest

from retryloom.replay.evaluator import evaluate
from retryloom.replay.models import CheckResult, ReplayCase, ReplayCheck, ReplayReport, ReplayRun, RunResult
from retryloom.storage.database import connect, initialize_schema
from retryloom.storage.repository import load_replay_report, save_evaluation, save_replay
from retryloom.storage.schema import SCHEMA_SQL


def recorded_report():
    case = ReplayCase(id="case", repository="/repo", base_commit="a" * 40, task="Fix task",
                      runs=2, lesson_id="lesson", lesson_hash="b" * 64,
                      agent="codex", agent_version="1", model="recorded-model",
                      checks=[ReplayCheck(name="tests", command=["test-command"])])
    manifest = {key: value for key, value in case.model_dump(mode="json").items()
                if key not in {"id", "repository", "control_timeout_seconds", "reflex_timeout_seconds"}}
    manifest["requested_base_commit"] = case.base_commit
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    runs = []
    for arm in ("CONTROL", "REFLEX"):
        for number in (1, 2):
            success = arm == "REFLEX" and number == 1
            status = "COMPLETED" if success else "REQUIRED_CHECK_FAILED"
            runs.append(ReplayRun(
                id=f"{arm}-{number}", condition=arm, run_number=number, seed=number - 1,
                status=status, statuses=[status],
                result=RunResult(success=success, exit_code=0, token_total=100,
                                 elapsed_seconds=1, stdout="recorded", stderr="", agent=case.agent,
                                 agent_version=case.agent_version, model=case.model,
                                 lesson_hash=case.lesson_hash),
                checks=[CheckResult(name="tests", command=["test-command"], success=success,
                                    exit_code=0 if success else 1)],
                metadata={"manifest_hash": digest, "base_commit": case.base_commit,
                          "tool_environment_keys": ["HOME", "PATH"]},
            ))
    report = ReplayReport(id="report", case_id=case.id, status="COMPLETED", runs=runs,
                          metadata={"manifest": manifest, "manifest_hash": digest,
                                    "resolved_base_commit": case.base_commit, "efficiency_usable": True,
                                    "runner": {"agent": case.agent, "agent_version": case.agent_version,
                                               "model": case.model, "model_settings": {}}})
    return case, report


def saved_report(tmp_path):
    database = connect(tmp_path / "evaluation.db")
    case, report = recorded_report()
    database.execute("INSERT INTO lessons(id,scope,trigger_json,rule,evidence_sessions_json,confidence,status,lesson_hash) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (case.lesson_id, "repository", "{}", "Run tests.", "[]", 0.5, "candidate", case.lesson_hash))
    database.commit()
    save_replay(database, case, report)
    return database, report


def snapshot(database):
    return {table: [tuple(row) for row in database.execute(f"SELECT * FROM {table}")]
            for table in ("replay_cases", "replay_checks", "replay_reports", "replay_runs",
                          "replay_check_results", "lessons")}


def test_round_trip_evaluation_is_append_only_and_linked(tmp_path):
    database, report = saved_report(tmp_path)
    assert load_replay_report(database, report.id).model_dump() == report.model_dump()
    evaluation = evaluate(report)
    assert evaluation.status == "replay_confirmed"
    original = snapshot(database)
    first = save_evaluation(database, report, evaluation)
    second = save_evaluation(database, report, evaluation)
    assert isinstance(first, int) and second > first
    assert snapshot(database) == original
    row = database.execute("SELECT * FROM evaluations WHERE id=?", (first,)).fetchone()
    assert row["report_id"] == report.id
    assert row["manifest_hash"] == report.metadata["manifest_hash"]
    report_json = json.dumps(report.model_dump(mode="json"), sort_keys=True, allow_nan=False)
    assert row["report_hash"] == hashlib.sha256(report_json.encode()).hexdigest()
    assert json.loads(row["evaluation_json"]) == evaluation.model_dump()
    assert json.loads(row["evidence_json"]) == evaluation.evidence
    assert row["condition"] == "A" and row["created_at"]
    database.close()
    reopened = connect(tmp_path / "evaluation.db")
    assert load_replay_report(reopened, report.id) == report
    assert reopened.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 2
    reopened.close()


def test_loading_frozen_report_ignores_mutable_case_definitions(tmp_path):
    database, report = saved_report(tmp_path)
    expected = evaluate(report)
    database.execute("UPDATE replay_cases SET runs=99,task='changed',lesson_hash='changed'")
    database.execute("UPDATE replay_checks SET command='[\"different\"]',required=0")
    database.commit()
    frozen = load_replay_report(database, report.id)
    assert frozen == report
    assert evaluate(frozen) == expected
    save_evaluation(database, frozen, expected)
    database.close()


def test_missing_or_changed_report_cannot_be_linked(tmp_path):
    database, report = saved_report(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        load_replay_report(database, "absent")
    evaluation = evaluate(report)
    altered = report.model_copy(deep=True)
    altered.runs[0].result.stdout = "tampered"
    with pytest.raises(ValueError, match="differs from persisted"):
        save_evaluation(database, altered, evaluation)
    altered_evaluation = evaluation.model_copy(deep=True)
    altered_evaluation.evidence["report_id"] = "other"
    with pytest.raises(ValueError, match="linkage"):
        save_evaluation(database, report, altered_evaluation)
    assert database.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
    database.close()


@pytest.mark.parametrize("field,value", [
    ("status", "rejected"), ("condition", "B"), ("reason", "forged reason"),
    ("control_success_count", 100), ("reflex_success_count", 100),
    ("control_median_tokens", 999), ("reflex_median_tokens", 1),
    ("token_reduction_ratio", 0.99), ("required_check_regression", True),
])
def test_forged_evaluation_fields_are_rejected(tmp_path, field, value):
    database, report = saved_report(tmp_path)
    before = snapshot(database)
    forged = evaluate(report).model_copy(update={field: value})
    with pytest.raises(ValueError, match="deterministic recorded-evidence"):
        save_evaluation(database, report, forged)
    assert database.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
    assert snapshot(database) == before
    database.close()


def test_forged_confirmation_of_rejected_report_is_rejected(tmp_path):
    database, report = saved_report(tmp_path)
    # Use independently saved evidence with no improvement, preserving all linkage IDs.
    database.execute("UPDATE replay_runs SET success=0,status='REQUIRED_CHECK_FAILED',"
                     "statuses_json='[\"REQUIRED_CHECK_FAILED\"]' WHERE condition='REFLEX'")
    database.execute("UPDATE replay_check_results SET success=0,exit_code=1 "
                     "WHERE replay_run_id IN (SELECT id FROM replay_runs WHERE condition='REFLEX')")
    database.commit()
    recorded = load_replay_report(database, report.id)
    genuine = evaluate(recorded)
    assert genuine.status == "rejected"
    forged = genuine.model_copy(update={"status": "replay_confirmed", "condition": "A"})
    before = snapshot(database)
    with pytest.raises(ValueError, match="deterministic recorded-evidence"):
        save_evaluation(database, recorded, forged)
    assert snapshot(database) == before
    assert database.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
    database.close()


@pytest.mark.parametrize("version", ["1", "2"])
def test_migration_preserves_legacy_evaluation_and_null_evidence(tmp_path, version):
    path = tmp_path / "legacy.db"
    database = sqlite3.connect(path)
    database.executescript(SCHEMA_SQL)
    database.execute("INSERT INTO metadata VALUES ('schema_version',?)", (version,))
    database.execute("INSERT INTO evaluations(replay_case_id,status,control_success,reflex_success,reason) "
                     "VALUES ('legacy-case','rejected',1,1,'legacy reason')")
    database.commit()
    database.close()
    upgraded = connect(path)
    row = upgraded.execute("SELECT * FROM evaluations").fetchone()
    assert tuple(row)[:6] == (1, "legacy-case", "rejected", 1, 1, "legacy reason")
    assert row["report_id"] is None and row["evidence_json"] is None and row["report_hash"] is None
    assert upgraded.execute("SELECT value FROM metadata").fetchone()[0] == "5"
    initialize_schema(upgraded)
    assert upgraded.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    upgraded.close()


def test_v2_evaluation_migration_failure_rolls_back_all_added_columns(tmp_path, monkeypatch):
    import retryloom.storage.database as module
    database = sqlite3.connect(tmp_path / "rollback.db")
    database.executescript(SCHEMA_SQL)
    database.execute("INSERT INTO metadata VALUES ('schema_version','2')")
    database.commit()
    before = list(database.execute("PRAGMA table_info(evaluations)"))
    monkeypatch.setattr(module, "COLUMN_MIGRATIONS", {"evaluations": {
        "report_id": "TEXT REFERENCES replay_reports(id)", "broken": "INVALID SYNTAX ("}})
    with pytest.raises(sqlite3.OperationalError):
        initialize_schema(database)
    assert list(database.execute("PRAGMA table_info(evaluations)")) == before
    assert database.execute("SELECT value FROM metadata").fetchone()[0] == "2"
    database.close()
