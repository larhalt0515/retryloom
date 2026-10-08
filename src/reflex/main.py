"""Reflex command-line entry point."""

from __future__ import annotations

import typer
from pathlib import Path
import subprocess
import sqlite3
from pydantic import ValidationError
from contextlib import closing
from reflex.git.repository import _git
from reflex.adapters.claude import ClaudeAdapter
from reflex.adapters.codex import CodexAdapter
from reflex.storage.database import connect, initialize_schema
from reflex.storage.repository import save_session

app = typer.Typer(help="Learn reusable coding-agent lessons through fair replay.")


def _error_summary(error: Exception) -> str:
    if not isinstance(error, ValidationError):
        return str(error)
    # Extra-field locations can themselves contain untrusted input keys.
    fields = {
        "id", "repository", "base_commit", "task", "checks", "runs", "lesson_id",
        "lesson_hash", "timeout_seconds", "agent", "agent_version", "model",
        "model_settings", "control_timeout_seconds", "reflex_timeout_seconds",
        "name", "command", "required", "version", "project", "root", "default_agent",
        "replay", "timeout_minutes", "evaluation", "token_reduction_threshold",
        "started_at", "ended_at", "result", "token_input", "token_output",
        "token_total", "tool_call_count", "events", "type", "timestamp", "exit_code",
        "file_path", "content", "metadata", "session_id", "score", "confidence",
        "signals", "summary", "evidence", "scope", "trigger", "failure_type", "rule",
        "evidence_sessions", "evidence_refs", "status", "leak_suspect", "audit",
        "canonical_fingerprint", "historical_evidence_fingerprint",
    }
    locations = set()
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        parts = [str(part) if type(part) is int or part in fields else "<field>"
                 for part in detail["loc"]]
        locations.add(".".join(parts) or "document")
    return "validation failed at: " + ", ".join(sorted(locations))


@app.command()
def init(repository: Path = typer.Option(Path("."))) -> None:
    """Initialize Reflex storage in a Git repository."""
    from reflex.cli_workflows import initialize_project, agent_cli_availability
    try:
        root = initialize_project(repository)
        typer.echo(f"Initialized Reflex: {root / '.reflex'}")
        availability = agent_cli_availability()
        typer.echo("Agent CLIs (PATH lookup only): " + "; ".join(
            f"{agent}={'available' if found else 'unavailable'}" for agent, found in availability.items()))
    except (ValueError, OSError, sqlite3.Error, RuntimeError, subprocess.SubprocessError) as error:
        typer.echo(f"Init failed: {_error_summary(error)}", err=True)
        raise typer.Exit(1)


@app.command()
def scan(agent: str = typer.Option("codex"), source_root: Path | None = typer.Option(None), repository: Path = typer.Option(Path("."))) -> None:
    """Import only sessions associated with the selected Git repository."""
    if agent not in {"codex", "claude"}:
        raise typer.BadParameter("agent must be codex or claude")
    try:
        root = Path(_git(repository.resolve(), "rev-parse", "--show-toplevel").strip()).resolve()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise typer.BadParameter("repository must be a Git repository")
    adapter = ClaudeAdapter(source_root) if agent == "claude" else CodexAdapter(source_root)
    imported = skipped = rejected = 0
    from reflex.cli_workflows import guard_storage
    try:
        database_path = guard_storage(root)
    except (ValueError, OSError) as error:
        typer.echo(f"Scan failed: {_error_summary(error)}", err=True)
        raise typer.Exit(1)
    try:
        with closing(connect(database_path)) as connection:
            for path in adapter.discover():
                try:
                    session = adapter.parse(path)
                    cwd = Path(session.repository)
                    if not session.repository or not cwd.is_absolute():
                        skipped += 1
                        continue
                    associated = Path(_git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
                    if associated != root:
                        skipped += 1
                        continue
                    existing = connection.execute("SELECT agent FROM sessions WHERE id=?", (session.id,)).fetchone()
                    # Imported IDs are immutable; changed transcripts need explicit future refresh support.
                    if existing is not None:
                        skipped += 1
                        continue
                    save_session(connection, session)
                    imported += 1
                except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    rejected += 1
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        typer.echo(f"Scan failed: {_error_summary(error)}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Imported: {imported}; skipped: {skipped}; rejected: {rejected}")


@app.command()
def learn(session_id: str = typer.Option(...), repository: Path = typer.Option(Path("."))) -> None:
    """Generate candidate lessons from detected failures."""
    from reflex.cli_workflows import learn_session
    try:
        for lesson in learn_session(repository, session_id):
            typer.echo(f"Lesson {lesson.id}: {lesson.status}; hash={lesson.lesson_hash}; audit={lesson.audit.status}")
    except (ValueError, OSError, sqlite3.Error, RuntimeError, subprocess.SubprocessError) as error:
        typer.echo(f"Learn failed: {_error_summary(error)}", err=True)
        raise typer.Exit(1)


@app.command()
def replay(
    case: Path = typer.Option(..., "--case", help="Exact canonical ReplayCase JSON file."),
    repository: Path = typer.Option(Path(".")),
    execute: bool = typer.Option(False, help="Explicitly authorize agent execution and local result writes."),
) -> None:
    """Replay a task under CONTROL and REFLEX conditions."""
    from reflex.cli_workflows import replay_preflight, execute_replay
    try:
        root, plan, frozen = replay_preflight(repository, case)
        typer.echo(f"Replay plan {plan.id}: repository={root}; commit={plan.base_commit}; lesson={plan.lesson_id}; runs={plan.runs} per arm")
        if not execute:
            typer.echo("Preflight passed. Use --execute to run Codex and persist evidence.")
            return
        report, evaluation, evaluation_id = execute_replay(root, plan, frozen)
        typer.echo(f"Replay {report.id}: {report.status}; {report.reason}")
        typer.echo(f"Evaluation {evaluation_id}: {evaluation.status}; {evaluation.reason}")
        counts = {status: sum(status in {run.status, *run.statuses} or
                              (status == "TIMEOUT" and run.result.timed_out) or
                              (status == "AGENT_ERROR" and bool(run.result.agent_error))
                              for run in report.runs)
                  for status in ("TIMEOUT", "AGENT_ERROR")}
        typer.echo("Run statuses: " + "; ".join(f"{status}={count}" for status, count in counts.items()))
        if report.status != "COMPLETED" or any(counts.values()):
            raise typer.Exit(1)
    except (ValueError, OSError, sqlite3.Error, RuntimeError, subprocess.SubprocessError) as error:
        typer.echo(f"Replay failed: {_error_summary(error)}", err=True)
        raise typer.Exit(1)


def _project_root(repository: Path) -> Path:
    try:
        return Path(_git(repository.resolve(), "rev-parse", "--show-toplevel").strip()).resolve()
    except (OSError, subprocess.SubprocessError) as error:
        raise typer.BadParameter("repository must be a Git repository") from error


def _read_project(root: Path, *, allow_empty: bool = False):
    """Open existing evidence read-only; never initialize a project during preview."""
    directory = root / ".reflex"
    path = directory / "reflex.db"
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("Reflex storage must not be a symlink")
    if not path.exists():
        if not allow_empty:
            raise ValueError("Reflex database is missing")
        connection = sqlite3.connect(":memory:")
        initialize_schema(connection)
    else:
        if not path.is_file():
            raise ValueError("Reflex database must be a regular file")
        from reflex.cli_workflows import guard_read_only_database
        guard_read_only_database(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


@app.command()
def apply(
    lesson_id: str | None = typer.Option(None),
    target: str | None = typer.Option(None, help="AGENTS.md, CLAUDE.md, or skills; defaults to the configured agent's instruction file."),
    repository: Path = typer.Option(Path(".")),
    preview: bool = typer.Option(False, help="Show the proposed change without applying."),
) -> None:
    """Apply a replay-confirmed lesson after review."""
    from reflex.application import preview_apply, commit_apply
    root = _project_root(repository)
    try:
        with closing(_read_project(root)) as connection:
            if lesson_id is None:
                rows = connection.execute("SELECT id,rule FROM lessons WHERE status='replay_confirmed' ORDER BY id").fetchall()
                typer.echo("Replay-confirmed Lessons:")
                for row in rows:
                    typer.echo(f"{row['id']}: {row['rule']}")
                if not rows:
                    typer.echo("None available.")
                typer.echo("Select one with --lesson-id; application requires a preview and confirmation.")
                return
            if target is None:
                from reflex.config.loader import load_config
                config_path = root / ".reflex/config.toml"
                if config_path.is_symlink():
                    raise ValueError("Reflex configuration must not be a symlink")
                agent = load_config(config_path).project.default_agent.casefold() if config_path.exists() else "codex"
                if agent not in {"codex", "claude"}:
                    raise ValueError("default agent must be codex or claude")
                target = "CLAUDE.md" if agent == "claude" else "AGENTS.md"
            destination = ".reflex/skills/" if target == "skills" else target
            proposal = preview_apply(connection, root, lesson_id, target=destination)
            typer.echo(f"Target: {proposal.repository / proposal.target}")
            typer.echo(proposal.diff or "No textual change proposed.")
            if proposal.already_applied:
                typer.echo("Already applied; no change.")
                return
        if preview:
            return
        if not typer.confirm("Apply this change?", default=False):
            typer.echo("Declined; no changes.")
            return
        # Revalidate storage after confirmation before opening it for writes.
        with closing(_read_project(root)):
            pass
        with closing(connect(root / ".reflex" / "reflex.db")) as connection:
            outcome = commit_apply(connection, proposal, confirmed=True)
        typer.echo(f"Application completed: {outcome.status}")
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        typer.echo(f"Apply failed: {error}", err=True)
        raise typer.Exit(1)


@app.command()
def stats(repository: Path = typer.Option(Path("."))) -> None:
    """Show measured Reflex statistics."""
    from reflex.output.stats import collect_stats
    root = _project_root(repository)
    try:
        with closing(_read_project(root, allow_empty=True)) as connection:
            values = collect_stats(connection)
    except (ValueError, OSError, sqlite3.Error) as error:
        typer.echo(f"Stats unavailable: {error}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Sessions analyzed: {values['sessions_analyzed']}")
    typer.echo(f"Failures detected: {values['failures_detected']}")
    for status, count in values['lessons'].items():
        typer.echo(f"Lessons {status}: {count}")
    for key in ("replay_comparisons", "valid_comparisons", "invalid_comparisons", "unevaluated_comparisons", "improvement_confirmed"):
        typer.echo(f"{key.replace('_', ' ').capitalize()}: {values[key]}")
    for arm, metrics in values['arms'].items():
        rate = metrics['success_rate']
        typer.echo(f"{arm} success: {metrics['success_count']}/{metrics['run_count']} ({'N/A' if rate is None else f'{rate:.1%}'})")
        for field, label in (("token_total", "tokens"), ("tool_calls", "tool calls"), ("elapsed_seconds", "elapsed seconds")):
            value = metrics[f'median_{field}']
            typer.echo(f"{arm} median {label}: {'N/A' if value is None else value} (available: {metrics[f'{field}_available_count']})")


if __name__ == "__main__":
    app()
