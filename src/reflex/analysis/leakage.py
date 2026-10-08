"""Mechanical leakage audit, with reproducible findings and no model judge.

Overlap uses unique contiguous word trigrams after NFC, casefold, and Unicode
word tokenization (punctuation is discarded). The denominator is the number of
unique lesson trigrams; only a ratio strictly greater than 0.30 is SUSPECT.
Identifiers are camel/Pascal/snake case, assignment/call names, or explicitly code-marked names; plain
prose vocabulary is not treated as an identifier. Only correction additions
absent from source_before and removed diff lines are suspect.
"""
import re
import unicodedata

from reflex.models import Lesson, LessonAudit
from .lessons import (TEMPLATES, canonical_rule, evidence_items, evidence_references,
                      usable)


def tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold()))


def trigrams(text: str) -> set[tuple[str, ...]]:
    words = tokens(text)
    return {words[index:index + 3] for index in range(max(0, len(words) - 2))}


def identifiers(text: str) -> set[str]:
    names = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", text))
    marked = set(re.findall(r"`([A-Za-z][A-Za-z0-9_]*)`|\b(?:def|class|function)\s+([A-Za-z][A-Za-z0-9_]*)", text))
    targets = re.findall(
        r"(?m)^\s*[+-]?\s*(?:[A-Za-z][A-Za-z0-9_]*\s+)?"
        r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*)"
        r"(?:\s*:\s*[A-Za-z_][A-Za-z0-9_.\[\], |]*)?\s*=(?!=)", text)
    calls = re.findall(
        r"(?m)^\s*[+-]?\s*(?:[A-Za-z][A-Za-z0-9_]*\s+)?"
        r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*)\s*\([^\n]*\)", text)
    syntax = {part for target in targets + calls for part in target.split(".")}
    return syntax | {name for name in names if "_" in name or re.search(r"[a-z][A-Z]|[A-Z][a-z]+[A-Z]", name)} | {name for pair in marked for name in pair if name}


def audit_lesson(lesson: Lesson, evidence) -> LessonAudit:
    items = evidence_items(evidence)
    refs = evidence_references(items)
    text = canonical_rule(lesson.rule)
    reject = []
    suspect = []
    if not text:
        reject.append("empty_rule")
    if "```" in text or "~~~" in text:
        reject.append("fenced_code")
    if re.search(r"\b(?:lines?\s*\d+|L\d+)\b|\b[^\s:]+:\d+\b", text, re.I):
        reject.append("source_coordinates")
    if re.search(r"(?m)^(?:diff --git|@@|\+\+\+|---\s|[+-](?:\s*(?:def |class |return |import |const |let |function )))", text):
        reject.append("patch_content")
    if re.search(r"\b(?:sk-[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})\b|(?:password|api[_-]?key|token|secret)\s*[:=]\s*\S+|-----BEGIN .*PRIVATE KEY", text, re.I):
        reject.append("secret_content")
    if re.search(r"(?:/Users/|/home/|/tmp/|/private/|[A-Za-z]:\\|(?:\b|\./)[\w.-]+/(?:[\w.-]+/)*[\w.-]+|\b[\w.-]+\.(?:py|js|ts|json|yaml|yml|toml|go|rs|cpp|c)\b)", text):
        reject.append("source_path")
    if re.search(r"Traceback \(most recent call last\)|\bat \S+\([^\n]*:\d+|\bFile \"[^\"]+\", line|\b\w+(?:Error|Exception):", text):
        reject.append("stack_trace")
    if not items or not all(usable(item) for item in items):
        reject.append("missing_success_evidence")
    if any(item.repository != lesson.trigger.repository or item.failure_type != lesson.trigger.failure_type for item in items):
        reject.append("evidence_trigger_mismatch")
    if refs != lesson.evidence_refs or tuple(sorted({item.session_id for item in items})) != lesson.evidence_sessions:
        reject.append("evidence_reference_mismatch")
    sessions = len({item.session_id for item in items if usable(item)})
    bound = 0.5 if sessions <= 1 else 0.65 if sessions == 2 else 0.8
    if lesson.confidence > bound:
        reject.append("unsupported_confidence")
    if lesson.scope != "repository":
        reject.append("unsupported_generic_scope")
    if re.search(r"\b(?:always|never|every time|all repositories|all code|any task|every project|universally|(?:all|any|every)\s+(?:implementations?|programs?|repositories|projects?|tasks?))\b|常に|絶対|いつでも|必ず", text, re.I):
        # Supported templates describe bounded triggers and contain no universals.
        reject.append("unsupported_universal_rule")
    if not re.search(r"\b(?:when|if|after|before|whenever)\b|場合|とき|際", text, re.I):
        reject.append("unbounded_behavior")
    if re.search(r"\b(?:use|write|switch to)\s+(?:only\s+)?(?:C|Python|JavaScript|Java|Rust)\b|(?:Python|C)ではなく|(?:Python|C)を使", text, re.I):
        reject.append("unsupported_language_prescription")
    if lesson.trigger.failure_type not in TEMPLATES:
        reject.append("unsupported_failure_type")
    lesson_grams = trigrams(text)
    correction_grams = set()
    new_identifiers = set()
    normalized_text = " ".join(tokens(text))
    for item in items:
        corrections = "\n".join((item.correction_diff, item.final_patch))
        correction_grams.update(trigrams(corrections))
        removed = "\n".join(line[1:] for line in item.correction_diff.splitlines() if line.startswith("-") and not line.startswith("---"))
        new_identifiers.update(identifiers(corrections) - set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", item.source_before + "\n" + removed)))
        for snippet in (item.correction_diff, item.final_patch):
            normalized = " ".join(tokens(snippet))
            # Whole correction/final answer or an 8-word contiguous excerpt is
            # direct copying. Smaller partial overlap is handled by the ratio.
            whole_match = bool(normalized and re.search(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", normalized_text))
            if len(tokens(snippet)) <= 2 and re.search(r"[=()]", snippet):
                # A short code call must actually be copied as code; merely
                # mentioning its name belongs to the identifier SUSPECT check.
                code = re.sub(r"(?m)^\s*\+", "", snippet)
                compact = re.sub(r"\s+", "", code).casefold()
                compact_rule = re.sub(r"\s+", "", text).casefold()
                whole_match = bool(compact and compact in compact_rule)
            if whole_match:
                reject.append("direct_correction_or_final_answer")
            words = tokens(snippet)
            if any(" ".join(words[index:index + 8]) in normalized_text for index in range(max(0, len(words) - 7))):
                reject.append("direct_correction_or_final_answer")
    overlap = len(lesson_grams & correction_grams) / len(lesson_grams) if lesson_grams else 0.0
    if overlap > 0.30:
        suspect.append("correction_ngram_overlap_gt_30_percent")
    if new_identifiers & set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", text)):
        suspect.append("correction_only_identifier")
    reasons = tuple(sorted(set(reject + suspect)))
    return LessonAudit(status="REJECT" if reject else "SUSPECT" if suspect else "ACCEPT",
                       reasons=reasons, evidence_refs=refs,
                       evidence_hashes=tuple(ref.evidence_hash for ref in refs), overlap_ratio=overlap)
