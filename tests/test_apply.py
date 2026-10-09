"""Apply evidence, confirmation and filesystem boundaries."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from retryloom.application import preview_apply, commit_apply
from retryloom.storage.lifecycle import load_lesson_state
from retryloom.storage.database import connect
from retryloom.storage.repository import save_lesson, save_evaluation, save_replay
from test_lesson_storage import setup
from test_evaluation_storage import recorded_report


@pytest.fixture
def eligible(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()
    db = connect(tmp_path / 'db.sqlite')
    lesson, evidence = setup(db)
    lesson = lesson.model_copy(update={'trigger': lesson.trigger.model_copy(update={'repository': str(root)})})
    # Evidence repository must match candidate generation context.
    db.execute('UPDATE sessions SET repository=?', (str(root),))
    db.commit()
    from retryloom.analysis.lessons import generate_candidate
    evidence = [item.model_copy(update={'repository': str(root)}) for item in evidence]
    lesson = save_lesson(db, generate_candidate(evidence), evidence)
    case, report = recorded_report()
    case = case.model_copy(update={'repository': str(root), 'lesson_id': lesson.id, 'lesson_hash': lesson.lesson_hash})
    manifest = report.metadata['manifest']
    manifest.update(lesson_id=lesson.id, lesson_hash=lesson.lesson_hash)
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    report.metadata['manifest_hash'] = digest
    for run in report.runs:
        run.metadata['manifest_hash'] = digest
        run.result.lesson_hash = lesson.lesson_hash
    save_replay(db, case, report)
    from retryloom.replay.evaluator import evaluate
    evaluation = evaluate(report)
    assert evaluation.status == 'replay_confirmed'
    save_evaluation(db, report, evaluation)
    db.execute("UPDATE lessons SET status='replay_confirmed' WHERE id=?", (lesson.id,))
    db.commit()
    return db, root, lesson


@pytest.mark.parametrize('target', ['AGENTS.md','CLAUDE.md','.retryloom/skills/'])
def test_confirmation_preserves_files_freeze_and_proof(eligible, target):
    db, root, lesson = eligible
    path = root / target
    if target != '.retryloom/skills/':
        path.write_bytes(b'# Existing instructions\nKeep this.\n')
    tables = ('lesson_freezes','lesson_audits','replay_reports','replay_runs','evaluations')
    snapshots = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t}')] for t in tables}
    preview = preview_apply(db, root, lesson.id, target)
    before = list(root.rglob('*'))
    assert preview.diff and not preview.already_applied
    assert commit_apply(db, preview, False).status == 'declined'
    assert list(root.rglob('*')) == before
    assert load_lesson_state(db, lesson.id).effective_status == 'replay_confirmed'
    assert commit_apply(db, preview, True).status == 'applied'
    assert (root / preview.target).read_bytes() == preview.after
    if target != '.retryloom/skills/':
        assert preview.after.startswith(b'# Existing instructions\nKeep this.\n')
    state = load_lesson_state(db, lesson.id)
    assert state.effective_status == 'applied' and state.frozen_lesson == lesson
    again = preview_apply(db, root, lesson.id, target)
    assert commit_apply(db, again, True).status == 'already_applied'
    assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 1
    for table in tables:
        assert [tuple(r) for r in db.execute(f'SELECT * FROM {table}')] == snapshots[table]


@pytest.mark.parametrize('status',['candidate','rejected'])
def test_ineligible(eligible,status):
    db, root, lesson = eligible
    db.execute('UPDATE lessons SET status=? WHERE id=?',(status,lesson.id)); db.commit()
    with pytest.raises(ValueError): preview_apply(db,root,lesson.id)


@pytest.mark.parametrize('target',['../AGENTS.md','/tmp/AGENTS.md','other.md','.retryloom/skills/../evil.md'])
def test_unsafe_target(eligible,target):
    db, root, lesson = eligible
    with pytest.raises(ValueError): preview_apply(db,root,lesson.id,target)


def test_symlink_and_hardlink_and_directory(eligible,tmp_path):
    db, root, lesson = eligible
    outside = tmp_path/'outside'; outside.write_text('private')
    target=root/'AGENTS.md'; target.symlink_to(outside)
    with pytest.raises(ValueError): preview_apply(db,root,lesson.id)
    target.unlink()
    import os
    os.link(outside,target)
    with pytest.raises(ValueError): preview_apply(db,root,lesson.id)
    target.unlink(); target.mkdir()
    with pytest.raises(ValueError): preview_apply(db,root,lesson.id)
    assert outside.read_text() == 'private'


def test_parent_symlink(eligible,tmp_path):
    db,root,lesson=eligible
    outside=tmp_path/'elsewhere';outside.mkdir()
    (root/'.retryloom').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError): preview_apply(db,root,lesson.id,'.retryloom/skills/')
    assert list(outside.iterdir()) == []


def test_changed_target(eligible):
    db,root,lesson=eligible
    preview=preview_apply(db,root,lesson.id)
    (root/'AGENTS.md').write_text('concurrent change')
    with pytest.raises(ValueError): commit_apply(db,preview,True)
    assert (root/'AGENTS.md').read_text() == 'concurrent change'
    assert load_lesson_state(db,lesson.id).effective_status == 'replay_confirmed'


def test_missing_proof_rejected(eligible):
    db,root,lesson=eligible
    db.execute('DELETE FROM evaluations');db.commit()
    with pytest.raises(ValueError,match='evidence'): preview_apply(db,root,lesson.id)


def test_database_failure_restores_file(eligible):
    db,root,lesson=eligible
    (root/'AGENTS.md').write_text('original')
    preview=preview_apply(db,root,lesson.id)
    db.execute("CREATE TRIGGER deny_apply BEFORE INSERT ON applications BEGIN SELECT RAISE(ABORT,'injected'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError): commit_apply(db,preview,True)
    assert (root/'AGENTS.md').read_text() == 'original'
    assert load_lesson_state(db,lesson.id).effective_status == 'replay_confirmed'
    assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 0


def test_write_failure(eligible,monkeypatch):
    db,root,lesson=eligible
    preview=preview_apply(db,root,lesson.id)
    def fail(*args): raise OSError('write failed')
    monkeypatch.setattr('retryloom.application._replace_at',fail)
    with pytest.raises(OSError): commit_apply(db,preview,True)
    assert not (root/'AGENTS.md').exists()
    assert load_lesson_state(db,lesson.id).effective_status == 'replay_confirmed'


def test_verification_failure_preserves_concurrent_edit(eligible,monkeypatch):
    db,root,lesson=eligible
    preview=preview_apply(db,root,lesson.id)
    import retryloom.application as application
    original=application._replace_at
    def concurrent(fd,name,content,mode,*guard):
        original(fd,name,content,mode,*guard)
        (root/'AGENTS.md').write_text('concurrent owner')
    monkeypatch.setattr(application,'_replace_at',concurrent)
    with pytest.raises(RuntimeError,match='rollback'):
        commit_apply(db,preview,True)
    assert (root/'AGENTS.md').read_text() == 'concurrent owner'
    assert load_lesson_state(db,lesson.id).effective_status == 'replay_confirmed'


def test_rollback_failure_explicit(eligible,monkeypatch):
    db,root,lesson=eligible
    (root/'AGENTS.md').write_text('original')
    preview=preview_apply(db,root,lesson.id)
    db.execute("CREATE TRIGGER deny_apply BEFORE INSERT ON applications BEGIN SELECT RAISE(ABORT,'injected'); END");db.commit()
    import retryloom.application as application
    original=application._replace_at
    calls=[]
    def fail_rollback(*args):
        calls.append(1)
        if len(calls)>1: raise OSError('rollback failure')
        original(*args)
    monkeypatch.setattr(application,'_replace_at',fail_rollback)
    with pytest.raises(RuntimeError,match='rollback failed'): commit_apply(db,preview,True)
    assert load_lesson_state(db,lesson.id).effective_status == 'replay_confirmed'
    assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 0


def test_unsafe_applied_without_audit(eligible):
    db,root,lesson=eligible
    db.execute("UPDATE lessons SET status='applied' WHERE id=?",(lesson.id,));db.commit()
    with pytest.raises(ValueError,match='audit'): load_lesson_state(db,lesson.id)


def test_schema4_preview_no_mutation(eligible):
    db,root,lesson=eligible
    db.execute('DROP TABLE applications');db.commit()
    preview=preview_apply(db,root,lesson.id)
    assert preview.diff
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='applications'").fetchone() is None


def test_permissions_preserved(eligible):
    db,root,lesson=eligible
    target=root/'AGENTS.md';target.write_text('existing');target.chmod(0o600)
    commit_apply(db,preview_apply(db,root,lesson.id),True)
    assert target.stat().st_mode & 0o777 == 0o600


def test_different_rule_not_superficial_duplicate(eligible):
    db,root,lesson=eligible
    target=root/'AGENTS.md';target.write_text(lesson.rule.lower()+'\n')
    preview=preview_apply(db,root,lesson.id)
    assert preview.after.startswith(target.read_bytes()) and preview.after != target.read_bytes()


def test_applied_noop_after_other_guidance_appended(eligible):
    db,root,lesson=eligible
    commit_apply(db,preview_apply(db,root,lesson.id),True)
    target=root/'AGENTS.md'
    with target.open('a') as stream: stream.write('\nUnrelated later guidance.\n')
    before=target.read_bytes()
    preview=preview_apply(db,root,lesson.id)
    assert preview.already_applied
    assert commit_apply(db,preview,True).status == 'already_applied'
    assert target.read_bytes() == before


def test_open_parent_never_recreates_repository(eligible):
    db,root,lesson=eligible
    import retryloom.application as application
    preview=preview_apply(db,root,lesson.id)
    root.rmdir()
    with pytest.raises(ValueError,match='disappeared|identity changed'):
        application._open_parent(preview,[])
    assert not root.exists()


@pytest.mark.parametrize('field,value',[('after_hash','z'*64),('before_hash','invalid'),('created_at','not-a-date')])
def test_application_audit_fields_validated(eligible,field,value):
    db,root,lesson=eligible
    commit_apply(db,preview_apply(db,root,lesson.id),True)
    db.execute(f'UPDATE applications SET {field}=? WHERE lesson_id=?',(value,lesson.id));db.commit()
    with pytest.raises(ValueError,match='audit'): load_lesson_state(db,lesson.id)


def test_detached_parent_rollback_does_not_restore_outside(eligible,monkeypatch,tmp_path):
    db,root,lesson=eligible
    import retryloom.application as application
    preview=preview_apply(db,root,lesson.id,'.retryloom/skills/')
    original=application._replace_at
    moved=tmp_path/'detached-skills'
    def detach(fd,name,content,mode,*guard):
        original(fd,name,content,mode,*guard)
        parent=(root/preview.target).parent
        parent.rename(moved)
        parent.mkdir()
        (parent/name).write_bytes(content)
    monkeypatch.setattr(application,'_replace_at',detach)
    with pytest.raises(RuntimeError,match='rollback failed'):
        commit_apply(db,preview,True)
    # Crucially, rollback did not unlink/restore the written file outside root.
    assert (moved/Path(preview.target).name).read_bytes() == preview.after
    assert load_lesson_state(db,lesson.id).effective_status == 'replay_confirmed'
    assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 0
