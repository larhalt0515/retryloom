"""Conservative deterministic failure detection over canonical events."""
import hashlib
import re

from reflex.models import FailureEvent, Session
from .signals import confidence, score_signals

TEST_COMMAND = re.compile(r"\b(?:pytest|unittest|jest|vitest|go test|cargo test|npm test)\b")
BUILD_COMMAND = re.compile(r"\b(?:make|cmake|cargo build|npm run build|python -m build|tsc)\b")
CORRECTION = re.compile(r"\b(?:please fix|that's wrong|that is wrong|instead of|you missed|do not do that)\b|修正して|間違っています|そうではなく", re.I)
NEGATIVE = re.compile(r"\b(?:wrong|incorrect|broken|failed)\b|間違い|失敗", re.I)


def detect_failure(session: Session) -> FailureEvent | None:
    evidence: dict[str, list[str]] = {}
    calls: dict[str, str] = {}
    tasks: set[str] = set()
    edits = {}

    def add(signal, index, detail):
        evidence.setdefault(signal, []).append(f"{signal}:event[{index}]: {detail}")

    for index, event in enumerate(session.events):
        raw = event.metadata.get("source_record", {})
        payload = raw.get("payload", {}) if isinstance(raw, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        call_id = payload.get("call_id")
        command = event.command or ""
        if call_id and command:
            calls[call_id] = command
        if not command and call_id:
            command = calls.get(call_id, "")
        text = event.content or ""
        is_output = event.type in {"tool_result", "function_call_output", "custom_tool_call_output", "command_result"}
        if is_output and event.exit_code is not None and event.exit_code != 0:
            if TEST_COMMAND.search(command):
                add("test_failure", index, f"{command}; exit_code={event.exit_code}")
            if BUILD_COMMAND.search(command):
                add("build_failure", index, f"{command}; exit_code={event.exit_code}")
        if is_output and event.exit_code == 0 and re.search(r"\bgit\s+revert\b", command):
            add("git_revert", index, command)
        user = event.type == "user_message" or (event.type == "message" and payload.get("role") == "user")
        if user:
            if CORRECTION.search(text):
                add("explicit_user_correction", index, text)
            if NEGATIVE.search(text):
                add("negative_language", index, text)
            normalized = " ".join(text.split()).casefold()
            if normalized and normalized in tasks:
                add("same_task_repeated", index, text)
            tasks.add(normalized)
        if event.type == "file_edit" and event.file_path:
            previous = edits.get(event.file_path)
            if previous and previous.timestamp and event.timestamp:
                delta = (event.timestamp - previous.timestamp).total_seconds()
                if 0 <= delta <= 60:
                    add("rapid_reedit", index, event.file_path)
            if event.metadata.get("overwrites_previous_change") is True:
                add("change_overwritten", index, event.file_path)
            edits[event.file_path] = event
    if not evidence:
        return None
    signals = sorted(evidence)
    score = score_signals(signals)
    return FailureEvent(
        id=hashlib.sha256(f"{session.id}:failure-v1".encode()).hexdigest(),
        session_id=session.id, score=score, confidence=confidence(score), signals=signals,
        summary="Detected signals: " + ", ".join(signals),
        evidence=[item for name in signals for item in evidence[name]],
    )
