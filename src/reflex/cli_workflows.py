"""Small CLI boundaries around existing local evidence and replay APIs."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat

from reflex.analysis.corrections import extract_corrections
from reflex.analysis.failures import detect_failure
from reflex.analysis.lessons import generate_candidate
from reflex.config.loader import DEFAULT_CONFIG, load_config
from reflex.git.repository import ensure_repository
from reflex.replay.engine import ReplayEngine
from reflex.replay.evaluator import evaluate
from reflex.replay.isolation import git_command
from reflex.replay.models import ReplayCase
from reflex.storage.database import connect
from reflex.storage.repository import (
    load_failure, load_lesson, load_replay_report, load_session,
    prepare_lesson_for_replay, save_evaluation, save_failure, save_lesson,
)
from reflex.storage.schema import SCHEMA_VERSION

OWNED_DIRS = ("lessons", "replays", "reports", "logs")


def agent_cli_availability() -> dict[str, bool]:
    """PATH lookup only; never launch a provider for discovery."""
    return {agent: shutil.which(agent) is not None for agent in ("codex", "claude")}


def allowed_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
            if key in os.environ}


def project_root(repository: Path) -> Path:
    return ensure_repository(repository, env=allowed_environment())


def guard_storage(root: Path) -> Path:
    """Inspect lexical targets before SQLite or mkdir can follow them."""
    directory = root / ".reflex"
    targets = [(directory, True)]
    targets.extend((directory / name, True) for name in OWNED_DIRS)
    targets.extend((directory / name, False) for name in
                   ("config.toml", ".gitignore", "reflex.db", "reflex.db-wal",
                    "reflex.db-shm", "reflex.db-journal"))
    for path, is_directory in targets:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if is_directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"owned directory must be a real directory: {path}")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"owned file must be regular with one link: {path}")
    if directory.exists():
        def walk_error(error):
            raise error
        for parent, dirs, files in os.walk(directory, followlinks=False, onerror=walk_error):
            for name in dirs + files:
                path = Path(parent) / name
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError(f"unsafe Reflex descendant: {path}")
    return directory / "reflex.db"


def guard_read_only_database(path: Path) -> None:
    """Reject SQLite recovery modes that can write sidecars during a read."""
    with path.open("rb") as handle:
        header = handle.read(20)
    if len(header) >= 20 and header[18:20] != b"\x01\x01":
        raise ValueError("WAL/non-default journal mode is unsupported for read-only workflows; checkpoint and restore DELETE mode separately")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size):
            raise ValueError("active SQLite recovery state is unsupported for read-only workflows")


def read_database(root: Path) -> sqlite3.Connection:
    path = guard_storage(root)
    if not path.is_file():
        raise ValueError("Reflex database is missing; run init first")
    guard_read_only_database(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def write_database(root: Path) -> sqlite3.Connection:
    path = guard_storage(root)
    if not path.is_file():
        raise ValueError("Reflex database is missing; run init first")
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_project(repository: Path) -> Path:
    root = project_root(repository)
    path = guard_storage(root)
    config_path = path.parent / "config.toml"
    if config_path.exists():
        load_config(config_path)
    current = False
    if path.exists():
        with closing(read_database(root)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() if "metadata" in tables else None
            version = row[0] if row else None
            if version not in (None, "1", "2", "3", "4", str(SCHEMA_VERSION)):
                raise ValueError(f"unsupported schema version: {version}")
            if version is None and tables - {"metadata", "sqlite_sequence"}:
                raise ValueError("database has no recognized schema version")
            current = version == str(SCHEMA_VERSION)
    guard_storage(root)
    path.parent.mkdir(exist_ok=True)
    for name in OWNED_DIRS:
        (path.parent / name).mkdir(exist_ok=True)
    if not config_path.exists():
        try:
            with config_path.open("x", encoding="utf-8") as handle:
                handle.write(DEFAULT_CONFIG)
        except FileExistsError:
            # Another initializer may have created it after validation.
            guard_storage(root)
        load_config(config_path)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        with ignore.open("x", encoding="utf-8") as handle:
            handle.write("*\n")
    guard_storage(root)
    if not current:
        with closing(connect(path)):
            pass
    return root


def learn_session(repository: Path, session_id: str) -> list:
    root = project_root(repository)
    with closing(read_database(root)) as connection:
        session = load_session(connection, session_id)
        location = Path(session.repository)
        if not location.is_absolute() or project_root(location) != root:
            raise ValueError("session repository differs from selected repository")
        failure = detect_failure(session)
        if failure is None or failure.confidence not in {"MEDIUM", "HIGH"}:
            raise ValueError("session has no MEDIUM or HIGH confidence failure")
        evidence = extract_corrections(session, failure)
        groups = {}
        for item in evidence:
            groups.setdefault((item.repository, item.failure_type), []).append(item)
        candidates = [(generate_candidate(groups[key]), groups[key]) for key in sorted(groups)]
        candidates = [(candidate, group) for candidate, group in candidates if candidate is not None]
        if not candidates:
            raise ValueError("session has no complete correction and matching success evidence")
        previous = connection.execute("SELECT 1 FROM failures WHERE id=?", (failure.id,)).fetchone()
        if previous is not None and load_failure(connection, failure.id) != failure:
            raise ValueError("stored failure differs from current detection; history will not be rewritten")
    with closing(write_database(root)) as connection:
        if load_session(connection, session_id) != session:
            raise ValueError("session changed before learning")
        if previous is None:
            save_failure(connection, failure)
        elif load_failure(connection, failure.id) != failure:
            raise ValueError("failure changed before learning")
        return [save_lesson(connection, candidate, group) for candidate, group in candidates]


def selected_evidence(connection, lesson) -> list:
    sessions, failures = {}, {}
    evidence = []
    for ref in lesson.evidence_refs:
        if ref.session_id not in sessions:
            sessions[ref.session_id] = load_session(connection, ref.session_id)
        if ref.failure_id not in failures:
            failures[ref.failure_id] = load_failure(connection, ref.failure_id)
        matches = [item for item in extract_corrections(sessions[ref.session_id], failures[ref.failure_id])
                   if item.failure_type == lesson.trigger.failure_type and
                   (item.failure_event_ref, item.correction_event_ref, item.success_event_ref) ==
                   (ref.failure_event_ref, ref.correction_event_ref, ref.success_event_ref)]
        if len(matches) != 1:
            raise ValueError("frozen evidence reference no longer selects one correction")
        evidence.append(matches[0])
    return evidence


def _verify_case_identity(connection, root: Path, case: ReplayCase) -> None:
    existing = connection.execute("SELECT * FROM replay_cases WHERE id=?", (case.id,)).fetchone()
    if existing is not None:
        from json import loads
        recorded = dict(existing)
        same = all(recorded[name] == value for name, value in {
            "repository": str(root), "base_commit": case.base_commit, "task": case.task,
            "runs": case.runs, "lesson_id": case.lesson_id, "lesson_hash": case.lesson_hash,
            "timeout_seconds": case.timeout_seconds, "agent": case.agent,
            "agent_version": case.agent_version, "model": case.model,
        }.items()) and loads(recorded["model_settings_json"]) == case.model_settings
        checks = connection.execute("SELECT name,command,required,timeout_seconds FROM replay_checks WHERE replay_case_id=? ORDER BY id", (case.id,)).fetchall()
        same = same and [{"name": row[0], "command": loads(row[1]), "required": bool(row[2]),
                          "timeout_seconds": row[3]} for row in checks] == [check.model_dump() for check in case.checks]
        if not same:
            raise ValueError("case ID already records different inputs; use a fresh case ID")


def replay_preflight(repository: Path, case_path: Path):
    root = project_root(repository)
    local_config = git_command(root, allowed_environment(), "config", "--local", "--list")
    if any(line.casefold().startswith("extensions.partialclone=") or
           re.match(r"remote\..*\.promisor=", line, re.I)
           for line in local_config.splitlines()):
        raise ValueError("partial/promisor repositories are unsupported; replay must not fetch missing objects")
    info = case_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("case JSON must be a regular file with one link")
    case = ReplayCase.model_validate_json(case_path.read_text(encoding="utf-8"))
    if not case.repository.is_absolute() or case.repository.resolve() != root:
        raise ValueError("case repository must match the selected exact repository root")
    if project_root(case.repository) != case.repository.resolve():
        raise ValueError("case repository must be the Git top level")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", case.base_commit):
        raise ValueError("base_commit must be a full lowercase commit OID")
    if git_command(root, allowed_environment(), "rev-parse", "--verify", "--end-of-options",
                   case.base_commit + "^{commit}") != case.base_commit:
        raise ValueError("base_commit must identify the exact local commit")
    if case.agent != "codex":
        raise ValueError("only CodexRunner is supported; Claude replay is unavailable")
    if case.model_settings:
        raise ValueError("CodexRunner does not support nonempty model_settings")
    ReplayEngine._validate_plan(case)
    with closing(read_database(root)) as connection:
        _verify_case_identity(connection, root, case)
        lesson = load_lesson(connection, case.lesson_id)
        if lesson.status != "candidate":
            raise ValueError("replay requires a managed candidate lesson")
        if (lesson.scope != "repository" or not Path(lesson.trigger.repository).is_absolute() or
                Path(lesson.trigger.repository).resolve() != root):
            raise ValueError("lesson scope differs from the exact repository root")
        frozen = prepare_lesson_for_replay(connection, lesson.id, selected_evidence(connection, lesson))
        ReplayEngine._verify_lesson(case, frozen)
    return root, case, frozen


def execute_replay(root: Path, case: ReplayCase, frozen):
    from reflex.replay.runner import CodexRunner
    with closing(write_database(root)) as connection:
        database_path = root / ".reflex" / "reflex.db"
        identities = {path: (path.stat().st_dev, path.stat().st_ino)
                      for path in (root, database_path.parent, database_path)}
        _verify_case_identity(connection, root, case)
        lesson = load_lesson(connection, case.lesson_id)
        current = prepare_lesson_for_replay(connection, lesson.id, selected_evidence(connection, lesson))
        if lesson.status != "candidate" or current != frozen:
            raise ValueError("candidate changed after preflight")
        runner = CodexRunner(model=case.model, agent_version=case.agent_version)
        report = ReplayEngine(runner, connection, allowed_environment()).execute(case, frozen)
        if report.metadata.get("persistence_error"):
            raise RuntimeError(f"{report.status}: {report.reason}; {report.metadata['persistence_error']}")
        guard_storage(root)
        for path, identity in identities.items():
            info = path.lstat()
            if (info.st_dev, info.st_ino) != identity:
                raise RuntimeError("database ownership changed before evaluation persistence")
        stored = load_replay_report(connection, report.id)
        evaluation = evaluate(stored)
        guard_storage(root)
        evaluation_id = save_evaluation(connection, stored, evaluation)
        return stored, evaluation, evaluation_id
