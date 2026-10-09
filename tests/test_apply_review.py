"""Independent lifecycle, failure and race checks for Apply."""
from dataclasses import replace
import sqlite3

import pytest

from retryloom.application import preview_apply, commit_apply
from retryloom.storage.lifecycle import load_lesson_state
from test_apply import eligible


def assert_unapplied(db, lesson):
    assert db.execute("SELECT status FROM lessons WHERE id=?", (lesson.id,)).fetchone()[0] == "replay_confirmed"
    assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_candidate_with_successful_evaluation_is_not_promoted(eligible):
    db, root, lesson = eligible
    db.execute("UPDATE lessons SET status='candidate' WHERE id=?", (lesson.id,))
    db.commit()
    with pytest.raises(ValueError):
        preview_apply(db, root, lesson.id)
    assert db.execute("SELECT status FROM lessons").fetchone()[0] == "candidate"
    assert not (root / "AGENTS.md").exists()


def test_forged_preview_cannot_inject_another_rule(eligible):
    db, root, lesson = eligible
    proposal = preview_apply(db, root, lesson.id)
    forged = replace(proposal, after=b"Unconfirmed unrelated instruction")
    with pytest.raises(ValueError, match="preview"):
        commit_apply(db, forged, True)
    assert not (root / "AGENTS.md").exists()
    assert_unapplied(db, lesson)


def test_same_bytes_replacement_is_detected(eligible):
    db, root, lesson = eligible
    target = root / "AGENTS.md"
    target.write_text("Existing instructions")
    proposal = preview_apply(db, root, lesson.id)
    saved = root / "old-instructions"
    target.rename(saved)
    target.write_bytes(saved.read_bytes())
    with pytest.raises(ValueError, match="preview"):
        commit_apply(db, proposal, True)
    assert target.read_bytes() == saved.read_bytes()
    assert_unapplied(db, lesson)


def test_sqlite_commit_failure_restores_file_and_status(eligible):
    db, root, lesson = eligible
    target = root / "AGENTS.md"
    target.write_text("Original instructions")
    proposal = preview_apply(db, root, lesson.id)

    class FailedCommit:
        def __getattr__(self, name):
            return getattr(db, name)

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    with pytest.raises(sqlite3.OperationalError):
        commit_apply(FailedCommit(), proposal, True)
    assert target.read_text() == "Original instructions"
    assert_unapplied(db, lesson)


def test_failed_new_skills_apply_removes_created_empty_directories(eligible, monkeypatch):
    db, root, lesson = eligible
    proposal = preview_apply(db, root, lesson.id, ".retryloom/skills/")

    def fail(*args):
        raise OSError("injected write failure")

    monkeypatch.setattr("retryloom.application._replace_at", fail)
    with pytest.raises(OSError):
        commit_apply(db, proposal, True)
    assert not (root / ".retryloom").exists()
    assert_unapplied(db, lesson)


def test_detached_parent_with_copied_bytes_is_not_success(eligible, monkeypatch):
    import retryloom.application as module

    db, root, lesson = eligible
    (root / ".retryloom/skills").mkdir(parents=True)
    proposal = preview_apply(db, root, lesson.id, ".retryloom/skills/")
    original = module._replace_at

    def replace_then_detach(fd, name, content, mode, *guard):
        original(fd, name, content, mode, *guard)
        (root / ".retryloom/skills").rename(root / ".retryloom/detached")
        (root / ".retryloom/skills").mkdir()
        (root / ".retryloom/skills" / name).write_bytes(content)

    monkeypatch.setattr(module, "_replace_at", replace_then_detach)
    with pytest.raises((ValueError, RuntimeError)):
        commit_apply(db, proposal, True)
    assert_unapplied(db, lesson)


def test_applied_rule_is_not_duplicated_after_unrelated_append(eligible):
    db, root, lesson = eligible
    commit_apply(db, preview_apply(db, root, lesson.id), True)
    target = root / "AGENTS.md"
    with target.open("ab") as stream:
        stream.write(b"\nAdditional unrelated instructions\n")
    before = target.read_bytes()
    proposal = preview_apply(db, root, lesson.id)
    assert proposal.already_applied
    assert commit_apply(db, proposal, True).status == "already_applied"
    assert target.read_bytes() == before
    assert target.read_text().count(lesson.rule) == 1


@pytest.mark.parametrize("field,value", [
    ("lesson_hash", "a" * 64), ("frozen_fingerprint", "a" * 64),
    ("report_hash", "a" * 64), ("after_hash", "z" * 64),
    ("repository", "/workspace/unrelated"), ("target", "../AGENTS.md"),
])
def test_applied_audit_tampering_is_rejected(eligible, field, value):
    db, root, lesson = eligible
    commit_apply(db, preview_apply(db, root, lesson.id), True)
    db.execute(f"UPDATE applications SET {field}=?", (value,))
    db.commit()
    with pytest.raises(ValueError):
        load_lesson_state(db, lesson.id)
