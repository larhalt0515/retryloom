"""Independent adapter acceptance checks using invented transcript data."""
import json
import pytest

from reflex.adapters.claude import ClaudeAdapter
from reflex.models import Session
from reflex.analysis.failures import detect_failure


def transcript(tmp_path, records):
    path = tmp_path / "fixture.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records))
    return path


def record(kind, content, **extra):
    return dict(type=kind, sessionId="review-fixture", cwd="/workspace/example",
                timestamp="2026-01-01T00:00:00Z",
                message=dict(role=kind, content=content), **extra)


def test_command_result_preserves_failure_and_correction_evidence(tmp_path):
    records = [
        record("user", "Repair the test failure."),
        record("assistant", [{"type": "tool_use", "id": "test-1", "name": "Bash",
                              "input": {"command": "pytest"}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "test-1",
                         "content": "Exit code 1\n1 failed", "is_error": True, "exit_code": 1}]),
        record("user", "Please fix the missing validation instead of ignoring it."),
    ]
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, records))
    assert session.task == records[0]["message"]["content"]
    assert session.tool_call_count == 1
    outputs = [event for event in session.events if event.type == "tool_result"]
    assert len(outputs) == 1
    assert outputs[0].command == "pytest"
    assert outputs[0].exit_code == 1
    failure = detect_failure(session)
    assert failure is not None
    assert "test_failure" in failure.signals
    assert "explicit_user_correction" in failure.signals
    assert all(event.metadata.get("source_record") for event in session.events)
    assert Session.model_validate_json(session.model_dump_json()) == session


def test_unknown_completion_is_not_claimed_as_success(tmp_path):
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, [
        record("user", "Repair the parser."),
        record("assistant", [{"type": "text", "text": "The parser looks good now."}]),
    ]))
    assert session.result is None
    assert session.base_commit is None
    assert session.token_input is None and session.token_output is None
    assert session == ClaudeAdapter(tmp_path).parse(tmp_path / "fixture.jsonl")


@pytest.mark.parametrize("malformed", [
    {"type": []},
    {"type": "assistant", "message": {"id": {}, "usage": {"input_tokens": 1}}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": [], "name": "Bash"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": []}]}},
])
def test_malformed_optional_record_does_not_erase_valid_task(tmp_path, malformed):
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, [
        record("user", "Repair the parser."), malformed,
    ]))
    assert session.task == "Repair the parser."


def test_non_shell_result_text_is_not_shell_exit_evidence(tmp_path):
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, [
        record("user", "Inspect the manual."),
        record("assistant", [{"type": "tool_use", "id": "read-1", "name": "Read",
                              "input": {"file_path": "/workspace/example/manual.txt"}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "read-1",
                         "content": "The manual says: Exit code 42 means an input error."}]),
    ]))
    output = next(event for event in session.events if event.type == "tool_result")
    assert output.exit_code is None


def test_observed_edit_data_preserves_correction_sequence(tmp_path):
    from reflex.analysis.corrections import extract_corrections

    records = [
        record("user", "Repair the failing tests."),
        record("assistant", [{"type": "tool_use", "id": "test-1", "name": "Bash", "input": {"command": "pytest"}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "test-1", "content": "Exit code 1", "is_error": True, "exit_code": 1}]),
        record("assistant", [{"type": "tool_use", "id": "edit-1", "name": "Edit", "input": {"file_path": "/workspace/example/parser.py", "old_string": "old", "new_string": "new"}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "edit-1", "content": "Updated", "is_error": False}], toolUseResult={"filePath": "/workspace/example/parser.py", "oldString": "old", "newString": "new", "structuredPatch": [{"oldStart": 1, "lines": ["-old", "+new"]}]}),
        record("assistant", [{"type": "tool_use", "id": "test-2", "name": "Bash", "input": {"command": "pytest"}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "test-2", "content": "Passed", "is_error": False}], toolUseResult={"exit_code": 0}),
    ]
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, records))
    failure = detect_failure(session)
    assert failure is not None
    evidence = extract_corrections(session, failure)
    assert len(evidence) == 1
    assert evidence[0].failure_type == "test_failure"
    assert evidence[0].source_before == "old"


def test_missing_distinct_message_usage_does_not_claim_complete_total(tmp_path):
    records = [record("user", "Repair the parser."),
               record("assistant", [{"type": "text", "text": "First turn"}]),
               record("assistant", [{"type": "text", "text": "Second turn"}])]
    records[1]["message"].update(id="message-1", usage={"input_tokens": 10, "output_tokens": 3})
    records[2]["message"]["id"] = "message-2"
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, records))
    assert session.token_input is None
    assert session.token_output is None


def test_missing_repository_does_not_use_current_git_head(tmp_path):
    records = [record("user", "Repair the parser.")]
    del records[0]["cwd"]
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, records))
    assert session.repository == ""
    assert session.base_commit is None


def test_valid_record_with_bad_blocks_preserves_source(tmp_path):
    source = record("user", [42])
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, [
        record("user", "Repair the parser."), source,
    ]))
    assert any(event.metadata.get("source_record") == source for event in session.events)


def test_discovery_does_not_follow_external_links(tmp_path):
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    transcript(outside, [record("user", "Invented task.")])
    (source / "external").symlink_to(outside, target_is_directory=True)
    (source / "linked.jsonl").symlink_to(outside / "fixture.jsonl")
    assert ClaudeAdapter(source).discover() == []


def test_original_line_provenance_survives_malformed_line(tmp_path):
    path = transcript(tmp_path, [record("user", "Repair the parser.")])
    with path.open("a") as handle:
        handle.write("\n{broken\n" + json.dumps(record("user", "Please fix the validation.")))
    session = ClaudeAdapter(tmp_path).parse(path)
    assert session.events[0].metadata["source_line"] == 1
    assert session.events[1].metadata["source_line"] == 3
    assert session.token_total is None
    assert json.loads(session.model_dump_json())["token_total"] is None


def test_task_preserves_all_text_blocks_without_tool_results(tmp_path):
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, [record("user", [
        {"type": "text", "text": "Repair the parser."},
        {"type": "text", "text": "Preserve quoted fields."},
        {"type": "tool_result", "tool_use_id": "unknown", "content": "Not a task"},
    ])]))
    assert session.task == "Repair the parser.\nPreserve quoted fields."


def test_new_incomplete_checkpoint_cannot_reuse_old_totals(tmp_path):
    records = [record("user", "Repair the parser."),
               dict(type="cost-state", modelUsage={"model": {"inputTokens": 99, "outputTokens": 9}}),
               dict(type="cost-state", modelUsage={"model": {"inputTokens": 100}})]
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, records))
    assert session.token_input is None and session.token_output is None


def test_conflicting_session_ids_are_not_merged(tmp_path):
    records = [record("user", "First task."), record("user", "Other task.")]
    records[1]["sessionId"] = "other-session"
    with pytest.raises(ValueError, match="conflicting session IDs"):
        ClaudeAdapter(tmp_path).parse(transcript(tmp_path, records))


def test_symlink_discovery_root_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source"
    source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ClaudeAdapter(source).discover()


def test_skipped_malformed_record_does_not_claim_complete_usage(tmp_path):
    records = [record("user", "Repair the parser."), record("assistant", [])]
    records[1]["message"].update(id="message-1", usage={"input_tokens": 10, "output_tokens": 2})
    path = transcript(tmp_path, records)
    with path.open("a") as handle:
        handle.write("\n{broken")
    session = ClaudeAdapter(tmp_path).parse(path)
    assert session.token_input is None and session.token_output is None


def test_missing_fragment_identity_leaves_ambiguous_usage_unknown(tmp_path):
    records = [record("user", "Repair the parser.")]
    for _ in range(2):
        item = record("assistant", [{"type": "text", "text": "Fragment"}])
        item["message"]["usage"] = {"input_tokens": 10, "output_tokens": 2}
        records.append(item)
    session = ClaudeAdapter(tmp_path).parse(transcript(tmp_path, records))
    assert session.token_input is None and session.token_output is None
