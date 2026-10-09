"""Phase 6 acceptance tests exercise real Git clones with controlled agents."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from pydantic import ValidationError

from retryloom.git.repository import repository_state
from retryloom.replay.engine import ReplayEngine
from retryloom.replay.injection import FrozenLesson
from retryloom.replay.models import ReplayCase, ReplayCheck, RunResult
from retryloom.replay.runner import CodexRunner
from retryloom.storage.database import connect


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def replay_fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "tracked.txt").write_text("base\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-qm", "base")
    commit = git(source, "rev-parse", "HEAD").strip()
    (source / "tracked.txt").write_text("staged\n")
    git(source, "add", "tracked.txt")
    (source / "tracked.txt").write_text("unstaged\n")
    (source / "untracked.txt").write_text("original untracked\n")
    lesson = FrozenLesson.freeze("Run relevant checks after making a change.")
    database = connect(tmp_path / "retryloom.db")
    database.execute(
        "INSERT INTO lessons(id,scope,trigger_json,rule,evidence_sessions_json,confidence,status,lesson_hash,leak_suspect) VALUES (?,?,?,?,?,?,?,?,?)",
        ("lesson-1", "repository", "{}", lesson.text, "[]", 0.5, "candidate", lesson.lesson_hash, 0),
    )
    database.commit()
    case = ReplayCase(
        id="case-1", repository=source, base_commit=commit, task="Perform the fixture task.",
        checks=[ReplayCheck(name="deterministic", command=[sys.executable, "-c", "print('checked')"])],
        runs=3, lesson_id="lesson-1", lesson_hash=lesson.lesson_hash, timeout_seconds=5,
    )
    yield source, case, lesson, database, tmp_path
    database.close()


class FixtureRunner:
    def __init__(self, controls, reflexes=(True, True, True), callback=None):
        self.controls = list(controls)
        self.reflexes = list(reflexes)
        self.calls = []
        self.callback = callback

    def run(self, task, cwd, instructions, timeout_seconds, env):
        condition = "REFLEX" if instructions else "CONTROL"
        number = sum(c["condition"] == condition for c in self.calls)
        self.calls.append(dict(condition=condition, task=task, cwd=cwd, instructions=list(instructions),
                               timeout_seconds=timeout_seconds, env=dict(env),
                               head=git(cwd, "rev-parse", "HEAD").strip(),
                               origin=git(cwd, "remote")))
        (cwd / "tracked.txt").write_text("clone-only edit\n")
        git(cwd, "add", "tracked.txt")
        if self.callback:
            self.callback(self, cwd, instructions, env)
        good = (self.reflexes if instructions else self.controls)[number]
        return RunResult(success=good, exit_code=0 if good else 1,
                         stdout="fixture stdout", stderr="fixture stderr", elapsed_seconds=0.01)


def engine(fixture, runner):
    source, case, lesson, database, tmp_path = fixture
    return ReplayEngine(runner=runner, connection=database,
                        env={"PATH":os.environ.get("PATH", ""), "PYTHONHASHSEED":"0", "BASELINE_VALUE":"equal"},
                        temp_parent=tmp_path / "temporary")


def test_replay_case_rejects_single_run(replay_fixture):
    _, case, _, _, _ = replay_fixture
    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        ReplayCase.model_validate({**case.model_dump(), "runs": 1})


@pytest.mark.parametrize("runs", [2, 3])
def test_replay_case_accepts_multiple_runs(replay_fixture, runs):
    _, case, _, _, _ = replay_fixture
    assert ReplayCase.model_validate({**case.model_dump(), "runs": runs}).runs == runs


def test_replay_case_defaults_to_three_runs(replay_fixture):
    _, case, _, _, _ = replay_fixture
    assert ReplayCase.model_validate(case.model_dump(exclude={"runs"})).runs == 3


@pytest.mark.parametrize("controls,reflexes,expected", [
    ((True,False,False),(True,True,True),"COMPLETED"),
    ((False,False,False),(True,True,True),"COMPLETED"),
    ((True,False,False),(False,False,False),"COMPLETED"),
    ((False,False,False),(False,False,False),"COMPLETED"),
    ((True,True,False),(True,True,True),"BASELINE_NOT_REPRODUCIBLE"),
])
def test_combinations_and_baseline_gate(replay_fixture, controls, reflexes, expected):
    _, case, lesson, _, _ = replay_fixture
    runner = FixtureRunner(controls, reflexes)
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status == expected
    assert len(report.runs) == (3 if expected == "BASELINE_NOT_REPRODUCIBLE" else 6)
    assert sum(r.result.success for r in report.runs if r.condition == "CONTROL") == sum(controls)
    if expected == "COMPLETED":
        assert sum(r.result.success for r in report.runs if r.condition == "REFLEX") == sum(reflexes)


def test_independence_fairness_injection_and_persistence(replay_fixture):
    source, case, lesson, database, _ = replay_fixture
    before = repository_state(source)
    runner = FixtureRunner((False,False,False))
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status == "COMPLETED"
    assert repository_state(source) == before
    assert len({str(c["cwd"]) for c in runner.calls}) == 6
    assert len({c["env"]["HOME"] for c in runner.calls}) == 6
    for call in runner.calls:
        assert call["head"] == case.base_commit
        assert not call["origin"].strip()
        assert call["task"] == case.task and call["timeout_seconds"] == 5
        assert call["instructions"] == ([lesson.text] if call["condition"] == "REFLEX" else [])
        assert call["env"]["BASELINE_VALUE"] == "equal"
        for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "CODEX_HOME"):
            assert not Path(call["env"][name]).exists()
        assert not call["cwd"].exists()
    lesson.verify()
    rows = database.execute("SELECT * FROM replay_runs").fetchall()
    assert len(rows) == 6
    for row in rows:
        assert row["token_total"] is None and row["tool_calls"] is None
        assert row["stdout"] == "fixture stdout" and row["stderr"] == "fixture stderr"
    assert database.execute("SELECT status FROM lessons WHERE id='lesson-1'").fetchone()[0] == "candidate"


@pytest.mark.parametrize("required", [True, False])
def test_required_and_optional_check_failure(replay_fixture, required):
    _, case, lesson, _, _ = replay_fixture
    checks = [ReplayCheck(name="always-pass", command=[sys.executable,"-c","print('ok')"]),
              ReplayCheck(name="fails", required=required, command=[sys.executable,"-c","import sys; print('failed check'); sys.exit(1)"])]
    case = case.model_copy(update={"checks":checks})
    report = engine(replay_fixture, FixtureRunner((True,True,True))).execute(case, lesson)
    assert report.status == ("COMPLETED" if required else "BASELINE_NOT_REPRODUCIBLE")
    assert all(r.result.success is (not required) for r in report.runs)
    assert all(any(not c.success and "failed check" in c.stdout for c in r.checks) for r in report.runs)


def test_timeout_mismatch_is_rejected_without_running(replay_fixture):
    _, case, lesson, _, _ = replay_fixture
    case = case.model_copy(update={"control_timeout_seconds":5, "reflex_timeout_seconds":6})
    runner = FixtureRunner((False,False,False))
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"
    assert not runner.calls


@pytest.mark.parametrize("when", ["before", "during"])
def test_hash_mutation_invalidates_comparison(replay_fixture, when):
    _, case, lesson, _, _ = replay_fixture
    if when == "before":
        lesson = FrozenLesson("different", lesson.lesson_hash)
    def mutate(runner, cwd, instructions, env):
        object.__setattr__(lesson, "text", "changed")
    runner = FixtureRunner((False,False,False), callback=mutate if when == "during" else None)
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status in {"INVALID_LESSON_HASH", "INVALID_COMPARISON"}
    assert len(runner.calls) <= 1


def test_cleanup_warning_preserves_original(replay_fixture, monkeypatch):
    source, case, lesson, _, _ = replay_fixture
    before = repository_state(source)
    monkeypatch.setattr("retryloom.replay.isolation.OwnedDirectory.cleanup", lambda self:"forced cleanup failure")
    report = engine(replay_fixture, FixtureRunner((False,False,False))).execute(case, lesson)
    assert "CLEANUP_WARNING" in json.dumps(report.model_dump(), default=str)
    assert "forced cleanup failure" in json.dumps(report.model_dump(), default=str)
    assert repository_state(source) == before


def test_agent_exception_is_explicit(replay_fixture):
    class CrashingRunner:
        def run(self, *args, **kwargs):
            raise OSError("fixture spawn crash")
    _, case, lesson, _, _ = replay_fixture
    report = engine(replay_fixture, CrashingRunner()).execute(case, lesson)
    assert report.runs
    assert all(not r.result.success for r in report.runs)
    assert "AGENT_ERROR" in json.dumps(report.model_dump(), default=str)


def test_runner_timeout_and_crash_preserve_output(tmp_path):
    script = tmp_path / "agent"
    script.write_text(f"#!{sys.executable}\nimport sys,time\nprint('partial output',flush=True)\nprint('partial error',file=sys.stderr,flush=True)\ntime.sleep(30)\n")
    script.chmod(0o700)
    timeout = CodexRunner(str(script)).run("task", tmp_path, [], 1, {})
    assert timeout.timed_out and not timeout.success
    assert "partial output" in timeout.stdout and "partial error" in timeout.stderr
    script.write_text(f"#!{sys.executable}\nimport sys\nprint('crash output')\nprint('crash error',file=sys.stderr)\nsys.exit(7)\n")
    crash = CodexRunner(str(script)).run("task", tmp_path, [], 5, {})
    assert not crash.success and crash.exit_code == 7
    assert "crash output" in crash.stdout and "crash error" in crash.stderr


def test_checks_repeat_deterministically(replay_fixture):
    _, case, lesson, _, _ = replay_fixture
    report = engine(replay_fixture, FixtureRunner((False,False,False))).execute(case, lesson)
    assert len(report.runs) == 6
    assert len({r.checks[0].stdout for r in report.runs}) == 1
    assert all(r.checks[0].success for r in report.runs)


def test_historical_context_and_control_output_not_injected(replay_fixture):
    _, case, lesson, _, _ = replay_fixture
    class HistoricalRunner(FixtureRunner):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            result.stdout = "CONTROL PRIVATE OUTPUT; SECRET FINAL PATCH; SECRET CORRECTION DIFF"
            return result
    runner = HistoricalRunner((False,False,False))
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status == "COMPLETED"
    for call in runner.calls:
        assert call["task"] == case.task
        assert call["instructions"] == ([lesson.text] if call["condition"] == "REFLEX" else [])
        assert "SECRET" not in call["task"] + " ".join(call["instructions"])


def test_mutated_original_state_invalidates_report(replay_fixture):
    source, case, lesson, _, _ = replay_fixture
    def mutate(runner, cwd, instructions, env):
        (source / "untracked.txt").write_text("external change detected")
    report = engine(replay_fixture, FixtureRunner((False,False,False),callback=mutate)).execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"


def test_environment_baseline_does_not_leak_git_overrides(replay_fixture):
    source, case, lesson, database, tmp_path = replay_fixture
    runner = FixtureRunner((False,False,False))
    replay = ReplayEngine(runner=runner, connection=database,
                          env={"PATH":os.environ.get("PATH", ""), "GIT_DIR":str(source / ".git"),
                               "GIT_WORK_TREE":str(source), "CODEX_HOME":str(source)},
                          temp_parent=tmp_path / "temporary")
    report = replay.execute(case, lesson)
    assert report.status == "COMPLETED"
    assert all("GIT_DIR" not in c["env"] and "GIT_WORK_TREE" not in c["env"] for c in runner.calls)
    assert all(c["env"]["CODEX_HOME"] != str(source) for c in runner.calls)


def test_later_solution_commits_are_not_visible(replay_fixture):
    source, case, lesson, _, _ = replay_fixture
    (source / "hidden-answer.txt").write_text("historical final implementation")
    git(source, "add", "hidden-answer.txt")
    git(source, "commit", "-qm", "later solution")
    later = git(source, "rev-parse", "HEAD").strip()
    before = repository_state(source)
    def inspect(runner, cwd, instructions, env):
        assert not (cwd / "hidden-answer.txt").exists()
        result = subprocess.run(["git", "cat-file", "-e", later],cwd=cwd,capture_output=True)
        assert result.returncode != 0
        assert git(cwd, "rev-list", "--all").splitlines() == [case.base_commit]
        assert not (cwd / ".git" / "objects" / "info" / "alternates").exists()
    report = engine(replay_fixture, FixtureRunner((False,False,False),callback=inspect)).execute(case, lesson)
    assert report.status == "COMPLETED"
    assert repository_state(source) == before


def test_agent_timeout_is_recorded_and_partial_metrics_unusable(replay_fixture):
    class TimedRunner:
        def run(self, *args, **kwargs):
            return RunResult(success=False, timed_out=True, exit_code=-9,
                             stdout="partial", stderr="partial error", elapsed_seconds=1,token_total=7)
    _, case, lesson, _, _ = replay_fixture
    report = engine(replay_fixture, TimedRunner()).execute(case, lesson)
    assert "TIMEOUT" in json.dumps(report.model_dump(),default=str)
    assert all(r.result.timed_out and not r.result.success for r in report.runs)
    assert all(not r.result.efficiency_usable for r in report.runs)


def test_reported_model_mismatch_is_invalid(replay_fixture):
    _, case, lesson, _, _ = replay_fixture
    class DifferentModelRunner(FixtureRunner):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            result.model = "unexpected-model"
            return result
    case = case.model_copy(update={"model":"expected-model"})
    report = engine(replay_fixture, DifferentModelRunner((False,False,False))).execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"


def test_nested_process_group_timeout_preserves_logs(tmp_path):
    script = tmp_path / "nested-agent"
    script.write_text(
        f"#!{sys.executable}\nimport subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c',"
        "\"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print('descendant',flush=True); time.sleep(30)\"])\n"
        "print('parent',flush=True)\nprint('nested error',file=sys.stderr,flush=True)\ntime.sleep(30)\n"
    )
    script.chmod(0o700)
    result = CodexRunner(str(script)).run("task", tmp_path, [], 1, {})
    assert result.timed_out and not result.success
    assert "parent" in result.stdout and "descendant" in result.stdout
    assert "nested error" in result.stderr
    assert result.elapsed_seconds < 7


def test_original_cannot_be_temporary_parent(replay_fixture):
    source, case, lesson, database, _ = replay_fixture
    before = repository_state(source)
    runner = FixtureRunner((False,False,False))
    replay = ReplayEngine(runner=runner, connection=database, env={}, temp_parent=source / "unsafe-temp")
    report = replay.execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"
    assert not runner.calls
    assert repository_state(source) == before


@pytest.mark.parametrize("text", ["Use line 84 to fix it.", "Change L84.", "```python\nanswer\n```"])
def test_mechanically_rejected_lesson_never_runs(replay_fixture, text):
    _, case, _, _, _ = replay_fixture
    lesson = FrozenLesson.freeze(text)
    case = case.model_copy(update={"lesson_hash":lesson.lesson_hash})
    runner = FixtureRunner((False,False,False))
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status != "COMPLETED"
    assert not runner.calls


def test_cleanup_refuses_unknown_replacements_and_preserves_siblings(tmp_path):
    from retryloom.replay.isolation import OwnedDirectory
    parent = tmp_path / "parent"
    parent.mkdir()
    unrelated = parent / "keep"
    unrelated.mkdir()
    (unrelated / "important").write_text("preserve")
    owned = OwnedDirectory.create(parent)
    moved = parent / "moved-owned"
    owned.path.rename(moved)
    owned.path.symlink_to(unrelated, target_is_directory=True)
    assert owned.cleanup() is not None
    assert (unrelated / "important").read_text() == "preserve"
    assert moved.exists()


def test_check_timeout_recorded_as_required_failure(replay_fixture):
    _, case, lesson, _, _ = replay_fixture
    case = case.model_copy(update={"runs":2,"checks":[ReplayCheck(
        name="slow",command=[sys.executable,"-c","import time; print('check partial',flush=True); time.sleep(30)"],timeout_seconds=1)]})
    report = engine(replay_fixture, FixtureRunner((True,True))).execute(case, lesson)
    assert not report.runs[0].result.success
    assert report.runs[0].checks[0].timed_out
    assert "check partial" in report.runs[0].checks[0].stdout
    assert "REQUIRED_CHECK_FAILED" in json.dumps(report.model_dump(), default=str)


def test_missing_stored_lesson_fails_gracefully(replay_fixture):
    _, case, lesson, _, _ = replay_fixture
    case = case.model_copy(update={"lesson_id":"missing-lesson"})
    runner = FixtureRunner((False,False,False))
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"
    assert not runner.calls


def test_stored_lesson_rewrite_invalidates_comparison(replay_fixture):
    _, case, lesson, database, _ = replay_fixture
    def mutate(runner, cwd, instructions, env):
        database.execute("UPDATE lessons SET rule='rewrite' WHERE id='lesson-1'")
        database.commit()
    report = engine(replay_fixture, FixtureRunner((False,False,False),callback=mutate)).execute(case, lesson)
    assert report.status in {"INVALID_COMPARISON", "INVALID_LESSON_HASH"}
    assert len(report.runs) == 1


def test_suspect_lesson_is_blocked(replay_fixture):
    _, case, lesson, database, _ = replay_fixture
    database.execute("UPDATE lessons SET leak_suspect=1 WHERE id='lesson-1'")
    database.commit()
    runner = FixtureRunner((False,False,False))
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"
    assert not runner.calls


def test_nonadministrative_in_source_database_is_rejected(replay_fixture):
    source, case, lesson, _, tmp_path = replay_fixture
    db_path = source / "user.db"
    database = connect(db_path)
    before = db_path.read_bytes()
    runner = FixtureRunner((False,False,False))
    report = ReplayEngine(runner, database, env={}, temp_parent=tmp_path / "tmp").execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"
    assert db_path.read_bytes() == before
    assert not runner.calls
    database.close()


def test_only_ignored_retryloom_database_is_exempt_from_source_manifest(replay_fixture):
    source, case, lesson, original_db, tmp_path = replay_fixture
    (source / ".gitignore").write_text(".retryloom/\n")
    db_path = source / ".retryloom" / "retryloom.db"
    database = connect(db_path)
    row = original_db.execute("SELECT * FROM lessons WHERE id='lesson-1'").fetchone()
    database.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?)", tuple(row))
    database.commit()
    exemptions = tuple(Path(str(db_path) + suffix) for suffix in ("", "-wal", "-shm", "-journal"))
    before = repository_state(source, excluded_paths=exemptions)
    report = ReplayEngine(FixtureRunner((False,False,False)), database,
                          env={"PATH":os.environ.get("PATH", "")}, temp_parent=tmp_path / "tmp").execute(case, lesson)
    assert report.status == "COMPLETED"
    assert repository_state(source, excluded_paths=exemptions) == before
    assert database.execute("SELECT COUNT(*) FROM replay_runs").fetchone()[0] == 6
    database.close()


def test_preparation_failure_cleans_owned_temporary_state(replay_fixture, monkeypatch):
    source, case, lesson, _, tmp_path = replay_fixture
    before = repository_state(source)
    def fail(*args, **kwargs):
        raise OSError("preparation failed")
    monkeypatch.setattr("retryloom.replay.engine.create_clone", fail)
    runner = FixtureRunner((False,False,False))
    report = engine(replay_fixture, runner).execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"
    assert not runner.calls
    assert not list((tmp_path / "temporary").iterdir())
    assert repository_state(source) == before


def test_end_to_end_codex_cli_fixture(replay_fixture):
    source, case, lesson, database, tmp_path = replay_fixture
    script = tmp_path / "codex-fixture"
    script.write_text(
        f"#!{sys.executable}\nimport os,sys\n"
        "assert sys.argv[1:] == ['exec','--json','--ephemeral','--sandbox','workspace-write','-']\n"
        "prompt=sys.stdin.read()\nprint('fixture stdout')\nprint('fixture stderr',file=sys.stderr)\n"
        f"sys.exit(0 if {lesson.text!r} in prompt else 1)\n"
    )
    script.chmod(0o700)
    before = repository_state(source)
    report = engine(replay_fixture, CodexRunner(str(script))).execute(case, lesson)
    assert report.status == "COMPLETED"
    assert sum(r.result.success for r in report.runs if r.condition == "CONTROL") == 0
    assert sum(r.result.success for r in report.runs if r.condition == "REFLEX") == 3
    assert repository_state(source) == before
    assert all("fixture stdout" in r.result.stdout for r in report.runs)
    assert all("fixture stderr" in r.result.stderr for r in report.runs)
    assert database.execute("SELECT COUNT(*) FROM replay_runs").fetchone()[0] == 6


def test_hardlinked_administrative_database_is_rejected(replay_fixture):
    source, case, lesson, _, tmp_path = replay_fixture
    (source / ".gitignore").write_text(".retryloom/\n")
    database = connect(source / ".retryloom" / "retryloom.db")
    os.link(source / ".retryloom" / "retryloom.db", tmp_path / "db-alias")
    before = (tmp_path / "db-alias").read_bytes()
    runner = FixtureRunner((False,False,False))
    report = ReplayEngine(runner, database, env={},temp_parent=tmp_path / "temp").execute(case, lesson)
    assert report.status == "INVALID_COMPARISON"
    assert not runner.calls
    assert (tmp_path / "db-alias").read_bytes() == before
    database.close()


def test_available_raw_metrics_are_persisted_without_rewriting(replay_fixture):
    _, case, lesson, database, _ = replay_fixture
    class MeasuredRunner(FixtureRunner):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            return result.model_copy(update={"token_input":100,"token_output":20,"token_total":120,
                                             "tool_calls":2,"files_changed":1,"lines_added":3,"lines_removed":2})
    report = engine(replay_fixture, MeasuredRunner((False,False,False))).execute(case, lesson)
    assert report.status == "COMPLETED"
    rows = database.execute("SELECT token_input,token_output,token_total,tool_calls,files_changed,lines_added,lines_removed FROM replay_runs").fetchall()
    assert len(rows) == 6
    assert all(tuple(row) == (100,20,120,2,1,3,2) for row in rows)
