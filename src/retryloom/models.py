"""Canonical domain models shared by storage and analysis layers."""
from __future__ import annotations
from datetime import datetime
from typing import Any
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class SessionEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    timestamp: datetime | None = None
    command: str | None = None
    exit_code: int | None = None
    file_path: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class Session(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    agent: str
    agent_version: str | None = None
    model: str | None = None
    repository: str
    base_commit: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    task: str | None = None
    result: str | None = None
    token_input: int | None = None
    token_output: int | None = None
    # Unknown unless explicitly available; adapters do not estimate totals.
    token_total: int | None = None
    tool_call_count: int | None = None
    events: list[SessionEvent] = Field(default_factory=list)


class FailureEvent(BaseModel):
    id: str
    session_id: str
    score: int
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    signals: list[str]
    summary: str
    evidence: list[str]


class CorrectionEvidence(BaseModel):
    """Bounded selected evidence. Raw fixture inputs are never storage payloads.

    Event references are zero-based indices in the canonical Session.events.
    success_proof describes the matching successful check or explicit acceptance.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: str
    failure_id: str
    repository: str
    failure_type: str
    failure_event_ref: int | None = Field(default=None, ge=0)
    correction_event_ref: int | None = Field(default=None, ge=0)
    success_event_ref: int | None = Field(default=None, ge=0)
    success_proof: str = Field(default="", max_length=8192)
    correction_diff: str = Field(default="", max_length=65536)
    final_patch: str = Field(default="", max_length=65536)
    source_before: str = Field(default="", max_length=65536)


class LessonTrigger(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repository: str
    failure_type: str

    def __getitem__(self, key: str) -> str:
        return getattr(self, key)


class LessonEvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: str
    failure_id: str
    failure_event_ref: int
    correction_event_ref: int
    success_event_ref: int
    evidence_hash: str


class LessonAudit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ACCEPT", "SUSPECT", "REJECT"]
    reasons: tuple[str, ...] = ()
    evidence_refs: tuple[LessonEvidenceRef, ...] = ()
    evidence_hashes: tuple[str, ...] = ()
    overlap_ratio: float = Field(default=0, ge=0, le=1)


class Lesson(BaseModel):
    """Immutable rule and context. Lifecycle changes use separate storage APIs."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    scope: Literal["repository", "generic"] = "repository"
    trigger: LessonTrigger
    rule: str = Field(max_length=4096)
    evidence_sessions: tuple[str, ...] = ()
    evidence_refs: tuple[LessonEvidenceRef, ...] = ()
    confidence: float = Field(default=0, ge=0, le=1)
    status: Literal["candidate", "replay_confirmed", "rejected", "applied"] = "candidate"
    lesson_hash: str | None = None
    leak_suspect: bool = False
    audit: LessonAudit | None = None
    canonical_fingerprint: str | None = None
    historical_evidence_fingerprint: str | None = None
