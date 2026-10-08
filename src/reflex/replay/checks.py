"""Automatic checks run with the same isolated environment as the agent."""
from pathlib import Path

from .models import CheckResult, ReplayCheck
from .subprocess import execute_process


def run_check(check: ReplayCheck, cwd: Path, env: dict[str, str]) -> CheckResult:
    result = execute_process(check.command, cwd, env, check.timeout_seconds)
    return CheckResult(name=check.name, command=check.command, required=check.required,
                       success=result.success, stdout=result.stdout, stderr=result.stderr,
                       exit_code=result.exit_code, timed_out=result.timed_out,
                       elapsed_seconds=result.elapsed_seconds, error=result.error,
                       cleanup_error=result.cleanup_error, output_truncated=result.output_truncated)
