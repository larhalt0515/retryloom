"""Offline controlled pipeline evidence; never executes a live provider."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from typer.testing import CliRunner

from retryloom.main import app
from retryloom.output.stats import collect_stats
from retryloom.replay.evaluator import evaluate
from retryloom.replay.models import ReplayReport


@pytest.fixture(scope='module')
def benchmark(tmp_path_factory):
    path = Path(__file__).parents[1] / 'tools' / 'controlled_benchmark.py'
    spec = importlib.util.spec_from_file_location('phase11_controlled', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    root = tmp_path_factory.mktemp('phase11-parent') / 'trial'
    summary = module.run_benchmark(root)
    return root, summary, module


def artifact(benchmark, name):
    return json.loads((benchmark[0] / name).read_text())


def report(benchmark):
    return ReplayReport.model_validate(artifact(benchmark, 'replay.json'))


def test_recorded_report_is_valid_neutral_fixture(benchmark):
    recorded = report(benchmark)
    assert recorded.status == 'COMPLETED'
    decision = evaluate(recorded)
    assert decision.status == 'rejected'
    assert decision.control_success_count == decision.reflex_success_count == 0
    assert decision.condition is None
    assert len(recorded.runs) == 6
    assert recorded.metadata['manifest']['runs'] == 3
    assert recorded.metadata['manifest']['agent'] == 'synthetic-fixture'
    assert decision.model_dump(mode='json') == artifact(benchmark, 'evaluation.json')


def test_check_outcomes_not_fixture_self_report_determine_success(benchmark):
    for run in report(benchmark).runs:
        assert run.result.exit_code == 0
        assert not run.result.success
        assert 'REQUIRED_CHECK_FAILED' in run.statuses
        assert any(check.required and check.exit_code != 0 and not check.success for check in run.checks)
        assert all(not check.timed_out and not check.error for check in run.checks)


def test_no_fabricated_provider_metrics(benchmark):
    for run in report(benchmark).runs:
        assert run.result.agent == 'synthetic-fixture'
        assert run.result.model is None
        assert run.result.token_input is None
        assert run.result.token_output is None
        assert run.result.token_total is None
        assert run.result.tool_calls is None
        assert run.result.elapsed_seconds > 0


def test_frozen_lesson_matches_every_run(benchmark):
    lesson = artifact(benchmark, 'lesson.json')
    assert lesson['status'] == 'candidate'
    assert lesson['audit']['status'] == 'ACCEPT'
    assert not lesson['leak_suspect']
    assert lesson['lesson_hash']
    assert '```' not in lesson['rule']
    assert all(run.result.lesson_hash == lesson['lesson_hash'] for run in report(benchmark).runs)


def test_same_evidence_evaluates_identically_ten_times(benchmark):
    recorded = report(benchmark)
    before = recorded.model_dump_json()
    outputs = [evaluate(recorded).model_dump_json() for _ in range(10)]
    assert len(set(outputs)) == 1
    assert recorded.model_dump_json() == before


def test_session_failure_lesson_and_evaluation_persist(benchmark):
    db = sqlite3.connect((benchmark[0] / 'repository/.retryloom/retryloom.db').as_uri() + '?mode=ro', uri=True)
    try:
        for table in ('sessions', 'failures', 'lessons', 'evaluations'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM replay_runs').fetchone()[0] == 6
        assert db.execute('SELECT status FROM lessons').fetchone()[0] == 'candidate'
        assert db.execute('SELECT COUNT(*) FROM applications').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM session_events WHERE metadata_json LIKE ?', ('%source_record%',)).fetchone()[0] > 0
        assert db.execute('SELECT base_commit FROM sessions').fetchone()[0] == report(benchmark).metadata['resolved_base_commit']
    finally:
        db.close()


def test_stats_agree_with_persisted_evidence_and_remain_read_only(benchmark):
    root = benchmark[0] / 'repository'
    path = root / '.retryloom/retryloom.db'
    before = path.read_bytes()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        stats = collect_stats(db)
    assert stats == artifact(benchmark, 'stats.json')
    assert stats['valid_comparisons'] == 1
    assert stats['invalid_comparisons'] == stats['improvement_confirmed'] == 0
    for arm in ('CONTROL', 'REFLEX'):
        values = stats['arms'][arm]
        assert values['success_count'] == 0 and values['run_count'] == 3
        assert values['median_token_total'] is None
        assert values['median_tool_calls'] is None
        assert values['median_elapsed_seconds'] > 0
    result = CliRunner().invoke(app, ['stats', '--repository', str(root)])
    assert result.exit_code == 0, result.output
    assert 'CONTROL success: 0/3' in result.output
    assert 'REFLEX success: 0/3' in result.output
    assert 'median tokens: N/A' in result.output
    assert path.read_bytes() == before


def test_existing_trial_is_not_silently_replaced(benchmark):
    root, _, module = benchmark
    before = (root / 'replay.json').read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        module.run_benchmark(root)
    assert (root / 'replay.json').read_bytes() == before


def test_preregistered_inputs_and_only_lesson_difference(benchmark):
    registered = artifact(benchmark, 'preregistration.json')
    assert registered['evidence_category'] == 'synthetic fixture'
    assert registered['live_execution'] is False
    assert registered['case'] == artifact(benchmark, 'case.json')
    assert registered['lesson_hash'] == artifact(benchmark, 'lesson.json')['lesson_hash']
    calls = artifact(benchmark, 'calls.json')['calls']
    assert len(calls) == 6
    assert len({call['cwd'] for call in calls}) == 6
    assert len({call['home'] for call in calls}) == 6
    for index, call in enumerate(calls):
        assert call['task'] == registered['case']['task']
        assert call['head'] == registered['case']['base_commit']
        assert call['timeout_seconds'] == registered['case']['timeout_seconds']
        assert call['remotes'] == ''
        assert call['instructions'] == ([] if index < 3 else [registered['frozen_lesson']['rule']])
        assert call['runner_interprets_lesson'] is False
        assert not Path(call['cwd']).exists()
        assert not Path(call['home']).exists()
    isolated = {'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME',
                'XDG_DATA_HOME', 'CODEX_HOME', 'TMPDIR', 'TMP', 'TEMP', 'PYTHONPYCACHEPREFIX'}
    for index in range(3):
        left, right = calls[index]['environment'], calls[index + 3]['environment']
        assert left.keys() == right.keys()
        assert {k: v for k, v in left.items() if k not in isolated} == {
            k: v for k, v in right.items() if k not in isolated}


def test_original_repository_and_live_validation_labels(benchmark):
    state = artifact(benchmark, 'repository-state.json')
    assert state['unchanged'] is True
    assert state['before'] == state['after']
    summary = artifact(benchmark, 'summary.json')
    assert summary['result_classification'] == 'VALID NEUTRAL RESULT'
    assert summary['live_execution'] is False and summary['improvement_claim'] is False
    assert summary['live_codex_validation'] == 'INFRASTRUCTURE BLOCKED'
    assert summary['live_claude_validation'] == 'INFRASTRUCTURE BLOCKED'
    assert summary['cleanup_verified'] is True
