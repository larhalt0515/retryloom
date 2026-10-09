"""Minimal Codex CLI runner with explicit environment and bounded execution."""
from pathlib import Path
from typing import Protocol

from .models import RunResult
from .subprocess import execute_process


class AgentRunner(Protocol):
    def run(self, task: str, cwd: Path, instructions: list[str], timeout_seconds: int,
            env: dict[str, str]) -> RunResult: ...


class CodexRunner:
    """Version metadata is caller-attested; this runner does not query live auth or version."""
    agent = "codex"

    def __init__(self, executable: str = "codex", model: str | None = None,
                 agent_version: str | None = None):
        self.executable = executable
        self.model = model
        self.agent_version = agent_version
        self.agent_version_source = "caller_attested" if agent_version is not None else None
        self.model_settings: dict = {}

    def validate_configuration(self) -> None:
        if self.model_settings:
            raise ValueError("CodexRunner does not support nonempty model_settings")

    def run(self, task: str, cwd: Path, instructions: list[str], timeout_seconds: int,
            env: dict[str, str]) -> RunResult:
        self.validate_configuration()
        command = [self.executable, "exec", "--json", "--ephemeral", "--sandbox", "workspace-write"]
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        prompt = task + ("\n\n" + "\n\n".join(instructions) if instructions else "")
        result = execute_process(command, cwd, env, timeout_seconds, input_text=prompt)
        # Exec JSON events are retained locally; no usage semantics are guessed.
        return RunResult(success=result.success, exit_code=result.exit_code,
                         elapsed_seconds=result.elapsed_seconds,
                         stdout=result.stdout, stderr=result.stderr, timed_out=result.timed_out,
                         cleanup_error=result.cleanup_error, model=self.model, agent=self.agent,
                         agent_version=self.agent_version,
                         agent_error=result.error, output_truncated=result.output_truncated,
                         efficiency_usable=result.success)
