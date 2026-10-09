"""Fair paired replay orchestration; completion never means lesson promotion."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any
from uuid import uuid4

from retryloom.git.repository import ensure_repository, repository_state
from retryloom.storage.database import initialize_schema
from retryloom.storage.repository import save_replay
from .checks import run_check
from .injection import FrozenLesson, instructions_for
from .isolation import OwnedDirectory, baseline_environment, create_clone, git_command, run_environment
from .models import ReplayCase, ReplayReport, ReplayRun, RunResult
from .runner import AgentRunner


class InvalidComparison(ValueError):
    pass


class ReplayEngine:
    def __init__(self, runner: AgentRunner, connection: sqlite3.Connection,
                 env: dict[str, str], temp_parent: Path | None = None):
        self.runner = runner
        self.connection = connection
        self.env = baseline_environment(dict(env))
        self.temp_parent = Path(temp_parent) if temp_parent is not None else None

    def execute(self, case: ReplayCase, lesson: FrozenLesson) -> ReplayReport:
        report = ReplayReport(id=str(uuid4()), case_id=case.id, status="INVALID_COMPARISON")
        source = before = None
        runner = self.runner
        # Revalidate because Pydantic model_copy(update=...) deliberately bypasses validation.
        try:
            plan = ReplayCase.model_validate(case.model_dump()).model_copy(deep=True)
        except Exception as error:
            report.reason = f"invalid replay case: {error}"
            # Malformed models cannot safely be inserted into the typed case schema.
            return report
        baseline = dict(self.env)
        manifest: dict[str, Any] = {
            "task": plan.task, "checks": [check.model_dump() for check in plan.checks],
            "timeout_seconds": plan.timeout_seconds, "agent": plan.agent,
            "agent_version": plan.agent_version, "model": plan.model,
            "model_settings": plan.model_settings, "lesson_id": plan.lesson_id,
            "lesson_hash": plan.lesson_hash, "requested_base_commit": plan.base_commit,
            "runs": plan.runs,
        }
        report.metadata.update(manifest=manifest, lint_scope="mechanical_lines_and_fences_only",
                               auth="isolated state; no host credentials copied",
                               isolation="state paths and Git history; not a hostile-code sandbox")
        observed: dict[str, str] = {}
        config: dict = {}
        stored_lesson = None
        storage_ready = False
        administrative_paths: tuple[Path, ...] = ()
        database_identities: dict[Path, tuple[int, int]] = {}
        try:
            source = ensure_repository(plan.repository, env=baseline)
            gitdir = Path(git_command(source, baseline, "rev-parse", "--absolute-git-dir")).resolve()
            common = Path(git_command(source, baseline, "rev-parse", "--git-common-dir"))
            common = (source / common).resolve() if not common.is_absolute() else common.resolve()
            administrative_paths = self._validate_database(source, gitdir, common, baseline)
            database_identities = self._database_identities(source)
            report.metadata["administrative_database_paths"] = [str(path.relative_to(source))
                                                                for path in administrative_paths]
            before = repository_state(source, env=baseline, excluded_paths=administrative_paths)
            initialize_schema(self.connection)
            storage_ready = True
            self._verify_lesson(plan, lesson)
            self._validate_plan(plan)
            stored_lesson = self._stored_lesson(plan, lesson)
            config = self._runner_configuration(runner)
            self._validate_runner(plan, config)
            for name in ("agent", "agent_version", "model"):
                if config.get(name) is not None:
                    observed[name] = config[name]
            parent = (self.temp_parent or Path(tempfile.gettempdir())).resolve()
            if any(parent.is_relative_to(protected) for protected in (source, gitdir, common)):
                raise InvalidComparison("temporary parent overlaps original worktree or Git directory")
            parent.mkdir(parents=True, exist_ok=True)
            if not parent.is_dir():
                raise InvalidComparison("temporary parent must be a directory")
            # Resolve the requested revision once. All runs use only this full OID.
            commit = git_command(source, baseline, "rev-parse", "--verify", "--end-of-options",
                                 plan.base_commit + "^{commit}")
            if len(commit) not in (40, 64) or any(char not in "0123456789abcdef" for char in commit):
                raise InvalidComparison("base revision did not resolve to a full commit OID")
            report.metadata.update(resolved_base_commit=commit, runner=config)
            manifest["base_commit"] = commit
            manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
            report.metadata["manifest_hash"] = manifest_hash
            for condition in ("CONTROL", "REFLEX"):
                for number in range(1, plan.runs + 1):
                    self._verify_lesson(plan, lesson)
                    if self._stored_lesson(plan, lesson) != stored_lesson:
                        raise InvalidComparison("stored lesson changed during replay")
                    self._assert_runner(runner, config)
                    run = self._execute_run(plan, lesson, condition, number, source, commit,
                                            parent, baseline, runner, manifest_hash)
                    report.runs.append(run)
                    if run.result.cleanup_error:
                        report.warnings.append(f"{condition} {number}: {run.result.cleanup_error}")
                    # Audit integrity before interpreting success or moving to the next arm.
                    self._verify_lesson(plan, lesson)
                    if self._stored_lesson(plan, lesson) != stored_lesson:
                        raise InvalidComparison("stored lesson changed during replay")
                    self._assert_runner(runner, config)
                    self._check_metadata(plan, run.result, observed, config)
                    if "INVALID_COMPARISON" in run.statuses:
                        raise InvalidComparison(run.result.agent_error or "run isolation failed")
                    if run.result.lesson_hash != plan.lesson_hash:
                        raise InvalidComparison("run lesson hash differs from the frozen lesson")
                if condition == "CONTROL":
                    failed = sum(not run.result.success for run in report.runs)
                    if failed <= plan.runs // 2:
                        report.status = "BASELINE_NOT_REPRODUCIBLE"
                        report.reason = "control failures did not form a strict majority"
                        break
                else:
                    report.status = "COMPLETED"
                    report.reason = "eligible paired replay completed; improvement has not been evaluated"
        except InvalidComparison as error:
            report.status = "INVALID_COMPARISON"
            report.reason = str(error)
        except ValueError as error:
            report.status = "INVALID_LESSON_HASH" if "lesson hash" in str(error) else "INVALID_COMPARISON"
            report.reason = str(error)
        except Exception as error:
            report.status = "INVALID_COMPARISON"
            report.reason = f"replay setup or audit failed: {type(error).__name__}: {error}"
        finally:
            if storage_ready or database_identities:
                try:
                    self._revalidate_database(source, gitdir, common, baseline,
                                              administrative_paths, database_identities)
                except Exception as error:
                    storage_ready = False
                    report.status = "INVALID_COMPARISON"
                    report.reason = f"database ownership changed before persistence: {error}"
            if stored_lesson is not None:
                try:
                    if self._stored_lesson(plan, lesson) != stored_lesson:
                        raise InvalidComparison("stored lesson changed during replay")
                except Exception as error:
                    report.status = "INVALID_LESSON_HASH" if "lesson hash" in str(error) else "INVALID_COMPARISON"
                    report.reason = str(error)
            if source is not None and before is not None:
                try:
                    if repository_state(source, env=baseline, excluded_paths=administrative_paths) != before:
                        report.status = "INVALID_COMPARISON"
                        report.reason = "original repository changed during replay; no restoration attempted"
                except Exception as error:
                    report.status = "INVALID_COMPARISON"
                    report.reason = f"could not verify original repository after replay: {error}"
            report.metadata["observed_agent_metadata"] = observed
            for name in ("agent_version", "model"):
                report.metadata[f"{name}_parity_verified"] = (
                    report.status == "COMPLETED" and bool(report.runs) and
                    (config.get(name) is not None or
                     all(getattr(run.result, name) is not None for run in report.runs))
                )
            report.metadata["cleanup_warning"] = bool(report.warnings)
            report.metadata["efficiency_usable"] = (
                report.status == "COMPLETED" and not report.warnings and
                all(run.result.efficiency_usable for run in report.runs)
            )
        if not storage_ready:
            report.warnings.append("report persistence skipped: database boundary or schema validation failed")
            report.metadata["persistence_error"] = "database boundary or schema validation failed"
        elif self.connection.execute("SELECT 1 FROM lessons WHERE id=?", (plan.lesson_id,)).fetchone() is None:
            report.warnings.append("report could not be persisted: lesson foreign-key target is missing")
            report.metadata["persistence_error"] = "missing stored lesson"
        else:
            save_replay(self.connection, plan, report)
            database_safe = False
            try:
                self._revalidate_database(source, gitdir, common, baseline,
                                          administrative_paths, database_identities)
                database_safe = True
                if source is not None and before is not None and repository_state(
                    source, env=baseline, excluded_paths=administrative_paths
                ) != before:
                    raise InvalidComparison("original repository changed during final persistence")
            except Exception as error:
                report.status = "INVALID_COMPARISON"
                report.reason = str(error)
                report.metadata["efficiency_usable"] = False
                report.metadata["agent_version_parity_verified"] = False
                report.metadata["model_parity_verified"] = False
                if database_safe:
                    with self.connection:
                        self.connection.execute("UPDATE replay_reports SET status=?,reason=?,metadata_json=? WHERE id=?",
                                                (report.status, report.reason, json.dumps(report.metadata, sort_keys=True), report.id))
                else:
                    report.warnings.append("database ownership changed after saving; unsafe database was not updated again")
                    report.metadata["persistence_error"] = "database ownership changed after saving"
        return report

    @staticmethod
    def _verify_lesson(plan: ReplayCase, lesson: FrozenLesson) -> None:
        lesson.verify()
        if lesson.lesson_hash != plan.lesson_hash:
            raise ValueError("lesson hash differs from the replay case")
        lines = lesson.text.splitlines()
        if (not lesson.text.strip() or
            any("```" in line or "~~~" in line for line in lines) or
            re.search(r"\b(?:lines?\s*\d+|L\d+)\b", lesson.text, re.IGNORECASE)):
            raise InvalidComparison("lesson failed mechanical lint: nonempty, no line references or code fences")

    def _validate_database(self, source: Path, gitdir: Path, common: Path,
                           env: dict[str, str]) -> tuple[Path, ...]:
        administrative_paths: list[Path] = []
        for _, _, filename in self.connection.execute("PRAGMA database_list"):
            if not filename:
                continue  # SQLite in-memory databases have no filesystem writes.
            lexical = Path(filename).absolute()
            # SQLite may canonicalize its filename when opening a symlink. Check
            # the administrative spelling too so that evidence is not lost.
            administrative = source / ".retryloom" / "retryloom.db"
            if ((administrative.is_symlink() or administrative.parent.is_symlink()) and
                administrative.resolve() == lexical.resolve()):
                raise InvalidComparison("administrative database file or parent is a symlink")
            # Inspect the spelling before resolve() can erase symlink evidence.
            for component in (lexical, *lexical.parents):
                if component.is_symlink():
                    raise InvalidComparison("replay database path contains a symlink")
            path = lexical.resolve()
            for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
                try:
                    info = candidate.lstat()
                except FileNotFoundError:
                    if candidate == path:
                        raise InvalidComparison("replay database file is missing")
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise InvalidComparison("replay database and sidecars must be regular files with one link")
            if path.is_relative_to(gitdir) or path.is_relative_to(common):
                raise InvalidComparison("replay database overlaps original Git state")
            if not path.is_relative_to(source):
                continue
            relative = path.relative_to(source)
            if relative != Path(".retryloom/retryloom.db"):
                raise InvalidComparison("in-source replay database must be the ignored .retryloom/retryloom.db administrative file")
            names = [str(relative) + suffix for suffix in ("", "-wal", "-shm", "-journal")]
            if git_command(source, env, "--literal-pathspecs", "ls-files", "-z", "--", *names):
                raise InvalidComparison("in-source replay database or sidecar is tracked")
            for name in names:
                try:
                    git_command(source, env, "check-ignore", "--no-index", "--", name)
                except RuntimeError as error:
                    raise InvalidComparison("in-source .retryloom database and sidecars must be Gitignored") from error
            administrative_paths.extend(source / name for name in names)
        return tuple(administrative_paths)

    def _database_identities(self, source: Path) -> dict[Path, tuple[int, int]]:
        identities = {}
        for _, _, filename in self.connection.execute("PRAGMA database_list"):
            if not filename:
                continue
            path = Path(filename).absolute()
            paths = [path]
            if path.is_relative_to(source):
                paths.extend(parent for parent in path.parents if parent.is_relative_to(source))
            for owned in paths:
                info = owned.lstat()
                identities[owned] = (info.st_dev, info.st_ino)
        return identities

    def _revalidate_database(self, source: Path, gitdir: Path, common: Path, env: dict[str, str],
                             administrative_paths: tuple[Path, ...],
                             identities: dict[Path, tuple[int, int]]) -> None:
        if self._validate_database(source, gitdir, common, env) != administrative_paths:
            raise InvalidComparison("administrative database paths changed")
        for path, expected in identities.items():
            info = path.lstat()
            if (info.st_dev, info.st_ino) != expected:
                raise InvalidComparison("database file or administrative parent was replaced")

    def _stored_lesson(self, plan: ReplayCase, lesson: FrozenLesson) -> tuple:
        row = self.connection.execute("SELECT rule,lesson_hash,leak_suspect FROM lessons WHERE id=?",
                                      (plan.lesson_id,)).fetchone()
        if row is None:
            raise InvalidComparison("replay lesson must already exist in local storage")
        # Phase8 snapshots bind scope, refs, and audit as well as injected text.
        from retryloom.storage.repository import verify_managed_lesson
        managed = verify_managed_lesson(self.connection, plan.lesson_id)
        if managed is not None and (managed.status == "rejected" or managed.audit.status == "REJECT"):
            raise InvalidComparison("stored managed lesson is rejected")
        if managed is not None:
            # Scope is the exact recorded repository directory, including when
            # it is a subdirectory; matching commit IDs do not widen that scope.
            if (managed.scope != "repository" or
                    Path(managed.trigger.repository).resolve() != plan.repository.resolve()):
                raise InvalidComparison("managed lesson repository scope differs from replay case")
        if row[2]:
            raise InvalidComparison("stored lesson is flagged leak_suspect; leakage audit required")
        if row[0] != lesson.text or row[1] != plan.lesson_hash:
            raise ValueError("stored lesson hash or text differs from the frozen payload")
        if managed is not None:
            resolutions = tuple(tuple(item) for item in self.connection.execute(
                "SELECT id,reviewer,reason,approved,canonical_fingerprint,evidence_fingerprint,audit_fingerprint "
                "FROM lesson_audit_resolutions WHERE lesson_id=? ORDER BY id", (plan.lesson_id,)))
            return (*tuple(row), managed.canonical_fingerprint, managed.status, resolutions)
        return tuple(row)

    @staticmethod
    def _validate_plan(plan: ReplayCase) -> None:
        if not plan.task.strip() or not plan.checks or not any(check.required for check in plan.checks):
            raise InvalidComparison("replay requires a nonempty task and at least one required automatic check")
        if any(not check.command or any(not part or "\0" in part for part in check.command)
               for check in plan.checks):
            raise InvalidComparison("automatic checks require nonempty executable commands")
        for timeout in (plan.control_timeout_seconds, plan.reflex_timeout_seconds):
            if timeout is not None and timeout != plan.timeout_seconds:
                raise InvalidComparison("control and retryloom timeouts must equal the common timeout")
        # Require a deterministic serializable configuration before starting processes.
        json.dumps(plan.model_settings, sort_keys=True, allow_nan=False)

    @staticmethod
    def _runner_configuration(runner: AgentRunner) -> dict:
        validate = getattr(runner, "validate_configuration", None)
        if validate is not None:
            validate()
        result = {"type": f"{type(runner).__module__}.{type(runner).__qualname__}"}
        for name in ("agent", "executable", "agent_version", "agent_version_source", "model", "model_settings"):
            value = getattr(runner, name, None)
            if value is not None:
                result[name] = value
        return json.loads(json.dumps(result, sort_keys=True, allow_nan=False))

    @staticmethod
    def _validate_runner(plan: ReplayCase, config: dict) -> None:
        for name in ("agent", "agent_version", "model"):
            expected = getattr(plan, name)
            if expected is not None and config.get(name) is not None and expected != config[name]:
                raise InvalidComparison(f"runner {name} differs from the replay case")
        if plan.model_settings != config.get("model_settings", {}):
            raise InvalidComparison("runner settings differ from the replay case")

    def _assert_runner(self, runner: AgentRunner, config: dict) -> None:
        if self.runner is not runner or self._runner_configuration(runner) != config:
            raise InvalidComparison("runner configuration changed during replay")

    @staticmethod
    def _check_metadata(plan: ReplayCase, result: RunResult, observed: dict[str, str], config: dict) -> None:
        for name in ("agent", "agent_version", "model"):
            actual = getattr(result, name)
            expected = getattr(plan, name)
            if actual is None:
                if name in ("model", "agent_version") and expected is not None and config.get(name) != expected:
                    raise InvalidComparison(f"requested {name} is not attested by runner configuration or run metadata")
                continue
            if (expected is not None and actual != expected) or (name in observed and actual != observed[name]):
                raise InvalidComparison(f"available {name} metadata differs across the comparison")
            observed[name] = actual

    @staticmethod
    def _execute_run(plan: ReplayCase, lesson: FrozenLesson, condition: str, number: int,
                     source: Path, commit: str, parent: Path, baseline: dict[str, str],
                     runner: AgentRunner, manifest_hash: str) -> ReplayRun:
        start = time.monotonic()
        owned = None
        seed = number - 1
        checks = []
        statuses: list[str] = []
        result = RunResult(success=False, elapsed_seconds=0, stdout="", stderr="",
                           lesson_hash=plan.lesson_hash, efficiency_usable=False)
        tool_env_keys: list[str] = []
        agent_started = False
        try:
            owned = OwnedDirectory.create(parent)
            env = run_environment(owned.path, baseline, seed)
            tool_env_keys = sorted(env)
            cwd = create_clone(source, owned.path, commit, env)
            injected = instructions_for(condition, lesson)
            agent_started = True
            try:
                result = RunResult.model_validate(runner.run(plan.task, cwd, injected,
                                                             plan.timeout_seconds, dict(env)))
            except Exception as error:
                result.agent_error = f"{type(error).__name__}: {error}"
                result.stderr = result.agent_error
            result = result.model_copy(deep=True)
            if result.lesson_hash is None:
                result.lesson_hash = plan.lesson_hash
            if result.timed_out:
                statuses.append("TIMEOUT")
            elif result.agent_error or not result.success or (result.exit_code is not None and result.exit_code != 0):
                statuses.append("AGENT_ERROR")
            # Checks also run after agent failure, preserving all observable diagnostics.
            checks = [run_check(check, cwd, dict(env)) for check in plan.checks]
            required_failures = [check for check in checks if check.required and not check.success]
            if required_failures:
                statuses.append("REQUIRED_CHECK_FAILED")
            if any(check.required and check.timed_out for check in checks) and "TIMEOUT" not in statuses:
                statuses.insert(0, "TIMEOUT")
            result.success = result.success and not result.timed_out and not result.agent_error and not required_failures
            if result.exit_code is not None and result.exit_code != 0:
                result.success = False
            result.efficiency_usable = result.efficiency_usable and "TIMEOUT" not in statuses and "AGENT_ERROR" not in statuses
            for check in checks:
                if check.cleanup_error:
                    result.cleanup_error = "; ".join(filter(None, [result.cleanup_error, check.cleanup_error]))
        except Exception as error:
            result.success = False
            result.efficiency_usable = False
            result.agent_error = f"{type(error).__name__}: {error}"
            result.stderr = "\n".join(filter(None, [result.stderr, result.agent_error]))
            statuses.append("AGENT_ERROR" if agent_started else "INVALID_COMPARISON")
        finally:
            if owned is not None:
                try:
                    cleanup_error = owned.cleanup()
                except Exception as error:
                    cleanup_error = f"cleanup failed: {type(error).__name__}: {error}"
                if cleanup_error:
                    result.cleanup_error = "; ".join(filter(None, [result.cleanup_error, cleanup_error]))
            if result.cleanup_error:
                statuses.append("CLEANUP_WARNING")
            if not statuses:
                statuses.append("COMPLETED")
        return ReplayRun(id=str(uuid4()), condition=condition, run_number=number,
                         result=result, checks=checks, status=statuses[0], statuses=statuses, seed=seed,
                         metadata={"manifest_hash": manifest_hash, "tool_environment_keys": tool_env_keys,
                                   "seed_policy": "paired run index; PYTHONHASHSEED and RETRYLOOM_RUN_SEED",
                                   "base_commit": commit, "total_elapsed_seconds": time.monotonic() - start})
