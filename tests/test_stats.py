"""Stats aggregate verified persisted evidence without writes."""
import hashlib
import json
import sqlite3

import pytest

from retryloom.output.stats import collect_stats
from retryloom.replay.evaluator import evaluate
from retryloom.storage.database import connect
from retryloom.storage.repository import save_evaluation, save_replay
from test_evaluation_storage import recorded_report, saved_report


def attest(db, report):
    case, _ = recorded_report()
    case.runs = report.metadata["manifest"]["runs"] if "manifest" in report.metadata else 2
    # Replace only synthetic fixture evidence before its first evaluation.
    db.execute("DELETE FROM replay_check_results")
    db.execute("DELETE FROM replay_runs")
    db.execute("DELETE FROM replay_reports")
    db.execute("DELETE FROM replay_checks")
    db.execute("DELETE FROM replay_cases")
    save_replay(db, case, report)
    save_evaluation(db, report, evaluate(report))


def test_empty_database(tmp_path):
    db = connect(tmp_path / "empty.db")
    stats = collect_stats(db)
    assert stats["sessions_analyzed"] == stats["failures_detected"] == 0
    assert stats["lessons"] == dict(candidate=0, replay_confirmed=0, rejected=0, applied=0)
    assert stats["replay_comparisons"] == 0
    assert stats["arms"]["CONTROL"]["median_token_total"] is None
    assert stats["arms"]["REFLEX"]["success_rate"] is None


def test_counts_and_statuses(tmp_path):
    db = connect(tmp_path / "counts.db")
    db.execute("INSERT INTO sessions(id,agent,repository,started_at) VALUES ('s','Claude','/repo','2026-01-01')")
    db.execute("INSERT INTO failures VALUES ('f','s',5,'MEDIUM','[]','test failure','[]')")
    for status in ("candidate", "replay_confirmed", "rejected", "applied"):
        db.execute("INSERT INTO lessons(id,scope,trigger_json,rule,evidence_sessions_json,confidence,status) "
                   "VALUES (?, 'repository','{}','Run checks','[]',0.5,?)", (status, status))
    stats = collect_stats(db)
    assert stats["sessions_analyzed"] == stats["failures_detected"] == 1
    assert all(count == 1 for count in stats["lessons"].values())


def test_valid_counts_duplicate_evaluations_and_metrics(tmp_path):
    db, report = saved_report(tmp_path)
    for number, run in enumerate(report.runs):
        run.result.token_total = 100 + number * 20
        run.result.tool_calls = number + 1
        run.result.elapsed_seconds = number + 1
    attest(db, report)
    save_evaluation(db, report, evaluate(report))
    stats = collect_stats(db)
    assert stats["replay_comparisons"] == stats["valid_comparisons"] == 1
    assert stats["improvement_confirmed"] == 1
    assert stats["arms"]["CONTROL"]["success_count"] == 0
    assert stats["arms"]["REFLEX"]["success_rate"] == 0.5
    assert stats["arms"]["CONTROL"]["median_token_total"] == 110
    assert stats["arms"]["REFLEX"]["median_tool_calls"] == 3.5
    assert stats["arms"]["REFLEX"]["median_elapsed_seconds"] == 3.5


def test_missing_metrics_are_unknown_with_availability(tmp_path):
    db, report = saved_report(tmp_path)
    for run in report.runs:
        run.result.token_total = None
        run.result.tool_calls = None
    report.runs[0].result.elapsed_seconds = 2
    attest(db, report)
    arm = collect_stats(db)["arms"]["CONTROL"]
    assert arm["median_token_total"] is arm["median_tool_calls"] is None
    assert arm["token_total_available_count"] == arm["tool_calls_available_count"] == 0
    assert arm["median_elapsed_seconds"] == 1.5


def test_mixed_available_missing_metrics(tmp_path):
    db, report = saved_report(tmp_path)
    report.runs[0].result.token_total = None
    report.runs[0].result.tool_calls = 0
    report.runs[1].result.tool_calls = None
    attest(db, report)
    arm = collect_stats(db)["arms"]["CONTROL"]
    assert arm["median_token_total"] == 100
    assert arm["token_total_available_count"] == 1
    assert arm["median_tool_calls"] == 0
    assert arm["tool_calls_available_count"] == 1


@pytest.mark.parametrize("status", ["BASELINE_NOT_REPRODUCIBLE", "INVALID_COMPARISON", "INVALID_LESSON_HASH"])
def test_invalid_reports_excluded(tmp_path, status):
    db, report = saved_report(tmp_path)
    report.status = status
    attest(db, report)
    stats = collect_stats(db)
    assert stats["invalid_comparisons"] == 1
    assert stats["valid_comparisons"] == stats["arms"]["CONTROL"]["run_count"] == 0


@pytest.mark.parametrize("column,value", [
    ("report_hash", "bad"), ("evaluation_json", "{}"), ("evidence_json", "{}"),
    ("control_success", 999), ("status", "rejected"), ("manifest_hash", "wrong"),
])
def test_corrupt_latest_evaluation_not_replaced_by_old_valid_one(tmp_path, column, value):
    db, report = saved_report(tmp_path)
    save_evaluation(db, report, evaluate(report))
    latest = save_evaluation(db, report, evaluate(report))
    db.execute(f"UPDATE evaluations SET {column}=? WHERE id=?", (value, latest))
    stats = collect_stats(db)
    assert stats["valid_comparisons"] == 0
    assert stats["invalid_comparisons"] == 1


def test_corrupted_report_and_missing_evidence(tmp_path):
    db, report = saved_report(tmp_path)
    save_evaluation(db, report, evaluate(report))
    db.execute("UPDATE replay_runs SET stdout='tampered'")
    assert collect_stats(db)["invalid_comparisons"] == 1
    db.execute("DELETE FROM evaluations")
    assert collect_stats(db)["unevaluated_comparisons"] == 1


@pytest.mark.parametrize("field,value", [("timed_out", True), ("agent_error", "crash"),
                                         ("cleanup_error", "cleanup"), ("efficiency_usable", False)])
def test_partial_unusable_run_not_efficiency_observation(tmp_path, field, value):
    db, report = saved_report(tmp_path)
    setattr(report.runs[0].result, field, value)
    report.runs[0].result.token_total = 1
    report.runs[0].result.tool_calls = 1
    attest(db, report)
    arm = collect_stats(db)["arms"]["CONTROL"]
    assert arm["median_token_total"] == 100
    assert arm["token_total_available_count"] == 1
    assert arm["tool_calls_available_count"] == 0


def test_insufficient_tokens_still_complete_success_comparison(tmp_path):
    db, report = saved_report(tmp_path)
    for index, arm in ((1, "CONTROL"), (3, "REFLEX")):
        third = report.runs[index].model_copy(deep=True)
        third.id = f"{arm}-3"
        third.run_number = 3
        third.seed = 2
        report.runs.append(third)
    report.metadata["manifest"]["runs"] = 3
    digest = hashlib.sha256(json.dumps(report.metadata["manifest"], sort_keys=True).encode()).hexdigest()
    report.metadata["manifest_hash"] = digest
    for run in report.runs:
        run.metadata["manifest_hash"] = digest
    report.runs[0].result.success = True
    report.runs[0].status = "COMPLETED"
    report.runs[0].statuses = ["COMPLETED"]
    report.runs[0].checks[0].success = True
    report.runs[0].checks[0].exit_code = 0
    report.runs[3].result.token_total = None
    attest(db, report)
    assert evaluate(report).status == "INSUFFICIENT_EVIDENCE"
    assert collect_stats(db)["valid_comparisons"] == 1
    assert collect_stats(db)["improvement_confirmed"] == 0


@pytest.mark.parametrize("field,value", [("token_total", -1), ("tool_calls", -1)])
def test_negative_and_nonfinite_metrics_not_observations(tmp_path, field, value):
    db, report = saved_report(tmp_path)
    report.runs[1].result.tool_calls = 2
    setattr(report.runs[0].result, field, value)
    attest(db, report)
    arm = collect_stats(db)["arms"]["CONTROL"]
    assert arm[f"{field}_available_count"] == 1


def test_nonfinite_corrupted_runtime_excludes_comparison(tmp_path):
    db, report = saved_report(tmp_path)
    save_evaluation(db, report, evaluate(report))
    db.execute("UPDATE replay_runs SET elapsed_seconds=? WHERE id='CONTROL-1'", (float("inf"),))
    assert collect_stats(db)["invalid_comparisons"] == 1


def test_report_warning_disables_efficiency_not_success_counts(tmp_path):
    db, report = saved_report(tmp_path)
    report.warnings = ["cleanup advisory"]
    attest(db, report)
    stats = collect_stats(db)
    assert stats["valid_comparisons"] == 1
    assert stats["arms"]["REFLEX"]["success_count"] == 1
    assert stats["arms"]["REFLEX"]["median_elapsed_seconds"] is None


def test_missing_manifest_integrity_excluded(tmp_path):
    db, report = saved_report(tmp_path)
    report.metadata.pop("manifest")
    attest(db, report)
    assert collect_stats(db)["invalid_comparisons"] == 1


def test_malformed_stored_evidence_fails_closed(tmp_path):
    db, report = saved_report(tmp_path)
    save_evaluation(db, report, evaluate(report))
    db.execute("UPDATE evaluations SET evidence_json='not json'")
    assert collect_stats(db)["invalid_comparisons"] == 1


def test_read_only_bytes_and_logical_evidence_unchanged(tmp_path):
    db, report = saved_report(tmp_path)
    save_evaluation(db, report, evaluate(report))
    path = tmp_path / "evaluation.db"
    before_dump = list(db.iterdump())
    db.close()
    before = path.read_bytes()
    readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    expected = collect_stats(readonly)
    for _ in range(10):
        assert collect_stats(readonly) == expected
    assert list(readonly.iterdump()) == before_dump
    assert readonly.total_changes == 0
    assert json.loads(json.dumps(expected)) == expected
    readonly.close()
    assert path.read_bytes() == before
