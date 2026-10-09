import json

import pytest

from retryloom.adapters.codex import CodexAdapter


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
def test_codex_normalization_preserves_records(tmp_path, suffix):
    records = [
        {"type": "session_meta", "payload": {"id": "abc", "cwd": "/repo", "timestamp": "2026-01-01T00:00:00Z", "cli_version": "fixture", "git": {"commit_hash": "deadbeef"}}},
        {"type": "turn_context", "payload": {"model": "fixture-model"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "fix tests"}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "call1", "arguments": '{"cmd":"pytest"}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call1", "output": "failed"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}}}},
        {"type": "future_record", "payload": {"unknown": "preserved"}},
    ]
    path = tmp_path / ("session" + suffix)
    path.write_text(json.dumps(records) if suffix == ".json" else "\n".join(map(json.dumps, records)))
    adapter = CodexAdapter(tmp_path)
    session = adapter.parse(path)
    assert adapter.discover() == [path]
    assert session.agent == "codex"
    assert session.task == "fix tests"
    assert session.model == "fixture-model"
    assert session.base_commit == "deadbeef"
    assert session.started_at.utcoffset().total_seconds() == 0
    assert session.token_input == 100 and session.token_output == 20
    assert session.tool_call_count == 1
    assert session.events[3].command == "pytest"
    assert [e.metadata["source_record"] for e in session.events] == records
    assert session.ended_at is None


def test_codex_rejects_missing_required_metadata(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"type":"unknown"}')
    with pytest.raises(ValueError, match="session_meta"):
        CodexAdapter().parse(path)


def test_actual_rollout_record_shapes_are_normalized():
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "codex_rollout_format.jsonl"
    session = CodexAdapter().parse(path)

    assert session.agent_version == "fixture-version"
    assert session.model == "fixture-model"
    assert session.task == "Update the example."
    assert session.repository == "/workspace/project"
    assert session.base_commit == "0123456789abcdef"
    assert (session.token_input, session.token_output) == (123, 45)
    assert session.tool_call_count == 1
    call_event, output_event = session.events[3:5]
    assert call_event.command == "pytest"
    assert output_event.exit_code == 0
    assert "Process exited with code 0" in output_event.content
    assert output_event.metadata["source_record"]["payload"]["call_id"] == "call-1"


def test_unknown_or_missing_optional_fields_remain_nonfatal(tmp_path):
    path = tmp_path / "sparse.jsonl"
    path.write_text(
        '{"type":"session_meta","payload":{"cwd":"/repo","timestamp":"2026-10-07T10:00:00Z"}}\n'
        '{"type":"turn_context","payload":{"unknown_field":true}}\n'
        '{"type":"response_item","payload":{"type":"custom_tool_call_output","output":[{"text":"no exit status"}]}}\n'
    )
    session = CodexAdapter(tmp_path).parse(path)
    assert session.agent == "codex"
    assert session.model is None
    assert session.base_commit is None
    assert session.token_input is None
    assert session.events[-1].exit_code is None
