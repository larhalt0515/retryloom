"""Managed lesson persistence and migration safety boundaries."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest
from pydantic import ValidationError

from reflex.analysis.corrections import extract_corrections
from reflex.analysis.failures import detect_failure
from reflex.analysis.lessons import generate_candidate
from reflex.models import Session, SessionEvent
from reflex.storage.database import connect, initialize_schema
from reflex.storage.repository import (load_lesson, prepare_lesson_for_replay, resolve_lesson_audit,
                                       save_failure, save_lesson, save_session, verify_managed_lesson)
from reflex.storage.schema import SCHEMA_SQL


def setup(db, session_id="storage-session"):
    session = Session(id=session_id, agent="fixture", repository="/fixture-repository",
                      started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), events=[
        SessionEvent(type="tool_result", command="pytest", exit_code=1),
        SessionEvent(type="file_edit", content="+return corrected_record(row)",
                     metadata={"source_before": "return previous_record(row)"}),
        SessionEvent(type="tool_result", command="pytest", exit_code=0),
    ])
    failure = detect_failure(session)
    save_session(db, session)
    save_failure(db, failure)
    evidence = extract_corrections(session, failure)
    return generate_candidate(evidence), evidence


def contents(db):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}


def test_roundtrip_preserves_sources_and_persists_hashes_only(tmp_path):
    db = connect(tmp_path / "lesson.db")
    candidate, evidence = setup(db)
    before = contents(db)
    saved = save_lesson(db, candidate, evidence)
    assert load_lesson(db, saved.id) == saved
    assert saved.status == "candidate" and saved.audit.status == "ACCEPT"
    payload = prepare_lesson_for_replay(db, saved.id, evidence)
    assert payload.text == saved.rule
    assert payload.lesson_hash == hashlib.sha256(saved.rule.encode("utf-8")).hexdigest()
    after = contents(db)
    for table in ("sessions", "session_events", "failures"):
        assert after[table] == before[table]
    audit = db.execute("SELECT audit_json,evidence_json FROM lesson_audits").fetchone()
    assert "corrected_record" not in " ".join(audit)
    assert "previous_record" not in " ".join(audit)
    assert evidence[0].success_proof not in audit[1]
    snapshot = json.loads(audit[1])[0]
    assert snapshot["success_event_ref"] == 2
    assert snapshot["correction_diff"]["sha256"] == hashlib.sha256(evidence[0].correction_diff.encode()).hexdigest()
    original = contents(db)
    assert save_lesson(db, saved, evidence) == saved
    assert contents(db) == original


def test_frozen_context_is_deeply_immutable_and_save_cannot_promote(tmp_path):
    db = connect(tmp_path / "lesson.db")
    lesson, evidence = setup(db)
    saved = save_lesson(db, lesson, evidence)
    with pytest.raises(ValidationError):
        saved.trigger.repository = "another"
    with pytest.raises(ValidationError):
        saved.evidence_refs[0].session_id = "another"
    original = contents(db)
    for status in ("replay_confirmed", "applied"):
        with pytest.raises(ValueError):
            save_lesson(db, saved.model_copy(update={"status": status}), evidence)
    altered = lesson.model_copy(update={"rule": lesson.rule + " Inspect the check output."})
    with pytest.raises(ValueError, match="rewrite"):
        save_lesson(db, altered, evidence)
    assert contents(db) == original


@pytest.mark.parametrize("column,value", [
    ("scope", "generic"), ("trigger_json", '{"repository":"another","failure_type":"test_failure"}'),
    ("evidence_sessions_json", '[]'), ("status", "replay_confirmed"),
    ("lesson_hash", "forged"), ("canonical_fingerprint", "forged"),
    ("historical_evidence_fingerprint", "forged"), ("rule", "replacement"),
])
def test_db_context_tampering_is_detected(tmp_path, column, value):
    db = connect(tmp_path / "lesson.db")
    lesson, evidence = setup(db)
    saved = save_lesson(db, lesson, evidence)
    table = "lesson_freezes" if column in {"canonical_fingerprint", "historical_evidence_fingerprint"} else "lessons"
    key = "lesson_id" if table == "lesson_freezes" else "id"
    db.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (value, saved.id))
    db.commit()
    with pytest.raises(ValueError):
        load_lesson(db, saved.id)
    with pytest.raises(ValueError):
        prepare_lesson_for_replay(db, saved.id, evidence)
    with pytest.raises(ValueError):
        verify_managed_lesson(db, saved.id)


@pytest.mark.parametrize("table,column,value", [
    ("lesson_audits", "audit_json", '{"status":"ACCEPT"}'),
    ("lesson_audits", "evidence_json", '[]'),
    ("lesson_audits", "audit_fingerprint", "forged"),
    ("lesson_freezes", "lesson_json", ""),
])
def test_missing_or_tampered_snapshots_fail_closed(tmp_path, table, column, value):
    db = connect(tmp_path / "lesson.db")
    lesson, evidence = setup(db)
    save_lesson(db, lesson, evidence)
    key = "lesson_id" if table in {"lesson_audits", "lesson_freezes"} else "id"
    db.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (value, lesson.id))
    db.commit()
    with pytest.raises(ValueError):
        verify_managed_lesson(db, lesson.id)


def test_suspect_review_preserves_findings_and_is_bound_to_current_evidence(tmp_path):
    db = connect(tmp_path / "lesson.db")
    lesson, evidence = setup(db)
    suspect = lesson.model_copy(update={"rule": "When a test fails, inspect corrected_record before repeating the check."})
    saved = save_lesson(db, suspect, evidence)
    assert saved.audit.status == "SUSPECT" and saved.leak_suspect
    with pytest.raises(ValueError, match="resolution"):
        prepare_lesson_for_replay(db, saved.id, evidence)
    changed = [evidence[0].model_copy(update={"correction_diff": "+return other_record(row)"})]
    with pytest.raises(ValueError, match="evidence"):
        resolve_lesson_audit(db, saved.id, changed, reviewer="human", reason="reviewed", approved=True)
    with pytest.raises(ValueError, match="reviewer"):
        resolve_lesson_audit(db, saved.id, evidence, reviewer="", reason="reviewed", approved=True)
    denied = resolve_lesson_audit(db, saved.id, evidence, reviewer="human", reason="needs review", approved=False)
    assert denied == saved
    with pytest.raises(ValueError):
        prepare_lesson_for_replay(db, saved.id, evidence)
    resolved = resolve_lesson_audit(db, saved.id, evidence, reviewer="human", reason="generic fixture identifier verified", approved=True)
    assert resolved == saved and resolved.audit.reasons
    assert db.execute("SELECT leak_suspect FROM lessons").fetchone()[0] == 0
    assert prepare_lesson_for_replay(db, saved.id, evidence).text == saved.rule
    assert db.execute("SELECT COUNT(*) FROM lesson_audit_resolutions").fetchone()[0] == 2
    db.execute("UPDATE lesson_audit_resolutions SET evidence_fingerprint='forged' WHERE approved=1")
    db.commit()
    with pytest.raises(ValueError):
        load_lesson(db, saved.id)


def test_reject_is_persisted_but_cannot_be_overridden(tmp_path):
    db = connect(tmp_path / "lesson.db")
    lesson, evidence = setup(db)
    rejected = lesson.model_copy(update={"rule": "Always use C."})
    saved = save_lesson(db, rejected, evidence)
    assert saved.status == "rejected" and saved.audit.status == "REJECT"
    assert load_lesson(db, saved.id) == saved
    with pytest.raises(ValueError, match="REJECT"):
        resolve_lesson_audit(db, saved.id, evidence, reviewer="human", reason="override", approved=True)
    with pytest.raises(ValueError, match="rejected"):
        prepare_lesson_for_replay(db, saved.id, evidence)
    db.execute("UPDATE lessons SET leak_suspect=0")
    db.commit()
    with pytest.raises(ValueError):
        load_lesson(db, saved.id)


@pytest.mark.parametrize("version", ["1", "2", "3", "4"])
def test_v4_migration_preserves_all_existing_rows_and_is_idempotent(tmp_path, version):
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    # The old base schema retains its lessons definition; remove only new tables.
    schema = "\n".join(line for line in SCHEMA_SQL.splitlines()
                       if not line.startswith(("CREATE TABLE IF NOT EXISTS lesson_audit", "CREATE TABLE IF NOT EXISTS lesson_freezes")))
    old.executescript(schema)
    old.execute("INSERT INTO metadata VALUES('schema_version',?)", (version,))
    old.execute("INSERT INTO sessions(id,agent,repository,started_at) VALUES('session','fixture','repo','2026-01-01')")
    old.execute("INSERT INTO failures VALUES('failure','session',5,'MEDIUM','[\"test_failure\"]','legacy','[]')")
    old.execute("INSERT INTO lessons VALUES('lesson','repository','{}','legacy rule','[\"session\"]',0.5,'candidate','old-hash',0)")
    old.execute("INSERT INTO lesson_evidence VALUES('lesson','session')")
    old.execute("INSERT INTO replay_cases VALUES('case','repo','commit','task',3,'lesson','old-hash')")
    old.execute("INSERT INTO evaluations(replay_case_id,status,control_success,reflex_success,reason) VALUES('case','rejected',1,1,'legacy reason')")
    old.commit()
    before = contents(old)
    old.close()
    migrated = connect(path)
    for table, rows in before.items():
        if table == "metadata":
            continue
        got = [tuple(row)[:len(rows[0])] for row in migrated.execute(f"SELECT * FROM {table}")] if rows else []
        assert got == rows
    assert migrated.execute("SELECT value FROM metadata").fetchone()[0] == "5"
    assert verify_managed_lesson(migrated, "lesson") is None
    with pytest.raises(ValueError, match="legacy"):
        load_lesson(migrated, "lesson")
    after = contents(migrated)
    initialize_schema(migrated)
    assert contents(migrated) == after


def test_v4_migration_failure_is_atomic(tmp_path, monkeypatch):
    import reflex.storage.database as module
    db = sqlite3.connect(tmp_path / "broken.db")
    db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
    db.execute("INSERT INTO metadata VALUES('schema_version','3')")
    db.commit()
    monkeypatch.setattr(module, "COLUMN_MIGRATIONS", {"lessons": {"added": "TEXT", "broken": "INVALID SYNTAX ("}})
    with pytest.raises(sqlite3.OperationalError):
        initialize_schema(db)
    assert [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["metadata"]
    assert db.execute("SELECT value FROM metadata").fetchone()[0] == "3"


@pytest.mark.parametrize("changes", [
    {"failure_id": "nonexistent-failure"},
    {"failure_event_ref": 5, "correction_event_ref": 6, "success_event_ref": 7},
    {"failure_event_ref": 1, "correction_event_ref": 1},
    {"correction_diff": "+return invented_answer(row)"},
    {"source_before": "invented source"},
    {"correction_diff": "", "source_before": ""},
    {"success_proof": "invented successful proof"},
    {"final_patch": "unrelated final answer"},
])
def test_save_requires_selected_stored_failure_and_canonical_events(tmp_path, changes):
    db = connect(tmp_path / "lesson.db")
    lesson, evidence = setup(db)
    changed = [evidence[0].model_copy(update=changes)]
    candidate = generate_candidate(changed)
    before = contents(db)
    with pytest.raises(ValueError):
        save_lesson(db, candidate, changed)
    assert contents(db) == before


def test_save_rejects_failure_from_another_session_and_unrelated_success(tmp_path):
    db = connect(tmp_path / "lesson.db")
    lesson, evidence = setup(db)
    _, other = setup(db, "another-session")
    forged = [evidence[0].model_copy(update={"failure_id": other[0].failure_id})]
    before = contents(db)
    with pytest.raises(ValueError, match="belong"):
        save_lesson(db, generate_candidate(forged), forged)
    assert contents(db) == before
    db.execute("UPDATE session_events SET command='pytest unrelated.py' WHERE session_id=? AND exit_code=0",
               (evidence[0].session_id,))
    db.commit()
    before = contents(db)
    with pytest.raises(ValueError, match="history"):
        save_lesson(db, lesson, evidence)
    assert contents(db) == before
