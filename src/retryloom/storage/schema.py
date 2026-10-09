"""SQLite storage, including local replay records and the v1 migration."""
SCHEMA_VERSION = 5
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, agent TEXT NOT NULL, agent_version TEXT, model TEXT, repository TEXT NOT NULL, base_commit TEXT, started_at TEXT NOT NULL, ended_at TEXT, task TEXT, result TEXT, token_input INTEGER, token_output INTEGER, tool_call_count INTEGER);
CREATE TABLE IF NOT EXISTS session_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id), type TEXT NOT NULL, timestamp TEXT, command TEXT, exit_code INTEGER, file_path TEXT, content TEXT, metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failures (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), score INTEGER NOT NULL, confidence TEXT NOT NULL, signals_json TEXT NOT NULL, summary TEXT NOT NULL, evidence_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lessons (id TEXT PRIMARY KEY, scope TEXT NOT NULL, trigger_json TEXT NOT NULL, rule TEXT NOT NULL, evidence_sessions_json TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL, lesson_hash TEXT, leak_suspect INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS lesson_evidence (lesson_id TEXT NOT NULL REFERENCES lessons(id), session_id TEXT NOT NULL REFERENCES sessions(id), PRIMARY KEY (lesson_id, session_id));
CREATE TABLE IF NOT EXISTS lesson_freezes (lesson_id TEXT PRIMARY KEY REFERENCES lessons(id), lesson_json TEXT NOT NULL, canonical_fingerprint TEXT NOT NULL, historical_evidence_fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lesson_audits (lesson_id TEXT PRIMARY KEY REFERENCES lessons(id), audit_json TEXT NOT NULL, evidence_json TEXT NOT NULL, audit_fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lesson_audit_resolutions (id INTEGER PRIMARY KEY AUTOINCREMENT, lesson_id TEXT NOT NULL REFERENCES lessons(id), reviewer TEXT NOT NULL, reason TEXT NOT NULL, approved INTEGER NOT NULL, canonical_fingerprint TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL, audit_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, lesson_id TEXT NOT NULL UNIQUE REFERENCES lessons(id), lesson_hash TEXT NOT NULL, frozen_fingerprint TEXT NOT NULL, repository TEXT NOT NULL, target TEXT NOT NULL, evaluation_id INTEGER NOT NULL REFERENCES evaluations(id), report_hash TEXT NOT NULL, before_hash TEXT, after_hash TEXT NOT NULL, from_status TEXT NOT NULL CHECK(from_status='replay_confirmed'), to_status TEXT NOT NULL CHECK(to_status='applied'), outcome TEXT NOT NULL CHECK(outcome IN ('applied','already_present')), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_cases (id TEXT PRIMARY KEY, repository TEXT NOT NULL, base_commit TEXT NOT NULL, task TEXT NOT NULL, runs INTEGER NOT NULL, lesson_id TEXT NOT NULL REFERENCES lessons(id), lesson_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, replay_case_id TEXT NOT NULL REFERENCES replay_cases(id), name TEXT NOT NULL, command TEXT NOT NULL, required INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS replay_runs (id TEXT PRIMARY KEY, replay_case_id TEXT NOT NULL REFERENCES replay_cases(id), condition TEXT NOT NULL, run_number INTEGER NOT NULL, agent_version TEXT, model TEXT, lesson_hash TEXT, success INTEGER, exit_code INTEGER, token_total INTEGER, tool_calls INTEGER, elapsed_seconds REAL);
CREATE TABLE IF NOT EXISTS evaluations (id INTEGER PRIMARY KEY AUTOINCREMENT, replay_case_id TEXT NOT NULL REFERENCES replay_cases(id), status TEXT NOT NULL, control_success INTEGER NOT NULL, reflex_success INTEGER NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_reports (id TEXT PRIMARY KEY, replay_case_id TEXT NOT NULL REFERENCES replay_cases(id), status TEXT NOT NULL, reason TEXT NOT NULL, warnings_json TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_check_results (id INTEGER PRIMARY KEY AUTOINCREMENT, replay_run_id TEXT NOT NULL REFERENCES replay_runs(id), check_number INTEGER NOT NULL, name TEXT NOT NULL, command_json TEXT NOT NULL, required INTEGER NOT NULL, success INTEGER NOT NULL, stdout TEXT NOT NULL, stderr TEXT NOT NULL, exit_code INTEGER, timed_out INTEGER NOT NULL, elapsed_seconds REAL NOT NULL, error TEXT, cleanup_error TEXT, output_truncated INTEGER NOT NULL);
"""

COLUMN_MIGRATIONS = {
    "lessons": {
        "lesson_hash": "TEXT", "leak_suspect": "INTEGER NOT NULL DEFAULT 0",
    },
    "evaluations": {
        "report_id": "TEXT REFERENCES replay_reports(id)",
        "condition": "TEXT", "control_median_tokens": "REAL", "reflex_median_tokens": "REAL",
        "token_reduction_ratio": "REAL", "required_check_regression": "INTEGER",
        "evidence_json": "TEXT", "manifest_hash": "TEXT", "report_hash": "TEXT",
        "evaluation_json": "TEXT", "created_at": "TEXT",
    },
    "replay_cases": {
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 1800",
        "agent": "TEXT NOT NULL DEFAULT 'codex'", "agent_version": "TEXT", "model": "TEXT",
        "model_settings_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "replay_checks": {"timeout_seconds": "INTEGER NOT NULL DEFAULT 60"},
    "replay_runs": {
        "report_id": "TEXT REFERENCES replay_reports(id)",
        "token_input": "INTEGER", "token_output": "INTEGER", "files_changed": "INTEGER",
        "lines_added": "INTEGER", "lines_removed": "INTEGER", "stdout": "TEXT", "stderr": "TEXT",
        "timed_out": "INTEGER NOT NULL DEFAULT 0", "cleanup_error": "TEXT", "agent": "TEXT",
        "agent_error": "TEXT", "output_truncated": "INTEGER NOT NULL DEFAULT 0",
        "efficiency_usable": "INTEGER NOT NULL DEFAULT 0", "status": "TEXT",
        "statuses_json": "TEXT NOT NULL DEFAULT '[]'", "seed": "INTEGER",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    },
}
