"""Evidence-backed lifecycle views without rewriting frozen Lesson snapshots."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

from retryloom.replay.evaluator import evaluate
from retryloom.storage.repository import _load_frozen_lesson, load_replay_report


@dataclass(frozen=True)
class LessonState:
    frozen_lesson: object
    effective_status: str
    evaluation_id: int | None = None
    report_hash: str | None = None
    application: dict | None = None


def load_lesson_state(connection, lesson_id):
    lesson = _load_frozen_lesson(connection, lesson_id, check_status=False)
    row = connection.execute('SELECT status,leak_suspect FROM lessons WHERE id=?', (lesson_id,)).fetchone()
    status = row[0]
    if status in ('candidate', 'rejected'):
        if status != lesson.status:
            raise ValueError('unsupported lifecycle state')
        return LessonState(lesson, status)
    if status not in ('replay_confirmed', 'applied') or lesson.status != 'candidate' or row[1]:
        raise ValueError('lesson lacks eligible frozen candidate/audit')
    proofs = []
    for record in connection.execute('SELECT * FROM evaluations WHERE status=? ORDER BY id', ('replay_confirmed',)):
        proof = dict(record)
        try:
            report = load_replay_report(connection, proof['report_id'])
            digest = hashlib.sha256(json.dumps(report.model_dump(mode='json'), sort_keys=True, allow_nan=False).encode()).hexdigest()
            manifest = report.metadata['manifest']
            case = connection.execute('SELECT repository,lesson_id,lesson_hash FROM replay_cases WHERE id=?', (report.case_id,)).fetchone()
            outcome = evaluate(report)
            if (digest != proof['report_hash'] or outcome.status != 'replay_confirmed' or
                json.loads(proof['evaluation_json']) != outcome.model_dump(mode='json') or
                proof['control_success'] != outcome.control_success_count or
                proof['reflex_success'] != outcome.reflex_success_count or
                proof['condition'] != outcome.condition or proof['reason'] != outcome.reason or
                proof['control_median_tokens'] != outcome.control_median_tokens or
                proof['reflex_median_tokens'] != outcome.reflex_median_tokens or
                proof['token_reduction_ratio'] != outcome.token_reduction_ratio or
                proof['required_check_regression'] != int(outcome.required_check_regression) or
                json.loads(proof['evidence_json']) != outcome.evidence or
                proof['replay_case_id'] != report.case_id or proof['manifest_hash'] != report.metadata['manifest_hash'] or
                manifest.get('lesson_id') != lesson.id or manifest.get('lesson_hash') != lesson.lesson_hash or
                not case or case[1] != lesson.id or case[2] != lesson.lesson_hash or
                Path(case[0]).resolve() != Path(lesson.trigger.repository).resolve()):
                continue
            proofs.append(proof)
        except (ValueError, KeyError, TypeError):
            continue
    if not proofs:
        raise ValueError('confirmed state lacks valid persisted evaluation evidence')
    has_applications = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='applications'").fetchone()
    app = connection.execute('SELECT * FROM applications WHERE lesson_id=?', (lesson_id,)).fetchone() if has_applications else None
    if status == 'replay_confirmed':
        if app:
            raise ValueError('application and lifecycle disagree')
        proof = proofs[-1]
        return LessonState(lesson, status, proof['id'], proof['report_hash'])
    if app is None:
        raise ValueError('applied state lacks application audit')
    app = dict(app)
    proof = next((p for p in proofs if p['id'] == app['evaluation_id'] and p['report_hash'] == app['report_hash']), None)
    def digest_valid(value):
        return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)

    try:
        timestamp_valid = datetime.fromisoformat(app['created_at']).tzinfo is not None
    except (ValueError, TypeError):
        timestamp_valid = False
    target = Path(app['target'])
    if (proof is None or app['lesson_hash'] != lesson.lesson_hash or
        app['frozen_fingerprint'] != lesson.canonical_fingerprint or
        Path(app['repository']).resolve() != Path(lesson.trigger.repository).resolve() or
        target.is_absolute() or '..' in target.parts or
        not (str(target) in ('AGENTS.md','CLAUDE.md') or target.parts[:2] == ('.retryloom','skills')) or
        app['from_status'] != 'replay_confirmed' or app['to_status'] != 'applied' or
        app['outcome'] not in ('applied','already_present') or not timestamp_valid or
        not all(digest_valid(app[key]) for key in ('after_hash','report_hash','lesson_hash','frozen_fingerprint')) or
        (app['before_hash'] is not None and not digest_valid(app['before_hash']))):
        raise ValueError('invalid application audit')
    return LessonState(lesson, status, proof['id'], proof['report_hash'], app)
