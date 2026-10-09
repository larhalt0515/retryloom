"""Canonical runner result with unavailable metrics represented as null."""
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class ReplayCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "check"
    command: list[str]
    required: bool = True
    timeout_seconds: int = Field(default=60, gt=0)


class ReplayCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    repository: Path
    base_commit: str
    task: str
    checks: list[ReplayCheck]
    runs: int = Field(default=3, ge=2)
    lesson_id: str
    lesson_hash: str
    timeout_seconds: int = Field(default=1800, gt=0)
    agent: str = "codex"
    agent_version: str | None = None
    model: str | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)
    control_timeout_seconds: int | None = Field(default=None, gt=0)
    reflex_timeout_seconds: int | None = Field(default=None, gt=0)


class RunResult(BaseModel):
    success: bool
    exit_code: int | None = None
    token_input: int | None = None
    token_output: int | None = None
    token_total: int | None = None
    tool_calls: int | None = None
    elapsed_seconds: float
    files_changed: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    stdout: str
    stderr: str
    timed_out: bool = False
    cleanup_error: str | None = None
    agent_version: str | None = None
    model: str | None = None
    lesson_hash: str | None = None
    agent: str | None = None
    agent_error: str | None = None
    output_truncated: bool = False
    efficiency_usable: bool = True


class CheckResult(BaseModel):
    name: str = "check"
    command: list[str] = Field(default_factory=list)
    required: bool = True
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    elapsed_seconds: float = 0.0
    error: str | None = None
    cleanup_error: str | None = None
    output_truncated: bool = False


class ReplayRun(BaseModel):
    id: str
    condition: Literal["CONTROL", "REFLEX"]
    run_number: int
    result: RunResult
    checks: list[CheckResult] = Field(default_factory=list)
    status: str = "COMPLETED"
    statuses: list[str] = Field(default_factory=list)
    seed: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplayReport(BaseModel):
    id: str
    case_id: str
    status: str
    runs: list[ReplayRun] = Field(default_factory=list)
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evaluation(BaseModel):
    """Deterministic interpretation of recorded evidence, without promotion effects."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["replay_confirmed", "rejected", "INVALID_COMPARISON",
                    "INSUFFICIENT_EVIDENCE", "BASELINE_NOT_REPRODUCIBLE"]
    condition: Literal["A", "B"] | None = None
    control_success_count: int = 0
    reflex_success_count: int = 0
    control_median_tokens: float | None = None
    reflex_median_tokens: float | None = None
    token_reduction_ratio: float | None = None
    required_check_regression: bool = False
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
