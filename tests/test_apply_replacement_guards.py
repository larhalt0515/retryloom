"""Concurrent edits at the final replacement and restoration boundaries."""
import os
import sqlite3
import stat

import pytest

import retryloom.application as application
from retryloom.application import commit_apply, preview_apply
from test_apply import eligible
from test_apply_review import assert_unapplied


def after_temp_fsync(monkeypatch, operation, occurrence=1):
    original = os.fsync
    prepared = 0

    def fsync(fd):
        nonlocal prepared
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        original(fd)
        if regular:
            prepared += 1
            if prepared == occurrence:
                operation()

    monkeypatch.setattr(application.os, 'fsync', fsync)


def change_target(target, kind, outside):
    if kind == 'modified':
        target.write_bytes(b'External instructions')
    elif kind in ('replaced', 'same_bytes_replaced'):
        previous = target.read_bytes()
        target.rename(target.with_suffix('.saved'))
        target.write_bytes(previous if kind == 'same_bytes_replaced' else b'Replacement instructions')
    elif kind == 'deleted':
        target.unlink()
    elif kind == 'created':
        target.write_bytes(b'External new file')
    elif kind == 'symlink':
        target.unlink()
        target.symlink_to(outside)
    elif kind == 'directory':
        target.unlink()
        target.mkdir()
    elif kind == 'hardlink':
        os.link(target, target.with_suffix('.linked'))
    elif kind == 'mode':
        target.chmod(0o600)


def target_state(target):
    try:
        info = target.lstat()
    except FileNotFoundError:
        return None
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)
    if stat.S_ISLNK(info.st_mode):
        return identity, os.readlink(target)
    if stat.S_ISREG(info.st_mode):
        return identity, target.read_bytes()
    return identity, None


@pytest.mark.parametrize('kind', [
    'modified', 'replaced', 'same_bytes_replaced', 'deleted', 'created',
    'symlink', 'directory', 'hardlink', 'mode',
])
def test_stale_target_during_apply_preparation(eligible, monkeypatch, tmp_path, kind):
    db, root, lesson = eligible
    target = root / 'AGENTS.md'
    outside = tmp_path / 'outside'
    outside.write_bytes(b'Outside content')
    if kind != 'created':
        target.write_bytes(b'Original instructions')
        target.chmod(0o644)
    proposal = preview_apply(db, root, lesson.id)
    observed = []

    def edit():
        change_target(target, kind, outside)
        observed.append(target_state(target))

    after_temp_fsync(monkeypatch, edit)
    with pytest.raises(ValueError, match='preview'):
        commit_apply(db, proposal, True)
    assert len(observed) == 1
    assert target_state(target) == observed[0]
    assert outside.read_bytes() == b'Outside content'
    assert_unapplied(db, lesson)
    assert not list(root.glob('.retryloom-apply-*'))


@pytest.mark.parametrize('kind', ['modified', 'replaced', 'same_bytes_replaced', 'deleted', 'symlink', 'directory'])
def test_stale_target_during_rollback_preparation(eligible, monkeypatch, tmp_path, kind):
    db, root, lesson = eligible
    target = root / 'AGENTS.md'
    target.write_bytes(b'Original instructions')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'Outside content')
    proposal = preview_apply(db, root, lesson.id)
    db.execute("CREATE TRIGGER deny_apply BEFORE INSERT ON applications BEGIN SELECT RAISE(ABORT,'injected'); END")
    db.commit()
    observed = []

    def edit():
        change_target(target, kind, outside)
        observed.append(target_state(target))

    after_temp_fsync(monkeypatch, edit, occurrence=2)
    with pytest.raises(RuntimeError, match='rollback failed') as error:
        commit_apply(db, proposal, True)
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    assert len(observed) == 1
    assert target_state(target) == observed[0]
    assert outside.read_bytes() == b'Outside content'
    assert_unapplied(db, lesson)
    assert not list(root.glob('.retryloom-apply-*'))


def test_same_bytes_replacement_before_rollback_is_not_apply_owned(eligible, monkeypatch):
    db, root, lesson = eligible
    target = root / 'AGENTS.md'
    target.write_bytes(b'Original instructions')
    proposal = preview_apply(db, root, lesson.id)
    original = application._replace_at
    observed = []

    def replace_then_external_copy(*args):
        result = original(*args)
        change_target(target, 'same_bytes_replaced', None)
        observed.append(target_state(target))
        raise OSError('injected post-write failure')

    monkeypatch.setattr(application, '_replace_at', replace_then_external_copy)
    with pytest.raises(RuntimeError, match='rollback failed'):
        commit_apply(db, proposal, True)
    assert target_state(target) == observed[0]
    assert target.read_bytes() == proposal.after
    assert_unapplied(db, lesson)


def test_ordinary_persistence_failure_restores_original(eligible):
    db, root, lesson = eligible
    target = root / 'AGENTS.md'
    target.write_bytes(b'Original instructions')
    proposal = preview_apply(db, root, lesson.id)
    db.execute("CREATE TRIGGER deny_apply BEFORE INSERT ON applications BEGIN SELECT RAISE(ABORT,'injected'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        commit_apply(db, proposal, True)
    assert target.read_bytes() == proposal.before
    assert_unapplied(db, lesson)


def test_normal_apply_and_duplicate_remain_safe(eligible):
    db, root, lesson = eligible
    proposal = preview_apply(db, root, lesson.id)
    assert commit_apply(db, proposal, True).status == 'applied'
    assert (root / proposal.target).read_bytes() == proposal.after
    assert db.execute('SELECT COUNT(*) FROM applications').fetchone()[0] == 1
    assert commit_apply(db, preview_apply(db, root, lesson.id), True).status == 'already_applied'
    assert db.execute('SELECT COUNT(*) FROM applications').fetchone()[0] == 1
