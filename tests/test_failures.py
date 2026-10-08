from datetime import datetime, timezone, timedelta
import json
import pytest

from reflex.models import Session, SessionEvent
from reflex.analysis.failures import detect_failure
from reflex.analysis.signals import confidence, score_signals
from reflex.storage.database import connect
from reflex.storage.repository import save_session, save_failure


def session(events):
    return Session(id="fixture", agent="codex", repository="/repo", started_at=datetime.now(timezone.utc), events=events)


@pytest.mark.parametrize("score,expected", [(0,"LOW"),(3,"LOW"),(4,"MEDIUM"),(5,"MEDIUM"),(7,"MEDIUM"),(8,"HIGH"),(10,"HIGH")])
def test_score_boundaries(score, expected):
    assert confidence(score) == expected


def test_test_failure_alone():
    failure = detect_failure(session([SessionEvent(type="tool_result", command="pytest", exit_code=1)]))
    assert failure.score == 5 and failure.confidence == "MEDIUM"


def test_correction_and_aggregation_and_evidence(tmp_path):
    source = session([SessionEvent(type="tool_result", command="pytest", exit_code=1), SessionEvent(type="user_message", content="Please fix the parser")])
    failure = detect_failure(source)
    assert failure.score == 9 and failure.confidence == "HIGH"
    assert len(failure.evidence) == 2
    database = connect(tmp_path / "db")
    save_session(database, source)
    save_failure(database, failure)
    save_failure(database, failure)
    row = database.execute("SELECT * FROM failures").fetchone()
    assert json.loads(row["evidence_json"]) == failure.evidence
    assert database.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 1


def test_negative_language_repetition_is_not_high():
    failure = detect_failure(session([SessionEvent(type="user_message", content=f"wrong {i}") for i in range(20)]))
    assert failure.score == 1 and failure.confidence == "LOW"
    assert len(failure.evidence) == 20


def test_ordinary_iteration_is_low():
    now = datetime.now(timezone.utc)
    failure = detect_failure(session([SessionEvent(type="file_edit", file_path="a.py", timestamp=now), SessionEvent(type="file_edit", file_path="a.py", timestamp=now+timedelta(seconds=10))]))
    assert failure.score == 2 and failure.confidence == "LOW"
    assert detect_failure(session([SessionEvent(type="tool_result", command="pytest", exit_code=0)])) is None


def test_incomplete_events_and_codex_call_correlation():
    events = [SessionEvent(type="unknown", metadata={"source_record":None}), SessionEvent(type="function_call", command="pytest", metadata={"source_record":{"payload":{"call_id":"c"}}}), SessionEvent(type="function_call_output", exit_code=1, metadata={"source_record":{"payload":{"call_id":"c"}}})]
    assert detect_failure(session(events)).signals == ["test_failure"]
    assert score_signals(["negative_language"] * 20) == 1
