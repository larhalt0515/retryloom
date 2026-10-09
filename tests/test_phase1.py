from datetime import datetime, timezone
from typing import get_type_hints

from retryloom.config.loader import load_config, write_default_config
from retryloom.models import Session, SessionEvent
from retryloom.storage.database import connect
from retryloom.storage.repository import save_session
from retryloom.adapters.base import SessionAdapter


def test_default_config_round_trips(tmp_path):
    path = tmp_path / ".retryloom" / "config.toml"
    config = write_default_config(path)

    assert load_config(path) == config
    assert config.replay.runs == 3
    assert config.evaluation.token_reduction_threshold == 0.15


def test_session_and_events_are_persisted(tmp_path):
    database = connect(tmp_path / "retryloom.db")
    session = Session(
        id="session-1",
        agent="codex",
        repository="/repo",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        events=[SessionEvent(type="tool_call", command="pytest", metadata={"ok": True})],
    )

    save_session(database, session)
    row = database.execute("SELECT agent, started_at FROM sessions WHERE id = ?", (session.id,)).fetchone()
    event = database.execute("SELECT type, metadata_json FROM session_events WHERE session_id = ?", (session.id,)).fetchone()

    assert dict(row) == {"agent": "codex", "started_at": "2026-01-01T00:00:00+00:00"}
    assert event["type"] == "tool_call"
    assert '"ok": true' in event["metadata_json"]


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "retryloom.db"
    first = connect(path)
    second = connect(path)

    assert second.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()[0] == "5"
    assert first.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluations'").fetchone()


def test_session_adapter_protocol_and_canonical_event_fields():
    hints = get_type_hints(SessionAdapter.parse)

    assert hints["path"].__name__ == "Path"
    assert hints["return"] is Session
    event = SessionEvent(
        type="tool_result",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        command="pytest",
        exit_code=1,
        file_path="tests/test_example.py",
        content="failed",
        metadata={"tool": "shell", "call_id": "abc"},
    )
    assert event.metadata["call_id"] == "abc"
    assert event.exit_code == 1
