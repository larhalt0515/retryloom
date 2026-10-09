"""Deterministic behavior templates; no source text is inserted into rules."""
import hashlib
import json
import unicodedata
from collections.abc import Sequence

from retryloom.models import CorrectionEvidence, Lesson, LessonAudit, LessonEvidenceRef, LessonTrigger
from retryloom.replay.injection import FrozenLesson

TEMPLATES = {
    "test_failure": "When a test fails in this repository, inspect the failing check, make a focused correction, and rerun the same check before reporting success.",
    "build_failure": "When a build fails in this repository, inspect the failing build step, make a focused correction, and rerun the same build before reporting success.",
    "git_revert": "When a change needs to be reverted in this repository, verify the restored behavior with a relevant check before proceeding.",
    "explicit_user_correction": "When the user corrects a requirement in this repository, confirm the intended constraint, make a focused correction, and verify it before reporting success.",
}


def evidence_items(evidence: CorrectionEvidence | Sequence[CorrectionEvidence]) -> tuple[CorrectionEvidence, ...]:
    if evidence is None:
        return ()
    items = (evidence,) if isinstance(evidence, CorrectionEvidence) else tuple(evidence)
    if any(not isinstance(item, CorrectionEvidence) for item in items):
        return ()
    # Input order and exact duplicates have no semantic effect.
    return tuple(sorted(set(items), key=lambda item: item.model_dump_json()))


def canonical_rule(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def usable(item: CorrectionEvidence) -> bool:
    refs = (item.failure_event_ref, item.correction_event_ref, item.success_event_ref)
    return bool(item.session_id and item.failure_id and item.repository and
                item.failure_type in TEMPLATES and item.success_proof.strip() and
                all(ref is not None for ref in refs) and refs[0] <= refs[1] < refs[2])


def evidence_references(evidence) -> tuple[LessonEvidenceRef, ...]:
    return tuple(LessonEvidenceRef(session_id=item.session_id, failure_id=item.failure_id,
                                  failure_event_ref=item.failure_event_ref,
                                  correction_event_ref=item.correction_event_ref,
                                  success_event_ref=item.success_event_ref,
                                  evidence_hash=fingerprint(item.model_dump(mode="json")))
                 for item in evidence_items(evidence) if usable(item))


def evidence_snapshot(evidence) -> list[dict]:
    """References and hashes only, including hashes of each bounded source input."""
    result = []
    for item in evidence_items(evidence):
        data = item.model_dump(mode="json")
        for key in ("correction_diff", "final_patch", "source_before", "success_proof"):
            data[key] = {"sha256": hashlib.sha256(data[key].encode("utf-8")).hexdigest()}
        data["evidence_hash"] = fingerprint(item.model_dump(mode="json"))
        result.append(data)
    return result


def lesson_fingerprint(lesson: Lesson) -> str:
    return fingerprint(lesson.model_dump(mode="json", exclude={"canonical_fingerprint"}))


def generate_candidate(evidence) -> Lesson | None:
    items = evidence_items(evidence)
    if not items or not all(usable(item) for item in items):
        return None
    repositories = {item.repository for item in items}
    families = {item.failure_type for item in items}
    if len(repositories) != 1 or len(families) != 1:
        return None
    refs = evidence_references(items)
    sessions = tuple(sorted({item.session_id for item in items}))
    count = len(sessions)
    confidence = 0.5 if count == 1 else 0.65 if count == 2 else 0.8
    trigger = LessonTrigger(repository=items[0].repository, failure_type=items[0].failure_type)
    rule = TEMPLATES[trigger.failure_type]
    identity = fingerprint({"trigger": trigger.model_dump(), "rule": rule,
                            "evidence": [ref.model_dump() for ref in refs]})
    return Lesson(id=identity, trigger=trigger, rule=rule, evidence_sessions=sessions,
                  evidence_refs=refs, confidence=confidence)


def _freeze_audited(lesson: Lesson, evidence) -> Lesson:
    from .leakage import audit_lesson
    if lesson.status not in {"candidate", "rejected"}:
        raise ValueError("Phase8 cannot create a confirmed or applied lesson")
    if lesson.lesson_hash is not None:
        if lesson.canonical_fingerprint != lesson_fingerprint(lesson):
            raise ValueError("frozen lesson context changed")
        if lesson.historical_evidence_fingerprint != fingerprint(evidence_snapshot(evidence)):
            raise ValueError("frozen lesson evidence changed")
        FrozenLesson(lesson.rule, lesson.lesson_hash).verify()
        return lesson
    rule = canonical_rule(lesson.rule)
    canonical = lesson.model_copy(update={"rule": rule}, deep=True)
    audit = audit_lesson(canonical, evidence)
    frozen = canonical.model_copy(update={
        "lesson_hash": FrozenLesson.freeze(rule).lesson_hash,
        "audit": audit, "leak_suspect": audit.status != "ACCEPT",
        "status": "rejected" if audit.status == "REJECT" else canonical.status,
        "historical_evidence_fingerprint": fingerprint(evidence_snapshot(evidence)),
    }, deep=True)
    return frozen.model_copy(update={"canonical_fingerprint": lesson_fingerprint(frozen)})


def audit_lesson(lesson: Lesson, evidence) -> LessonAudit:
    from .leakage import audit_lesson as mechanical_audit
    return mechanical_audit(lesson, evidence)


def freeze_candidate(lesson: Lesson, evidence) -> Lesson:
    frozen = _freeze_audited(lesson, evidence)
    if frozen.audit.status != "ACCEPT":
        raise ValueError("unsafe lesson cannot be frozen for replay without audit resolution")
    return frozen
