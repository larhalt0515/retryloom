"""SQLite connection and schema initialization."""
import sqlite3
from pathlib import Path
from .schema import COLUMN_MIGRATIONS, SCHEMA_SQL, SCHEMA_VERSION


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Reject unknown versions before changing anything; migrate v1/v2/v3 atomically."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    row = (connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
           if "metadata" in tables else None)
    if row is None and tables - {"metadata", "sqlite_sequence"}:
        raise ValueError("database has no recognized schema version")
    version = row[0] if row else None
    if version not in (None, "1", "2", "3", "4", str(SCHEMA_VERSION)):
        raise ValueError(f"unsupported schema version: {version}")
    connection.execute("PRAGMA foreign_keys = ON")
    with connection:
        # sqlite3 does not start a transaction for DDL by itself.
        if not connection.in_transaction:
            connection.execute("BEGIN")
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
        for table, additions in COLUMN_MIGRATIONS.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        connection.execute("INSERT INTO metadata(key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           ("schema_version", str(SCHEMA_VERSION)))

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        initialize_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection
