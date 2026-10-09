"""Synthetic regressions for structured Claude process-status provenance."""

import json

import pytest

from retryloom.adapters.claude import ClaudeAdapter
from retryloom.analysis.failures import detect_failure


def parse_results(tmp_path, blocks, extra=None):
    def record(kind, content):
        return {
            "type": kind,
            "sessionId": "exit-provenance-fixture",
            "cwd": "/workspace/example",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": kind, "content": content},
        }

    results = record("user", blocks)
    if extra is not None:
        results["toolUseResult"] = extra
    records = [
        record("user", "Run the checks."),
        record("assistant", [
            {"type": "tool_use", "id": identifier, "name": "Bash",
             "input": {"command": "pytest"}}
            for identifier in ("call-1", "call-2")
        ]),
        results,
    ]
    path = tmp_path / "exit-provenance.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")
    session = ClaudeAdapter(tmp_path).parse(path)
    outputs = [event for event in session.events if event.type == "tool_result"]
    assert len(outputs) == len(blocks)
    for event, block in zip(outputs, blocks):
        assert event.content == block["content"]
        assert event.metadata["source_record"] == results
        assert event.metadata["source_block"] == block
        assert event.metadata["tool_call_id"] == block["tool_use_id"]
    return session, outputs


def result(identifier="call-1", **fields):
    return dict(type="tool_result", tool_use_id=identifier,
                content="exit code 1\nOrdinary documentation output.\n", **fields)


def test_successful_literal_exit_text_is_not_process_failure(tmp_path):
    session, outputs = parse_results(tmp_path, [result(is_error=False)])
    assert outputs[0].exit_code is None
    assert outputs[0].metadata["is_error"] is False
    failure = detect_failure(session)
    assert failure is None or "test_failure" not in failure.signals


@pytest.mark.parametrize("is_error", [None, False, True])
@pytest.mark.parametrize("text", ["exit code 1", "Exit code: 7\n", "exited with code 42\nDetails"])
def test_output_text_and_error_flag_never_supply_numeric_status(tmp_path, is_error, text):
    block = result(is_error=is_error)
    block["content"] = text
    _, outputs = parse_results(tmp_path, [block])
    assert outputs[0].exit_code is None


@pytest.mark.parametrize("key", ["exit_code", "exitCode"])
def test_structured_zero_wins_over_literal_failure_text(tmp_path, key):
    session, outputs = parse_results(tmp_path, [result(is_error=False)], {key: 0})
    assert outputs[0].exit_code == 0
    failure = detect_failure(session)
    assert failure is None or "test_failure" not in failure.signals


@pytest.mark.parametrize("key", ["exit_code", "exitCode"])
def test_single_result_accepts_unambiguous_record_status(tmp_path, key):
    _, outputs = parse_results(tmp_path, [result()], {key: 23, "stdout": "unchanged\n"})
    assert outputs[0].exit_code == 23


@pytest.mark.parametrize("key", ["exit_code", "exitCode"])
def test_multiple_results_do_not_share_unassociated_record_status(tmp_path, key):
    extra = {key: 9, "stdout": "first\nsecond\n", "stderr": "raw error\n",
             "unknown_field": {"keep": True}}
    _, outputs = parse_results(tmp_path, [result(), result("call-2")], extra)
    assert [event.exit_code for event in outputs] == [None, None]
    assert all(event.metadata["source_record"]["toolUseResult"] == extra for event in outputs)


def test_each_block_keeps_its_own_structured_status(tmp_path):
    _, outputs = parse_results(tmp_path, [result(exit_code=0), result("call-2", exitCode=8)])
    assert [event.exit_code for event in outputs] == [0, 8]


@pytest.mark.parametrize("id_key", ["tool_use_id", "toolUseId", "call_id"])
@pytest.mark.parametrize("exit_key", ["exit_code", "exitCode"])
def test_explicit_record_id_applies_only_to_matching_result(tmp_path, id_key, exit_key):
    _, outputs = parse_results(tmp_path, [result(), result("call-2")],
                               {id_key: "call-2", exit_key: 6})
    assert [event.exit_code for event in outputs] == [None, 6]


@pytest.mark.parametrize("id_key", ["tool_use_id", "toolUseId", "call_id"])
def test_mismatched_record_id_is_not_assigned_even_to_single_result(tmp_path, id_key):
    _, outputs = parse_results(tmp_path, [result()], {id_key: "other-call", "exit_code": 4})
    assert outputs[0].exit_code is None


@pytest.mark.parametrize("block_fields,extra", [
    ({"exit_code": 0, "exitCode": 1}, None),
    ({}, {"exit_code": 0, "exitCode": 1}),
    ({"exit_code": 0}, {"tool_use_id": "call-1", "exitCode": 1}),
])
def test_conflicting_structured_status_remains_unknown(tmp_path, block_fields, extra):
    _, outputs = parse_results(tmp_path, [result(**block_fields)], extra)
    assert outputs[0].exit_code is None


def test_raw_stdout_stderr_and_output_are_preserved_exactly(tmp_path):
    text = "\r\nstdout: exit code 1\t\nUnicode: Ω\n"
    block = result(is_error=False)
    block["content"] = text
    extra = {"exit_code": 0, "stdout": "\r\nraw stdout\t", "stderr": "raw stderr\n"}
    _, outputs = parse_results(tmp_path, [block], extra)
    assert outputs[0].content == text
    assert outputs[0].metadata["source_record"]["toolUseResult"] == extra
    assert outputs[0].exit_code == 0
