"""Recorded-evidence acceptance cases for the Phase 7 evaluator."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from retryloom.replay.evaluator import evaluate
from retryloom.replay.models import CheckResult, ReplayReport, ReplayRun, RunResult


def report_fixture(control=0, retryloom=1, control_tokens=100, reflex_tokens=100, runs=3):
    check = dict(name="tests", command=["fixture-check"], required=True, timeout_seconds=60)
    manifest = dict(task="Perform the recorded task.", checks=[check], timeout_seconds=30,
                    agent="codex", agent_version="fixture-1", model="fixture-model",
                    model_settings={}, lesson_id="lesson-1", lesson_hash="a" * 64,
                    requested_base_commit="b" * 40, base_commit="b" * 40, runs=runs)
    manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    recorded_runs = []
    for arm, successes, tokens in (("CONTROL", control, control_tokens), ("REFLEX", retryloom, reflex_tokens)):
        for index in range(runs):
            success = index < successes
            total = tokens[index] if isinstance(tokens, list) else tokens
            recorded_runs.append(ReplayRun(
                id=f"{arm}-{index + 1}", condition=arm, run_number=index + 1, seed=index,
                status="COMPLETED" if success else "REQUIRED_CHECK_FAILED",
                statuses=["COMPLETED" if success else "REQUIRED_CHECK_FAILED"],
                result=RunResult(success=success, exit_code=0, token_total=total,
                                 elapsed_seconds=1, stdout="recorded", stderr="",
                                 agent="codex", agent_version="fixture-1", model="fixture-model",
                                 lesson_hash="a" * 64, efficiency_usable=True),
                checks=[CheckResult(name="tests", command=["fixture-check"], required=True,
                                    success=success, exit_code=0 if success else 1)],
                metadata=dict(manifest_hash=manifest_hash, base_commit="b" * 40,
                              tool_environment_keys=["HOME", "PATH"],
                              seed_policy="paired run index; PYTHONHASHSEED and RETRYLOOM_RUN_SEED"),
            ))
    return ReplayReport(id="report-1", case_id="case-1", status="COMPLETED", runs=recorded_runs,
                        metadata=dict(manifest=manifest, manifest_hash=manifest_hash,
                                      resolved_base_commit="b" * 40, efficiency_usable=True,
                                      runner=dict(agent="codex", agent_version="fixture-1",
                                                  model="fixture-model", model_settings={}),
                                      observed_agent_metadata=dict(agent="codex", agent_version="fixture-1",
                                                                   model="fixture-model")))


@pytest.mark.parametrize("control,retryloom", [(0, 1), (1, 2), (1, 3)])
def test_condition_a(control, retryloom):
    result = evaluate(report_fixture(control, retryloom))
    assert result.status == "replay_confirmed" and result.condition == "A"
    assert result.control_success_count == control
    assert result.reflex_success_count == retryloom


@pytest.mark.parametrize("tokens,confirmed", [(80, True), (85, True), (86, False)])
def test_condition_b_boundary(tokens, confirmed):
    result = evaluate(report_fixture(1, 1, 100, tokens))
    assert result.status == ("replay_confirmed" if confirmed else "rejected")
    assert result.condition == ("B" if confirmed else None)
    assert result.control_median_tokens == 100
    assert result.reflex_median_tokens == tokens
    assert result.token_reduction_ratio == pytest.approx((100 - tokens) / 100)


@pytest.mark.parametrize("control,retryloom", [(0, 0), (1, 0)])
def test_no_success_or_success_regression_cannot_confirm(control, retryloom):
    result = evaluate(report_fixture(control, retryloom, 100, 50))
    assert result.status == "rejected" and result.condition is None


def test_required_check_regression_blocks_success_improvement():
    report = report_fixture(1, 2)
    # REFLEX gains elsewhere but loses the same paired required check that CONTROL passed.
    report.runs[3].result.success = False
    report.runs[3].checks[0].success = False
    report.runs[3].checks[0].exit_code = 1
    report.runs[3].status = "REQUIRED_CHECK_FAILED"
    report.runs[3].statuses = ["REQUIRED_CHECK_FAILED"]
    report.runs[5].result.success = True
    report.runs[5].checks[0].success = True
    report.runs[5].checks[0].exit_code = 0
    report.runs[5].status = "COMPLETED"
    report.runs[5].statuses = ["COMPLETED"]
    result = evaluate(report)
    assert result.required_check_regression
    assert result.status == "rejected"


def test_optional_regression_is_recorded_without_blocking_improvement():
    report = report_fixture(0, 1)
    report.metadata["manifest"]["checks"].append(
        dict(name="lint", command=["fixture-lint"], required=False, timeout_seconds=60))
    manifest_hash = hashlib.sha256(json.dumps(report.metadata["manifest"], sort_keys=True).encode()).hexdigest()
    report.metadata["manifest_hash"] = manifest_hash
    for run in report.runs:
        run.metadata["manifest_hash"] = manifest_hash
        run.checks.append(CheckResult(name="lint", command=["fixture-lint"], required=False,
                                      success=run.condition == "CONTROL",
                                      exit_code=0 if run.condition == "CONTROL" else 1))
    result = evaluate(report)
    assert result.status == "replay_confirmed" and result.condition == "A"
    assert not result.required_check_regression
    assert "optional" in json.dumps(result.evidence).lower()


@pytest.mark.parametrize("control,retryloom,expected", [(1, 1, "INSUFFICIENT_EVIDENCE"), (0, 1, "replay_confirmed")])
def test_missing_tokens_only_prevents_condition_b(control, retryloom, expected):
    report = report_fixture(control, retryloom, None, None)
    result = evaluate(report)
    assert result.status == expected
    assert result.control_median_tokens is None and result.reflex_median_tokens is None
    assert result.token_reduction_ratio is None
    assert all(run.result.token_total is None for run in report.runs)


@pytest.mark.parametrize("arm", ["CONTROL", "REFLEX"])
def test_one_missing_token_is_not_silently_dropped(arm):
    report = report_fixture(1, 1, 100, 50)
    next(run for run in report.runs if run.condition == arm).result.token_total = None
    assert evaluate(report).status != "replay_confirmed"


@pytest.mark.parametrize("partial", ["timed_out", "agent_error", "efficiency_usable", "negative_tokens"])
def test_partial_or_invalid_metric_cannot_win_condition_b(partial):
    report = report_fixture(1, 1, 100, 1)
    run = report.runs[5]
    if partial == "timed_out":
        run.result.timed_out = True
        run.status = "TIMEOUT"
        run.statuses = ["TIMEOUT", "REQUIRED_CHECK_FAILED"]
    elif partial == "agent_error":
        run.result.agent_error = "agent crashed"
        run.result.exit_code = -9
        run.status = "AGENT_ERROR"
        run.statuses = ["AGENT_ERROR", "REQUIRED_CHECK_FAILED"]
    elif partial == "efficiency_usable":
        run.result.efficiency_usable = False
    else:
        run.result.token_total = -1
    assert evaluate(report).status != "replay_confirmed"


@pytest.mark.parametrize("status", ["INVALID_COMPARISON", "INVALID_LESSON_HASH", "BASELINE_NOT_REPRODUCIBLE"])
def test_phase6_rejection_propagates(status):
    report = report_fixture(0, 3)
    report.status = status
    report.reason = "Phase 6 integrity rejection"
    result = evaluate(report)
    assert result.status == ("BASELINE_NOT_REPRODUCIBLE" if status == "BASELINE_NOT_REPRODUCIBLE" else "INVALID_COMPARISON")
    assert result.condition is None


@pytest.mark.parametrize("attack", ["timeout", "hash", "base_commit", "seed", "check_command", "missing_run", "duplicate_run", "claimed_success"])
def test_fairness_and_evidence_attacks_are_rejected(attack):
    report = report_fixture(0, 3)
    target = report.runs[3]
    if attack == "timeout":
        target.metadata["timeout_seconds"] = 999
    elif attack == "hash":
        target.result.lesson_hash = "c" * 64
    elif attack == "base_commit":
        target.metadata["base_commit"] = "d" * 40
    elif attack == "seed":
        target.seed += 10
    elif attack == "check_command":
        target.checks[0].command = ["different-check"]
    elif attack == "missing_run":
        report.runs.pop()
    elif attack == "duplicate_run":
        report.runs[5].run_number = 1
    else:
        target.checks[0].success = False
        target.checks[0].exit_code = 1
    assert evaluate(report).status == "INVALID_COMPARISON"


def test_baseline_is_rechecked_even_if_report_claims_completed():
    result = evaluate(report_fixture(2, 3))
    assert result.status == "BASELINE_NOT_REPRODUCIBLE"


def test_metrics_use_all_complete_runs_in_correct_arm():
    result = evaluate(report_fixture(1, 1, [100, 1000, 90], [80, 70, 900]))
    assert result.control_success_count == result.reflex_success_count == 1
    assert result.control_median_tokens == 100 and result.reflex_median_tokens == 80
    assert result.condition == "B"


def test_two_runs_are_supported_with_strict_majority_baseline():
    assert evaluate(report_fixture(0, 1, runs=2)).condition == "A"
    assert evaluate(report_fixture(1, 2, runs=2)).status == "BASELINE_NOT_REPRODUCIBLE"


def test_even_run_medians_are_not_rounded_to_favorable_tokens():
    result = evaluate(report_fixture(1, 1, [99, 100, 101, 102], [85, 85, 86, 86], runs=4))
    assert result.control_median_tokens == 100.5
    assert result.reflex_median_tokens == 85.5
    assert result.status == "rejected"


def test_token_threshold_uses_exact_comparison_for_large_integer_totals():
    control_tokens = 2 ** 60
    reflex_tokens = control_tokens * 85 // 100 + 1
    assert evaluate(report_fixture(1, 1, control_tokens, reflex_tokens)).status == "rejected"


def test_inconsistent_complete_token_accounting_is_unavailable():
    report = report_fixture(1, 1, 100, 50)
    report.runs[3].result.token_input = 80
    report.runs[3].result.token_output = 20
    result = evaluate(report)
    assert result.status != "replay_confirmed"


def test_missing_required_check_evidence_does_not_count_as_success():
    report = report_fixture(0, 3)
    report.runs[3].checks = []
    assert evaluate(report).status in {"INVALID_COMPARISON", "INSUFFICIENT_EVIDENCE"}


def test_missing_replay_manifest_does_not_claim_fairness():
    report = report_fixture(0, 3)
    report.metadata = {}
    assert evaluate(report).status in {"INVALID_COMPARISON", "INSUFFICIENT_EVIDENCE"}


def test_recorded_report_level_efficiency_restriction_blocks_condition_b():
    report = report_fixture(1, 1, 100, 50)
    report.metadata["efficiency_usable"] = False
    assert evaluate(report).status != "replay_confirmed"


def test_required_check_timeout_cannot_hide_behind_complete_efficiency_flag():
    report = report_fixture(1, 1, 100, 50)
    report.runs[5].checks[0].timed_out = True
    report.runs[5].checks[0].exit_code = None
    assert evaluate(report).status != "replay_confirmed"


@pytest.mark.parametrize("field,value", [("timeout_seconds", 0), ("task", ""), ("lesson_hash", "not-a-sha256-hash"), ("agent_version", {"unknown": "format"})])
def test_malformed_manifest_does_not_crash_or_confirm(field, value):
    report = report_fixture(0, 3)
    report.metadata["manifest"][field] = value
    if field == "lesson_hash":
        for run in report.runs:
            run.result.lesson_hash = value
    manifest_hash = hashlib.sha256(json.dumps(report.metadata["manifest"], sort_keys=True).encode()).hexdigest()
    report.metadata["manifest_hash"] = manifest_hash
    for run in report.runs:
        run.metadata["manifest_hash"] = manifest_hash
    assert evaluate(report).status in {"INVALID_COMPARISON", "INSUFFICIENT_EVIDENCE"}


def test_unavailable_optional_agent_metadata_does_not_invent_a_mismatch():
    report = report_fixture(0, 1)
    for name in ("agent_version", "model"):
        report.metadata["manifest"][name] = None
        report.metadata["runner"][name] = None
        report.metadata[f"{name}_parity_verified"] = False
        for run in report.runs:
            setattr(run.result, name, None)
    report.metadata["observed_agent_metadata"] = {"agent": "codex"}
    manifest_hash = hashlib.sha256(json.dumps(report.metadata["manifest"], sort_keys=True).encode()).hexdigest()
    report.metadata["manifest_hash"] = manifest_hash
    for run in report.runs:
        run.metadata["manifest_hash"] = manifest_hash
    result = evaluate(report)
    assert result.status == "replay_confirmed" and result.condition == "A"
    assert "unknown" in json.dumps(result.evidence).lower()


@pytest.mark.parametrize("repetitions", [3, 10])
def test_deterministic_evaluation_release_fixture(repetitions):
    report = report_fixture(1, 1, [100, 1000, 90], [80, 70, 900])
    original = report.model_dump_json()
    expected = evaluate(report).model_dump()
    assert all(evaluate(report).model_dump() == expected for _ in range(repetitions))
    assert report.model_dump_json() == original


def test_real_engine_recorded_report_evaluates_and_persists_without_reruns(tmp_path):
    from retryloom.replay.engine import ReplayEngine
    from retryloom.replay.injection import FrozenLesson
    from retryloom.replay.models import ReplayCase, ReplayCheck
    from retryloom.storage.database import connect
    from retryloom.storage.repository import load_replay_report, save_evaluation

    source = tmp_path / "source"
    source.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (source / "base").write_text("base")
    git("add", "base")
    git("commit", "-qm", "base")
    lesson = FrozenLesson.freeze("Run relevant checks after changes.")
    database = connect(tmp_path / "retryloom.db")
    database.execute("INSERT INTO lessons(id,scope,trigger_json,rule,evidence_sessions_json,confidence,status,lesson_hash) VALUES (?,?,?,?,?,?,?,?)",
                     ("lesson-1", "repository", "{}", lesson.text, "[]", 0.5, "candidate", lesson.lesson_hash))
    database.commit()
    case = ReplayCase(id="case-1", repository=source, base_commit=git("rev-parse", "HEAD"),
                      task="Run the fixture task.", lesson_id="lesson-1", lesson_hash=lesson.lesson_hash,
                      checks=[ReplayCheck(command=[sys.executable, "-c", "from pathlib import Path; import sys; sys.exit(0 if Path('outcome').exists() else 1)"])])
    class Runner:
        calls = 0
        def run(self, task, cwd, instructions, timeout_seconds, env):
            self.calls += 1
            if instructions:
                (cwd / "outcome").write_text("done")
            return RunResult(success=True, exit_code=0, elapsed_seconds=0.1, stdout="fixture", stderr="")
    runner = Runner()
    report = ReplayEngine(runner, database, env={"PATH":os.environ.get("PATH", "")}, temp_parent=tmp_path / "runs").execute(case, lesson)
    assert report.status == "COMPLETED" and runner.calls == 6
    stored = load_replay_report(database, report.id)
    assert stored.model_dump() == report.model_dump()
    before = {table: [tuple(row) for row in database.execute(f"SELECT * FROM {table}")] for table in
              ("replay_cases", "replay_checks", "replay_reports", "replay_runs", "replay_check_results", "lessons")}
    result = evaluate(stored)
    assert result.status == "replay_confirmed" and result.condition == "A"
    save_evaluation(database, stored, result)
    after = {table: [tuple(row) for row in database.execute(f"SELECT * FROM {table}")] for table in before}
    assert before == after and runner.calls == 6
    assert database.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    database.close()
