"""Independent Phase 8 acceptance tests for behavioral learning boundaries."""
from datetime import datetime, timezone
import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from reflex.analysis.corrections import extract_corrections
from reflex.analysis.failures import detect_failure
from reflex.analysis.lessons import audit_lesson, freeze_candidate, generate_candidate
from reflex.models import CorrectionEvidence, Session, SessionEvent
from reflex.replay.injection import FrozenLesson, instructions_for


def evidence(session_id="session-1", repository="/repo", **changes):
    values = dict(session_id=session_id, failure_id=f"{session_id}-failure", repository=repository,
                  failure_type="test_failure", failure_event_ref=0, correction_event_ref=1,
                  success_event_ref=2, success_proof="pytest completed with exit_code=0",
                  source_before="def parse_record(row):\n    return None\n",
                  correction_diff="@@ -1,2 +1,2 @@\n def parse_record(row):\n-    return None\n+    return corrected_record(row)\n",
                  final_patch="def parse_record(row):\n    return corrected_record(row)\n")
    return CorrectionEvidence(**{**values, **changes})


def candidate_rule(rule, records=None, **changes):
    records = records or [evidence()]
    candidate = generate_candidate(records)
    assert candidate is not None
    return candidate.model_copy(update={"rule": rule, **changes})


def test_bounded_behavior_is_accepted_and_generated_without_answers():
    records = [evidence()]
    candidate = generate_candidate(records)
    assert candidate.status == "candidate"
    assert candidate.scope == "repository" and candidate.trigger.repository == "/repo"
    assert candidate.trigger.failure_type == "test_failure"
    assert audit_lesson(candidate, records).status == "ACCEPT"
    assert candidate.rule.startswith("When ")
    assert records[0].correction_diff not in candidate.rule
    assert records[0].final_patch not in candidate.rule
    assert "corrected_record" not in candidate.rule
    assert candidate.confidence <= 0.5


@pytest.mark.parametrize("rule", ["Always use C.", "Always write code in C.", "Always use Python.",
                                  "Use C for every task.", "Use C for all projects."])
def test_one_off_preference_is_not_a_universal_lesson(rule):
    records = [evidence(success_proof="Try C instead of Python; pytest completed with exit_code=0")]
    lesson = candidate_rule(rule, records)
    assert audit_lesson(lesson, records).status == "REJECT"
    with pytest.raises(ValueError):
        freeze_candidate(lesson, records)


@pytest.mark.parametrize("coordinate", ["line 84", "L84", "parser.py:84", "parser.py#L84"])
def test_source_coordinates_are_rejected(coordinate):
    lesson = candidate_rule(f"When a test fails, edit {coordinate} before rerunning the check.")
    audit = audit_lesson(lesson, [evidence()])
    assert audit.status == "REJECT" and audit.reasons


@pytest.mark.parametrize("fence", ["```python\nreturn corrected_record(row)\n```", "~~~python\nreturn corrected_record(row)\n~~~"])
def test_fenced_answers_are_rejected(fence):
    assert audit_lesson(candidate_rule(fence), [evidence()]).status == "REJECT"


def test_correction_only_identifier_is_suspect_and_cannot_freeze():
    lesson = candidate_rule("When a test fails, inspect corrected_record before repeating the check.")
    audit = audit_lesson(lesson, [evidence()])
    assert audit.status == "SUSPECT"
    assert any("identifier" in reason.lower() for reason in audit.reasons)
    with pytest.raises(ValueError):
        freeze_candidate(lesson, [evidence()])


@pytest.mark.parametrize("assignment", ["+answer = 42", "+answer: int = 42", "+const answer = 42", "+self.answer = 42"])
def test_simple_correction_only_assignment_identifier_is_suspect(assignment):
    record = evidence(source_before="return row", correction_diff=assignment, final_patch="")
    lesson = candidate_rule("When tests fail, inspect answer before repeating the check.", [record])
    assert audit_lesson(lesson, [record]).status == "SUSPECT"


@pytest.mark.parametrize("patch", ["+x=1", "+return 7", "answer=42"])
def test_short_exact_correction_is_never_accepted_as_guidance(patch):
    record = evidence(correction_diff=patch, final_patch="", source_before="")
    assert audit_lesson(candidate_rule(patch, [record]), [record]).status == "REJECT"


@pytest.mark.parametrize("field", ["correction_diff", "final_patch"])
def test_exact_historical_patch_is_rejected(field):
    record = evidence()
    lesson = candidate_rule(getattr(record, field), [record])
    assert audit_lesson(lesson, [record]).status == "REJECT"


def test_high_ngram_overlap_is_suspect():
    rule = "When a test fails after an edit inspect the failing check and rerun it after a correction."
    record = evidence(source_before=rule, correction_diff=rule, final_patch="")
    audit = audit_lesson(candidate_rule(rule, [record]), [record])
    assert audit.status in {"SUSPECT", "REJECT"}
    assert audit.overlap_ratio > 0.3


def test_low_overlap_behavioral_paraphrase_is_accepted():
    lesson = candidate_rule("When tests fail after an edit, inspect the failing check and repeat it after a correction.")
    audit = audit_lesson(lesson, [evidence()])
    assert audit.status == "ACCEPT" and audit.overlap_ratio <= 0.3


@pytest.mark.parametrize("above_threshold", [False, True])
def test_ngram_gate_uses_strict_thirty_percent_boundary(above_threshold):
    rule = "When test checks fail inspect output then rerun checks after correcting changes"
    correction = "When test checks gap_one fail inspect output gap_two then rerun checks"
    if above_threshold:
        correction += " gap_three checks after correcting"
    record = evidence(source_before=rule + " " + correction, correction_diff=correction, final_patch="")
    audit = audit_lesson(candidate_rule(rule, [record]), [record])
    if above_threshold:
        assert audit.overlap_ratio > 0.3 and audit.status == "SUSPECT"
    else:
        assert audit.overlap_ratio == pytest.approx(0.3) and audit.status == "ACCEPT"


def test_hashes_are_deterministic_and_match_existing_injection():
    records = [evidence()]
    lesson = generate_candidate(records)
    frozen = freeze_candidate(lesson, records)
    assert frozen.lesson_hash == freeze_candidate(lesson, records).lesson_hash
    payload = FrozenLesson(frozen.rule, frozen.lesson_hash)
    payload.verify()
    assert instructions_for("CONTROL", payload) == []
    assert instructions_for("REFLEX", payload) == [frozen.rule]
    assert frozen.lesson_hash == FrozenLesson.freeze(frozen.rule).lesson_hash
    assert all(raw not in frozen.rule for raw in (records[0].correction_diff, records[0].final_patch))


def test_canonical_normalization_preserves_same_payload_hash():
    records = [evidence()]
    rule = "When café tests fail, inspect the failing check and repeat it after a correction."
    first = freeze_candidate(candidate_rule(rule, records), records)
    second = freeze_candidate(candidate_rule("  " + rule.replace("é", "e\u0301") + "\r\n", records), records)
    assert first.rule == second.rule and first.lesson_hash == second.lesson_hash


def test_different_unfrozen_rule_changes_hash():
    records = [evidence()]
    lesson = generate_candidate(records)
    first = freeze_candidate(lesson, records)
    second = freeze_candidate(lesson.model_copy(update={"rule": lesson.rule + " Inspect check output."}), records)
    assert first.lesson_hash != second.lesson_hash


def test_frozen_content_cannot_mutate_or_be_silently_refrozen():
    records = [evidence()]
    frozen = freeze_candidate(generate_candidate(records), records)
    with pytest.raises((ValidationError, AttributeError)):
        frozen.rule = "tampered"
    corrupted = frozen.model_copy(update={"rule": frozen.rule + " Do another thing."})
    with pytest.raises(ValueError):
        freeze_candidate(corrupted, records)
    with pytest.raises(ValueError, match="hash"):
        FrozenLesson(corrupted.rule, frozen.lesson_hash).verify()


def test_changed_historical_evidence_does_not_rewrite_frozen_candidate():
    records = [evidence()]
    frozen = freeze_candidate(generate_candidate(records), records)
    changed = [evidence(correction_diff="+return completely_different_answer()")]
    with pytest.raises(ValueError):
        freeze_candidate(frozen, changed)
    assert FrozenLesson(frozen.rule, frozen.lesson_hash).lesson_hash == frozen.lesson_hash


def test_corroboration_raises_confidence_without_counting_duplicate_evidence():
    first = evidence("session-1")
    single = generate_candidate([first])
    duplicated = generate_candidate([first, first, first])
    repeated = generate_candidate([first, evidence("session-2")])
    recurring = generate_candidate([first, evidence("session-2"), evidence("session-3")])
    assert single.confidence == duplicated.confidence
    assert single.confidence < repeated.confidence <= recurring.confidence
    assert all(lesson.status == "candidate" for lesson in (single, repeated, recurring))


def test_unrelated_repositories_are_not_combined_into_global_learning():
    records = [evidence("session-1", "/repo-a"), evidence("session-2", "/repo-b")]
    candidate = generate_candidate(records)
    assert candidate is None or audit_lesson(candidate, records).status == "REJECT"


def test_missing_or_partial_evidence_is_unusable():
    assert generate_candidate([]) is None
    partial = evidence(success_event_ref=None, success_proof="")
    assert generate_candidate([partial]) is None
    unknown = evidence(failure_type="unknown_signal")
    assert generate_candidate([unknown]) is None


def test_audit_and_generation_are_deterministic_and_preserve_evidence():
    records = [evidence()]
    before = records[0].model_dump_json()
    lesson = generate_candidate(records)
    expected = audit_lesson(lesson, records)
    assert all(audit_lesson(lesson, records) == expected for _ in range(10))
    assert all(generate_candidate(records) == lesson for _ in range(3))
    assert records[0].model_dump_json() == before
    assert lesson.evidence_sessions == ("session-1",)
    assert lesson.evidence_refs[0].failure_id == "session-1-failure"


def history(repository="/repo", instruction=None):
    events = [SessionEvent(type="tool_result", command="pytest", exit_code=1),
              SessionEvent(type="file_edit", file_path="parser.py", content=evidence().correction_diff,
                           metadata={"source_before": evidence().source_before}),
              SessionEvent(type="tool_result", command="pytest", exit_code=0)]
    if instruction:
        events.insert(1, SessionEvent(type="user_message", content=instruction))
    session = Session(id="real-shape", agent="codex", repository=repository,
                      started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), events=events)
    return session, detect_failure(session)


def test_canonical_failure_correction_success_extraction():
    source, failure = history()
    before = source.model_dump_json(), failure.model_dump_json()
    records = extract_corrections(source, failure)
    assert records and records[0].session_id == source.id
    assert records[0].failure_id == failure.id
    assert records[0].failure_event_ref == 0 and records[0].success_event_ref == 2
    lesson = generate_candidate(records)
    assert lesson is not None and lesson.confidence <= 0.5
    assert audit_lesson(lesson, records).status == "ACCEPT"
    assert (source.model_dump_json(), failure.model_dump_json()) == before


@pytest.mark.parametrize("instruction", ["Try C instead of Python.", "今回はPythonではなくCで"])
def test_real_one_off_instruction_does_not_generate_language_rule(instruction):
    source, failure = history(instruction=instruction)
    lesson = generate_candidate(extract_corrections(source, failure))
    if lesson:
        assert "always" not in lesson.rule.lower()
        assert lesson.scope == "repository" and lesson.confidence <= 0.5
        assert lesson.status != "replay_confirmed"


def test_unrelated_success_does_not_prove_a_correction():
    source, failure = history()
    source.events[-1].command = "pytest tests/unrelated.py"
    assert extract_corrections(source, failure) == []


def test_oversized_correction_evidence_is_not_silently_truncated_into_acceptance():
    source, failure = history()
    source.events[1].content = "x" * 65537
    assert extract_corrections(source, failure) == []


def test_malformed_optional_tool_trace_fails_gracefully():
    source, failure = history()
    source.events.insert(1, SessionEvent(type="unknown", metadata={"source_record":{"payload":{"call_id": ["malformed"]}}}))
    records = extract_corrections(source, failure)
    assert isinstance(records, list)
    assert all(record.failure_event_ref == 0 and record.success_event_ref == 3 for record in records)


def test_codex_raw_call_trace_correction_extraction():
    source = Session(id="codex-rollout", agent="codex", repository="/repo",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), events=[
            SessionEvent(type="function_call", command="pytest", metadata={"source_record":{"payload":{"call_id":"failed-check"}}}),
            SessionEvent(type="function_call_output", exit_code=1, metadata={"source_record":{"payload":{"call_id":"failed-check"}}}),
            SessionEvent(type="custom_tool_call", metadata={"source_record":{"payload":{"name":"apply_patch","input":"*** Begin Patch\n*** Update File: parser.py\n@@\n-old_value\n+new_value\n*** End Patch"}}}),
            SessionEvent(type="function_call", command="pytest", metadata={"source_record":{"payload":{"call_id":"corrected-check"}}}),
            SessionEvent(type="function_call_output", exit_code=0, metadata={"source_record":{"payload":{"call_id":"corrected-check"}}}),
        ])
    failure = detect_failure(source)
    records = extract_corrections(source, failure)
    assert records and records[0].failure_event_ref == 1 and records[0].success_event_ref == 4
    assert "new_value" not in generate_candidate(records).rule


def stored_candidate(tmp_path, rule=None):
    from reflex.storage.database import connect
    from reflex.storage.repository import save_failure, save_lesson, save_session
    source, failure = history()
    records = extract_corrections(source, failure)
    lesson = generate_candidate(records)
    if rule is not None:
        lesson = lesson.model_copy(update={"rule": rule})
    database = connect(tmp_path / "lessons.db")
    save_session(database, source)
    save_failure(database, failure)
    saved = save_lesson(database, lesson, records)
    return database, records, saved


def test_candidate_audit_scope_confidence_and_references_round_trip(tmp_path):
    from reflex.storage.repository import load_lesson
    database, records, saved = stored_candidate(tmp_path)
    loaded = load_lesson(database, saved.id)
    assert loaded == saved and loaded.status == "candidate"
    assert loaded.scope == "repository" and loaded.confidence == 0.5
    assert loaded.evidence_sessions == ("real-shape",)
    assert loaded.evidence_refs[0].failure_id == records[0].failure_id
    raw = database.execute("SELECT evidence_json FROM lesson_audits WHERE lesson_id=?", (saved.id,)).fetchone()[0]
    assert "corrected_record" not in raw and records[0].correction_diff not in raw
    database.close()


def test_unsafe_lesson_is_persisted_as_rejected_and_cannot_be_overridden(tmp_path):
    from reflex.storage.repository import load_lesson, prepare_lesson_for_replay, resolve_lesson_audit
    database, records, saved = stored_candidate(tmp_path, "Always use C.")
    assert saved.status == "rejected" and saved.audit.status == "REJECT"
    assert load_lesson(database, saved.id).status == "rejected"
    with pytest.raises(ValueError):
        prepare_lesson_for_replay(database, saved.id, records)
    with pytest.raises(ValueError):
        resolve_lesson_audit(database, saved.id, records, reviewer="fixture", reason="override", approved=True)
    database.close()


def test_suspect_requires_explicit_bound_audit_resolution_before_replay(tmp_path):
    from reflex.storage.repository import prepare_lesson_for_replay, resolve_lesson_audit
    database, records, saved = stored_candidate(tmp_path, "When tests fail, inspect corrected_record before repeating the check.")
    assert saved.status == "candidate" and saved.audit.status == "SUSPECT"
    assert database.execute("SELECT leak_suspect FROM lessons WHERE id=?", (saved.id,)).fetchone()[0] == 1
    with pytest.raises(ValueError):
        prepare_lesson_for_replay(database, saved.id, records)
    with pytest.raises(ValueError):
        resolve_lesson_audit(database, saved.id, records, reviewer="", reason="", approved=True)
    resolve_lesson_audit(database, saved.id, records, reviewer="fixture reviewer",
                         reason="Fixture audit: the identifier is contextual guidance, not an answer.", approved=True)
    assert prepare_lesson_for_replay(database, saved.id, records).text == saved.rule
    changed = [records[0].model_copy(update={"correction_diff": "+different_answer()"})]
    with pytest.raises(ValueError):
        prepare_lesson_for_replay(database, saved.id, changed)
    database.close()


def test_phase8_cannot_persist_forged_replay_confirmed_status(tmp_path):
    from reflex.storage.repository import save_lesson
    database, records, saved = stored_candidate(tmp_path)
    forged = saved.model_copy(update={"status": "replay_confirmed"})
    with pytest.raises(ValueError):
        save_lesson(database, forged, records)
    assert database.execute("SELECT status FROM lessons WHERE id=?", (saved.id,)).fetchone()[0] == "candidate"
    database.close()


def test_missing_historical_failure_reference_cannot_be_persisted(tmp_path):
    from reflex.storage.repository import save_lesson
    database, records, _ = stored_candidate(tmp_path)
    invalid = [records[0].model_copy(update={"failure_id": "missing-failure"})]
    lesson = generate_candidate(invalid)
    with pytest.raises(ValueError):
        save_lesson(database, lesson, invalid)
    assert database.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1
    database.close()


def test_correction_source_cannot_be_omitted_to_bypass_leakage_audit(tmp_path):
    from reflex.storage.repository import save_lesson
    database, records, _ = stored_candidate(tmp_path)
    omitted = [records[0].model_copy(update={"correction_diff": "", "source_before": ""})]
    lesson = generate_candidate(omitted).model_copy(update={
        "rule": "When tests fail, inspect corrected_record before repeating the check."})
    with pytest.raises(ValueError):
        save_lesson(database, lesson, omitted)
    assert database.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1
    database.close()


def test_post_freeze_context_mutation_is_detected_on_load(tmp_path):
    from reflex.storage.repository import load_lesson
    database, _, saved = stored_candidate(tmp_path)
    database.execute("UPDATE lessons SET scope='generic' WHERE id=?", (saved.id,))
    database.commit()
    with pytest.raises(ValueError):
        load_lesson(database, saved.id)
    database.close()


@pytest.mark.parametrize("scenario", ["normal", "context_mutation", "wrong_repository"])
def test_canonical_history_to_frozen_lesson_and_recorded_replay(tmp_path, scenario):
    from reflex.replay.engine import ReplayEngine
    from reflex.replay.evaluator import evaluate
    from reflex.replay.models import ReplayCase, ReplayCheck, RunResult
    from reflex.storage.database import connect
    from reflex.storage.repository import load_lesson, prepare_lesson_for_replay, save_failure, save_lesson, save_session

    source_root = tmp_path / "source"
    source_root.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=source_root, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (source_root / "base").write_text("base")
    git("add", "base")
    git("commit", "-qm", "base")
    session, failure = history(str(source_root))
    records = extract_corrections(session, failure)
    lesson = generate_candidate(records)
    database = connect(tmp_path / "reflex.db")
    save_session(database, session)
    save_failure(database, failure)
    before = {table: [tuple(row) for row in database.execute(f"SELECT * FROM {table}")] for table in ("sessions", "session_events", "failures")}
    saved = save_lesson(database, lesson, records)
    assert saved.status == "candidate"
    assert saved.scope == lesson.scope and saved.confidence == lesson.confidence
    assert saved.evidence_refs == lesson.evidence_refs
    payload = prepare_lesson_for_replay(database, lesson.id, records)
    payload.verify()
    assert load_lesson(database, lesson.id).lesson_hash == payload.lesson_hash
    case = ReplayCase(id="case-lesson", repository=source_root, base_commit=git("rev-parse", "HEAD"),
                      task="Run the fixture task.", lesson_id=lesson.id, lesson_hash=payload.lesson_hash,
                      checks=[ReplayCheck(command=[sys.executable, "-c", "from pathlib import Path; import sys; sys.exit(0 if Path('done').exists() else 1)"])])
    if scenario == "wrong_repository":
        other = tmp_path / "different-repository"
        subprocess.run(["git", "clone", "--no-local", str(source_root), str(other)], check=True, capture_output=True)
        case = case.model_copy(update={"repository": other})
    class Runner:
        calls = []
        def run(self, task, cwd, instructions, timeout_seconds, env):
            self.calls.append(list(instructions))
            if scenario == "context_mutation" and len(self.calls) == 1:
                database.execute("UPDATE lessons SET trigger_json='{}' WHERE id=?", (lesson.id,))
                database.commit()
            if instructions:
                (cwd / "done").write_text("fixture only")
            return RunResult(success=True, exit_code=0, elapsed_seconds=0.1, stdout="fixture", stderr="")
    runner = Runner()
    report = ReplayEngine(runner, database, env={"PATH": os.environ.get("PATH", "")}, temp_parent=tmp_path / "runs").execute(case, payload)
    if scenario == "wrong_repository":
        assert report.status == "INVALID_COMPARISON" and runner.calls == []
        assert evaluate(report).status != "replay_confirmed"
        database.close()
        return
    if scenario == "context_mutation":
        assert report.status == "INVALID_COMPARISON"
        assert evaluate(report).status != "replay_confirmed"
        with pytest.raises(ValueError):
            load_lesson(database, lesson.id)
        database.close()
        return
    assert report.status == "COMPLETED"
    assert runner.calls == [[], [], [], [payload.text], [payload.text], [payload.text]]
    assert evaluate(report).status == "replay_confirmed"
    assert load_lesson(database, lesson.id).status == "candidate"
    after = {table: [tuple(row) for row in database.execute(f"SELECT * FROM {table}")] for table in before}
    assert before == after
    assert database.execute("SELECT status FROM lessons WHERE id=?", (lesson.id,)).fetchone()[0] == "candidate"
    database.close()
