"""Migration evidence protects existing Phase 1 data."""
import sqlite3

import pytest

from reflex.storage.database import connect, initialize_schema


def test_v1_database_migrates_preserving_existing_data(tmp_path):
    path = tmp_path / "v1.db"
    database = sqlite3.connect(path)
    database.executescript("""
      CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      INSERT INTO metadata VALUES('schema_version','1');
      CREATE TABLE sessions(id TEXT PRIMARY KEY,agent TEXT NOT NULL,agent_version TEXT,model TEXT,repository TEXT NOT NULL,base_commit TEXT,started_at TEXT NOT NULL,ended_at TEXT,task TEXT,result TEXT,token_input INTEGER,token_output INTEGER,tool_call_count INTEGER);
      INSERT INTO sessions(id,agent,repository,started_at) VALUES('preserved','codex','/repo','2026-01-01');
    """)
    database.close()
    upgraded = connect(path)
    assert upgraded.execute("SELECT id FROM sessions").fetchone()[0] == "preserved"
    assert upgraded.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "5"
    assert "stdout" in {r[1] for r in upgraded.execute("PRAGMA table_info(replay_runs)")}
    upgraded.close()


def test_unknown_database_version_rejected_without_writes(tmp_path):
    path = tmp_path / "future.db"
    database = sqlite3.connect(path)
    database.executescript("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); INSERT INTO metadata VALUES('schema_version','999');")
    database.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unsupported schema version"):
        connect(path)
    assert path.read_bytes() == before


def test_failed_migration_rolls_back_schema(tmp_path, monkeypatch):
    import reflex.storage.database as module
    database = sqlite3.connect(tmp_path / "rollback.db")
    database.executescript("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); INSERT INTO metadata VALUES('schema_version','1');")
    monkeypatch.setattr(module, "SCHEMA_SQL", "CREATE TABLE should_rollback(id INTEGER); INVALID SQL;")
    with pytest.raises(sqlite3.OperationalError):
        initialize_schema(database)
    assert database.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
    assert database.execute("SELECT value FROM metadata").fetchone()[0] == "1"
    database.close()
