"""Opt-in, offline synthetic integration; never evidence of live agent improvement.

Run with the project environment: python -B tools/controlled_benchmark.py
--output /path/to/a/new-directory --execute. Existing nonempty trials are refused.
The fixed runner intentionally ignores Lessons and makes the same inadequate edit
in both arms. Actual unittest outcomes, not fixture flags, determine success.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from retryloom import __version__
from retryloom.adapters.codex import CodexAdapter
from retryloom.analysis.corrections import extract_corrections
from retryloom.analysis.failures import detect_failure
from retryloom.analysis.lessons import generate_candidate
from retryloom.git.repository import repository_state
from retryloom.output.stats import collect_stats
from retryloom.replay.engine import ReplayEngine
from retryloom.replay.evaluator import evaluate
from retryloom.replay.isolation import create_clone, git_command, run_environment
from retryloom.replay.models import ReplayCase, ReplayCheck, RunResult
from retryloom.replay.subprocess import execute_process
from retryloom.storage.database import connect
from retryloom.storage.repository import (
    load_lesson, load_replay_report, prepare_lesson_for_replay, save_evaluation,
    save_failure, save_lesson, save_session,
)

LABEL = "synthetic fixture"
PYTHON = str(Path(sys.executable).resolve())
TASK = "Correct parse_row to handle quoted commas, escaped quotes, and trim field whitespace; verify with unittest."
OLD_PARSER = "def parse_row(row):\n    return row.split(',')\n"
NAIVE_PARSER = "def parse_row(row):\n    return [field.strip() for field in row.split(',')]\n"
# This historical correction is used ONLY in the separate disposable evidence
# clone. It is never passed to the runner or copied into a measured clone.
HISTORICAL_CORRECTION = (
    "import csv\n\ndef parse_row(row):\n"
    "    return [field.strip() for field in next(csv.reader([row]))]\n"
)
TESTS = '''import unittest
from parser import parse_row

class CsvChecks(unittest.TestCase):
    def test_quoted_comma(self):
        self.assertEqual(parse_row('"one,two",three'), ['one,two', 'three'])
    def test_escaped_quotes(self):
        self.assertEqual(parse_row('"say ""hi""",end'), ['say "hi"', 'end'])
    def test_whitespace(self):
        self.assertEqual(parse_row('  one  , two '), ['one', 'two'])

if __name__ == '__main__':
    unittest.main()
'''
FIXTURE_PROGRAM = (
    "from pathlib import Path\n"
    f"Path('parser.py').write_text({NAIVE_PARSER!r}, encoding='utf-8')\n"
    "print('Synthetic fixture: applied fixed whitespace-only edit; no task-success claim.')\n"
)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeError(reason)


def _write(path: Path, value) -> None:
    # Never replace an artifact from an earlier trial.
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write('\n')


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SyntheticRunner:
    agent = 'synthetic-fixture'
    agent_version = '1'
    model = None
    model_settings = {}

    def __init__(self, calls: list[dict]):
        # Evidence observations are separate from the stable runner configuration.
        self.calls = calls

    def run(self, task: str, cwd: Path, instructions: list[str],
            timeout_seconds: int, env: dict[str, str]) -> RunResult:
        self.calls.append({
            'evidence_category': LABEL, 'task': task, 'instructions': list(instructions),
            'cwd': str(cwd), 'home': env['HOME'], 'head': git_command(cwd, env, 'rev-parse', 'HEAD'),
            'remotes': git_command(cwd, env, 'remote'), 'timeout_seconds': timeout_seconds,
            'environment': dict(env), 'runner_interprets_lesson': False,
        })
        result = execute_process([PYTHON, '-B', '-c', FIXTURE_PROGRAM],
                                 cwd, env, timeout_seconds)
        return RunResult(
            success=result.success, exit_code=result.exit_code,
            elapsed_seconds=result.elapsed_seconds, stdout=result.stdout, stderr=result.stderr,
            timed_out=result.timed_out, cleanup_error=result.cleanup_error,
            agent_error=result.error, output_truncated=result.output_truncated,
            efficiency_usable=not result.timed_out and not result.error and not result.cleanup_error,
            agent=self.agent, agent_version=self.agent_version, model=None,
            token_input=None, token_output=None, token_total=None, tool_calls=None,
        )


def run_benchmark(root: Path) -> dict:
    """Create one retained synthetic trial; refuse reuse or evidence replacement.

    Returns summary with status/result_classification/control_success_count/
    reflex_success_count and artifact/database paths. No live/network execution.
    """
    root = Path(root).absolute()
    source_root = Path(__file__).resolve().parents[1]
    if root.resolve().is_relative_to(source_root):
        raise ValueError('benchmark output must be outside the Retryloom source repository')
    if root.is_symlink():
        raise ValueError('output root must not be a symlink')
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise ValueError('output root must be new or empty; existing trials are never discarded')
    else:
        root.mkdir(parents=True, exist_ok=False)
    # Refuse an output directory inside Retryloom; benchmark repository is separate.
    root = root.resolve()
    _write(root / 'trial.json', {'evidence_category': LABEL, 'created_at': datetime.now(timezone.utc).isoformat(),
                              'live_execution': False, 'historical_agent_session': False})
    baseline = {'PATH': os.defpath, 'LANG': 'C', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}
    # Include the installed Git search path without inheriting secrets/config.
    baseline['PATH'] = '/opt/homebrew/bin:' + os.defpath
    setup = root / 'setup'
    setup.mkdir()
    setup_env = run_environment(setup, baseline, 0)
    repository = root / 'repository'
    repository.mkdir()
    git_command(repository, setup_env, 'init', '--template=', '.')
    (repository / 'parser.py').write_text(OLD_PARSER, encoding='utf-8')
    (repository / 'test_parser.py').write_text(TESTS, encoding='utf-8')
    (repository / '.gitignore').write_text('.retryloom/\n__pycache__/\n', encoding='utf-8')
    git_command(repository, setup_env, 'add', 'parser.py', 'test_parser.py', '.gitignore')
    git_command(repository, setup_env, '-c', 'user.name=Retryloom Synthetic Fixture',
                '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Predeclared synthetic CSV base')
    commit = git_command(repository, setup_env, 'rev-parse', 'HEAD')
    check_command = [PYTHON, '-B', '-m', 'unittest', '-v']

    historical_root = root / 'synthetic-evidence'
    historical_root.mkdir()
    historical_env = run_environment(historical_root, baseline, 0)
    historical_clone = create_clone(repository, historical_root, commit, historical_env)
    records = []

    def record(kind: str, payload: dict) -> None:
        records.append({'type': kind, 'timestamp': datetime.now(timezone.utc).isoformat(),
                        'payload': payload})

    record('session_meta', {'id': 'synthetic-csv-evidence', 'cwd': str(repository),
                           'timestamp': datetime.now(timezone.utc).isoformat(),
                           'git': {'commit_hash': commit}, 'cli_version': None,
                           'evidence_category': LABEL, 'actual_measurement_cwd': str(historical_clone),
                           'not_a_real_codex_session': True})
    record('event_msg', {'type': 'user_message', 'message': TASK, 'evidence_category': LABEL})

    def measure(call_id: str):
        record('response_item', {'type': 'function_call', 'name': 'exec_command',
                                'call_id': call_id, 'arguments': json.dumps({'cmd': ' '.join(check_command)})})
        measurement = execute_process(check_command, historical_clone, historical_env, 10)
        _require(not measurement.timed_out and not measurement.error and not measurement.cleanup_error,
                 'synthetic historical measurement infrastructure failed')
        record('response_item', {'type': 'function_call_output', 'call_id': call_id,
                                'content': measurement.stdout + measurement.stderr,
                                'exit_code': measurement.exit_code,
                                'evidence_category': LABEL, 'measurement': asdict(measurement)})
        return measurement

    failed = measure('synthetic-failed-check')
    _require(failed.exit_code not in (None, 0), 'predeclared source failure did not reproduce')
    patch = '--- parser.py\n+++ parser.py\n' + ''.join('-' + line + '\n' for line in OLD_PARSER.splitlines())
    patch += ''.join('+' + line + '\n' for line in HISTORICAL_CORRECTION.splitlines())
    (historical_clone / 'parser.py').write_text(HISTORICAL_CORRECTION, encoding='utf-8')
    record('response_item', {'type': 'file_edit', 'content': patch, 'evidence_category': LABEL,
                             'actual_measurement_cwd': str(historical_clone)})
    passed = measure('synthetic-corrected-check')
    _require(passed.success, 'manual synthetic correction did not pass the same check')
    transcript = historical_root / 'synthetic-codex-shaped.jsonl'
    with transcript.open('x', encoding='utf-8') as handle:
        for item in records:
            handle.write(json.dumps(item, sort_keys=True) + '\n')
    session = CodexAdapter(historical_root).parse(transcript)
    failure = detect_failure(session)
    _require(failure is not None, 'canonical detector found no failure')
    corrections = extract_corrections(session, failure)
    candidate = generate_candidate(corrections)
    _require(candidate is not None, 'canonical correction extraction/generation failed')
    database = repository / '.retryloom' / 'retryloom.db'
    connection = connect(database)
    try:
        save_session(connection, session)
        save_failure(connection, failure)
        lesson = save_lesson(connection, candidate, corrections)
        frozen = prepare_lesson_for_replay(connection, lesson.id, corrections)
        source_hashes = {str(path.relative_to(source_root)): _sha(path.read_bytes())
                         for path in sorted((source_root / 'src' / 'retryloom').rglob('*.py'))
                         if not path.is_symlink()}
        try:
            parent_commit = git_command(source_root, setup_env, 'rev-parse', 'HEAD')
        except RuntimeError:
            parent_commit = None
        try:
            git_command(source_root, setup_env, 'ls-files', '--error-unmatch', 'pyproject.toml')
            retryloom_tracked = True
        except RuntimeError:
            retryloom_tracked = False
        case = ReplayCase(id='synthetic-csv-neutral-v1', repository=repository, base_commit=commit,
                          task=TASK, checks=[ReplayCheck(name='CSV unittest', command=check_command,
                                                        required=True, timeout_seconds=10)],
                          runs=3, timeout_seconds=10, lesson_id=lesson.id, lesson_hash=frozen.lesson_hash,
                          agent=SyntheticRunner.agent, agent_version='1', model=None, model_settings={})
        _write(root / 'preregistration.json', {
            'evidence_category': LABEL, 'live_execution': False, 'case': case.model_dump(mode='json'),
            'frozen_lesson': lesson.model_dump(mode='json'), 'lesson_hash': frozen.lesson_hash,
            'runner_code_hash': _sha(FIXTURE_PROGRAM.encode()), 'script_hash': _sha(Path(__file__).read_bytes()),
            'retryloom_version': __version__, 'python_version': sys.version,
            'python_executable': PYTHON, 'retryloom_source_files_sha256': source_hashes,
            'retryloom_source_tree_hash': _sha(json.dumps(source_hashes, sort_keys=True).encode()),
            'parent_git_commit': parent_commit, 'retryloom_project_tracked': retryloom_tracked,
            'commit_provenance_note': 'Parent Git commit is not a Retryloom release commit; source hashes identify untracked Retryloom code.' if not retryloom_tracked else 'Source hashes identify the working implementation.',
            'expected_result': 'VALID NEUTRAL RESULT', 'expected_control_success_count': 0,
            'expected_reflex_success_count': 0, 'runner_interprets_lesson': False,
            'intentional_condition_difference': 'exact frozen Lesson injection only',
            'order': ['CONTROL'] * 3 + ['REFLEX'] * 3,
            'allowed_environment_differences': ['per-run isolated state paths', 'paired run-index seed'],
            'no_setup_runs_counted': True,
            'live_codex_validation': {'status': 'INFRASTRUCTURE BLOCKED',
                'reason': 'Parent sandbox prohibits nested sandbox-exec; installed CLI help does not document authenticated network execution with sufficient filesystem confinement.',
                'classification': 'validation gap, not Retryloom implementation failure',
                'production_changes_caused_by_failed_smokes': 'None'},
            'live_claude_validation': {'status': 'INFRASTRUCTURE BLOCKED',
                'reason': 'Equivalent filesystem confinement has not been established; no live Claude execution attempted.'},
        })
        excluded = tuple(Path(str(database) + suffix) for suffix in ('', '-wal', '-shm', '-journal'))
        before = repository_state(repository, setup_env, excluded_paths=excluded)
        calls: list[dict] = []
        replay_temp = root / 'replay-temporary'
        replay_temp.mkdir()
        report = ReplayEngine(SyntheticRunner(calls), connection, baseline, replay_temp).execute(case, frozen)
        _write(root / 'replay.json', report.model_dump(mode='json'))
        _write(root / 'calls.json', {'evidence_category': LABEL, 'calls': calls})
        unchanged = repository_state(repository, setup_env, excluded_paths=excluded) == before
        _write(root / 'repository-state.json', {'before': asdict(before), 'after': asdict(repository_state(
            repository, setup_env, excluded_paths=excluded)), 'unchanged': unchanged,
            'excluded_administrative_paths': [str(path) for path in excluded]})
        _require(unchanged, 'original benchmark repository changed')
        stored = load_replay_report(connection, report.id)
        _require(stored == report, 'persisted Replay evidence differs')
        dump_before = '\n'.join(connection.iterdump())
        evaluation = evaluate(stored)
        _require(all(evaluate(stored) == evaluation for _ in range(10)), 'evaluation nondeterminism')
        _require('\n'.join(connection.iterdump()) == dump_before, 'evaluation mutated evidence')
        save_evaluation(connection, stored, evaluation)
        _write(root / 'evaluation.json', evaluation.model_dump(mode='json'))
        _require(report.status == 'COMPLETED' and evaluation.status == 'rejected',
                 f'invalid/unexpected predeclared comparison: {report.status}/{evaluation.status}')
        _require(evaluation.control_success_count == evaluation.reflex_success_count == 0,
                 'predeclared neutral outcome did not occur; do not rescue or discard trial')
        _require(len(calls) == 6 and len({call['cwd'] for call in calls}) == 6
                 and len({call['home'] for call in calls}) == 6, 'run isolation proof failed')
        _require(all(call['head'] == commit and not call['remotes'] for call in calls), 'clone provenance failed')
        _require(all(call['instructions'] == ([] if i < 3 else [frozen.text])
                     for i, call in enumerate(calls)), 'Lesson injection parity failed')
        _require(all(not Path(call[key]).exists() for call in calls for key in ('cwd', 'home'))
                 and not any(replay_temp.iterdir()) and not report.warnings, 'Replay cleanup failed')
        _require(load_lesson(connection, lesson.id) == lesson and lesson.lesson_hash == frozen.lesson_hash,
                 'frozen Lesson changed')
        _require(all(run.result.token_total is None and run.result.tool_calls is None
                     for run in stored.runs), 'fixture fabricated unavailable metrics')
        _write(root / 'case.json', case.model_dump(mode='json'))
        _write(root / 'lesson.json', lesson.model_dump(mode='json'))
        _write(root / 'session.json', session.model_dump(mode='json'))
        _write(root / 'failure.json', failure.model_dump(mode='json'))
    finally:
        connection.close()
    database_hash = _sha(database.read_bytes())
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as readonly:
        readonly.execute('PRAGMA query_only=ON')
        stats = collect_stats(readonly)
    _require(_sha(database.read_bytes()) == database_hash, 'Stats mutated persisted database')
    _require(stats['sessions_analyzed'] == stats['failures_detected'] == 1
             and stats['lessons']['candidate'] == 1 and stats['valid_comparisons'] == 1
             and stats['improvement_confirmed'] == 0, 'Stats evidence counts disagree')
    for arm in ('CONTROL', 'REFLEX'):
        values = stats['arms'][arm]
        _require(values['run_count'] == 3 and values['success_count'] == 0
                 and values['median_token_total'] is None and values['median_tool_calls'] is None
                 and values['elapsed_seconds_available_count'] == 3, 'Stats measured metrics disagree')
    _write(root / 'stats.json', stats)
    summary = {
        'status': 'COMPLETED', 'result_classification': 'VALID NEUTRAL RESULT',
        'evidence_category': LABEL, 'live_execution': False,
        'control_success_count': evaluation.control_success_count,
        'reflex_success_count': evaluation.reflex_success_count,
        'control_run_count': 3, 'reflex_run_count': 3, 'base_commit': commit,
        'lesson_hash': frozen.lesson_hash, 'lesson_status': lesson.status,
        'evaluation_status': evaluation.status, 'report_id': report.id,
        'report_status': report.status, 'baseline_valid': True, 'comparison_valid': True,
        'original_repository_unchanged': unchanged, 'cleanup_verified': True,
        'deterministic_evaluations': 10, 'stats_verified': True,
        'output': str(root), 'database': str(database), 'repository': str(repository),
        'live_codex_validation': 'INFRASTRUCTURE BLOCKED',
        'live_claude_validation': 'INFRASTRUCTURE BLOCKED',
        'improvement_claim': False,
    }
    _write(root / 'summary.json', summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true', help='Explicitly authorize one local synthetic trial')
    arguments = parser.parse_args()
    if not arguments.execute:
        parser.error('--execute is required; no trial was run')
    print(json.dumps(run_benchmark(arguments.output), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
