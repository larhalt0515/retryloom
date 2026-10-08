import os
import sys

import pytest

from reflex.replay.injection import FrozenLesson, instructions_for
from reflex.replay.runner import CodexRunner


def fixture_cli(tmp_path, body):
    executable = tmp_path / "codex-fixture"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    return str(executable)


def test_runner_executes_cli_fixture_with_explicit_env(tmp_path):
    executable = fixture_cli(tmp_path, 'import os,sys\nassert sys.argv[1:] == ["exec","--json","--ephemeral","--sandbox","workspace-write","-"]\nassert os.environ["GATE_VALUE"] == "explicit"\nassert "HOME" not in os.environ\nprint(sys.stdin.read())\nprint("stderr fixture",file=sys.stderr)\n')
    result = CodexRunner(executable).run("harmless task", tmp_path, ["fixture instruction"], 5, {"GATE_VALUE":"explicit"})
    assert result.success
    assert result.stdout == "harmless task\n\nfixture instruction\n"
    assert result.stderr == "stderr fixture\n"
    assert result.exit_code == 0
    for name in ("token_input","token_output","token_total","tool_calls","files_changed","lines_added","lines_removed"):
        assert getattr(result, name) is None


def test_runner_timeout_captures_output(tmp_path):
    executable = fixture_cli(tmp_path, 'import time,sys\nprint("started",flush=True)\nprint("error stream",file=sys.stderr,flush=True)\ntime.sleep(30)\n')
    result = CodexRunner(executable).run("fixture", tmp_path, [], 1, {})
    assert result.timed_out and not result.success
    assert "started" in result.stdout and "error stream" in result.stderr


def test_injection_preserves_frozen_lesson_without_historical_context():
    historical = {"correction_diff":"SECRET DIFF", "final_patch":"SECRET PATCH"}
    lesson = FrozenLesson.freeze("Run relevant tests after changes.")
    before = (lesson.text, lesson.lesson_hash)
    assert instructions_for("CONTROL", lesson) == []
    assert instructions_for("REFLEX", lesson) == [lesson.text]
    assert (lesson.text, lesson.lesson_hash) == before
    assert all(value not in "".join(instructions_for("REFLEX", lesson)) for value in historical.values())
    with pytest.raises(ValueError, match="hash mismatch"):
        instructions_for("REFLEX", FrozenLesson("tampered", lesson.lesson_hash))
