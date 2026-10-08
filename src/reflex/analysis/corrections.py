"""Select bounded correction/success evidence from canonical sessions only."""
import re

from reflex.models import CorrectionEvidence, FailureEvent, Session
from .failures import BUILD_COMMAND, CORRECTION, TEST_COMMAND

OUTPUT_TYPES = {"tool_result", "function_call_output", "custom_tool_call_output", "command_result"}
ACCEPTANCE = re.compile(r"\b(?:looks good|works now|fixed now|that works|correct now)\b|直りました|修正できました|これで大丈夫", re.I)
def _is_user(event) -> bool:
    raw = event.metadata.get("source_record", {})
    payload = raw.get("payload", {}) if isinstance(raw, dict) else {}
    return event.type == "user_message" or (event.type == "message" and isinstance(payload, dict) and payload.get("role") == "user")


FAMILIES = {"test_failure", "build_failure", "git_revert", "explicit_user_correction"}


def extract_correction_evidence(session: Session, failure: FailureEvent) -> list[CorrectionEvidence]:
    """Require an actual failure, intervening correction, then matching success.

    Command checks match exact commands after whitespace normalization. An
    unrelated successful command is never evidence. Explicit corrections/reverts
    require explicit user acceptance or a check linked by correction_event_ref.
    A new user task stops the search; raw unrelated history is never copied.
    """
    if failure.session_id != session.id or not session.repository:
        return []
    selected = []
    commands = {}
    calls = {}
    for index, event in enumerate(session.events):
        raw = event.metadata.get("source_record", {})
        payload = raw.get("payload", {}) if isinstance(raw, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        call_id = payload.get("call_id")
        call_id = call_id if isinstance(call_id, str) else None
        if event.command and call_id:
            calls[call_id] = event.command
        commands[index] = " ".join((event.command or calls.get(call_id, "")).split())
    for index, event in enumerate(session.events):
        command = commands[index]
        family = None
        if event.type in OUTPUT_TYPES and event.exit_code not in (None, 0):
            family = "test_failure" if TEST_COMMAND.search(command) else "build_failure" if BUILD_COMMAND.search(command) else None
        elif event.type in OUTPUT_TYPES and event.exit_code == 0 and re.search(r"\bgit\s+revert\b", command):
            family = "git_revert"
        elif _is_user(event) and CORRECTION.search(event.content or ""):
            family = "explicit_user_correction"
        if family not in FAMILIES or family not in failure.signals:
            continue
        # A forged/stale FailureEvent must not provide evidence for a new event.
        if not any(f"{family}:event[{index}]:" in item for item in failure.evidence):
            continue
        correction = index if family in {"git_revert", "explicit_user_correction"} else None
        correction_text = (event.content or "") if family == "explicit_user_correction" else ""
        before = ""
        final_patch = ""
        if len(correction_text) > 65536:
            continue
        for later in range(index + 1, len(session.events)):
            candidate = session.events[later]
            if _is_user(candidate) and not ACCEPTANCE.search(candidate.content or ""):
                if CORRECTION.search(candidate.content or ""):
                    correction = later
                    correction_text = candidate.content or ""
                    before = ""
                    final_patch = ""
                    if len(correction_text) > 65536:
                        break
                    continue
                break
            raw = candidate.metadata.get("source_record", {})
            payload = raw.get("payload", {}) if isinstance(raw, dict) else {}
            payload = payload if isinstance(payload, dict) else {}
            patch = payload.get("input", payload.get("arguments", ""))
            is_patch = (candidate.type in {"custom_tool_call", "function_call"} and
                        str(payload.get("name", "")).split(".")[-1] == "apply_patch" and
                        isinstance(patch, str) and "*** Begin Patch" in patch and
                        "*** End Patch" in patch)
            if is_patch:
                correction = later
                correction_text = patch
                before = candidate.metadata.get("source_before", "")
                final_patch = candidate.metadata.get("final_patch", "")
                if (not isinstance(before, str) or not isinstance(final_patch, str) or
                        len(before) > 65536 or len(final_patch) > 65536 or len(correction_text) > 65536):
                    break
            if candidate.type == "file_edit":
                correction = later
                correction_text = candidate.content or ""
                before = candidate.metadata.get("source_before", "")
                final_patch = candidate.metadata.get("final_patch", "")
                if (not isinstance(before, str) or not isinstance(final_patch, str) or
                        len(before) > 65536 or len(final_patch) > 65536 or len(correction_text) > 65536):
                    break
            if correction is None:
                continue
            matched = (family in {"test_failure", "build_failure"} and
                       candidate.type in OUTPUT_TYPES and candidate.exit_code == 0 and
                       commands[later] == command)
            accepted = (family in {"git_revert", "explicit_user_correction"} and
                        ((_is_user(candidate) and ACCEPTANCE.search(candidate.content or "")) or
                         (candidate.type in OUTPUT_TYPES and candidate.exit_code == 0 and
                          candidate.metadata.get("correction_event_ref") == correction and
                          (TEST_COMMAND.search(commands[later]) or BUILD_COMMAND.search(commands[later])))))
            if matched or accepted:
                if len(commands[later]) > 8192:
                    break
                selected.append(CorrectionEvidence(
                    session_id=session.id, failure_id=failure.id, repository=session.repository,
                    failure_type=family, failure_event_ref=index, correction_event_ref=correction,
                    success_event_ref=later, success_proof=commands[later] or "explicit user acceptance",
                    correction_diff=correction_text, source_before=before, final_patch=final_patch,
                ))
                break
            # A later failure of the matching check invalidates this attempt.
            if candidate.type in OUTPUT_TYPES and commands[later] == command and candidate.exit_code not in (None, 0):
                break
    return selected


extract_corrections = extract_correction_evidence
