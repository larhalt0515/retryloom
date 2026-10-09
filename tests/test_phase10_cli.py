"""Independent CLI and migration acceptance checks for Phase 10."""
import sqlite3
import subprocess

from typer.testing import CliRunner

from retryloom.main import app
from retryloom.storage.database import connect
from test_apply import eligible


def repository(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    return root


def files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def test_stats_without_storage_creates_nothing(tmp_path):
    root = repository(tmp_path)
    before = files(root)
    result = CliRunner().invoke(app, ["stats", "--repository", str(root)])
    assert result.exit_code == 0, result.output
    assert "Sessions analyzed: 0" in result.output
    assert "N/A" in result.output
    assert files(root) == before
    assert not (root / ".retryloom").exists()


def test_stats_preserves_existing_database_and_files(tmp_path):
    root = repository(tmp_path)
    with connect(root / ".retryloom/retryloom.db") as db:
        db.execute("INSERT INTO sessions(id,agent,repository,started_at) VALUES ('s','Claude',?,'2026-01-01')", (str(root),))
    before = files(root)
    first = CliRunner().invoke(app, ["stats", "--repository", str(root)])
    second = CliRunner().invoke(app, ["stats", "--repository", str(root)])
    assert first.exit_code == second.exit_code == 0
    assert "Sessions analyzed: 1" in first.output
    assert first.output == second.output
    assert files(root) == before


def test_apply_listing_is_read_only(tmp_path):
    root = repository(tmp_path)
    with connect(root / ".retryloom/retryloom.db"):
        pass
    before = files(root)
    result = CliRunner().invoke(app, ["apply", "--repository", str(root)])
    assert result.exit_code == 0, result.output
    assert "None available" in result.output
    assert files(root) == before


def test_stats_rejects_symlink_storage(tmp_path):
    root = repository(tmp_path)
    outside = tmp_path / "external"
    outside.mkdir()
    with connect(outside / "retryloom.db"):
        pass
    (root / ".retryloom").symlink_to(outside, target_is_directory=True)
    before = (outside / "retryloom.db").read_bytes()
    result = CliRunner().invoke(app, ["stats", "--repository", str(root)])
    assert result.exit_code != 0
    assert (outside / "retryloom.db").read_bytes() == before


def test_v4_migration_adds_application_audit_without_changing_evidence(tmp_path):
    path = tmp_path / "legacy.db"
    db = connect(path)
    db.execute("DROP TABLE applications")
    db.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
    db.execute("INSERT INTO sessions(id,agent,repository,started_at) VALUES ('s','fixture','/workspace/example','2026-01-01')")
    db.commit()
    before = [tuple(row) for row in db.execute("SELECT * FROM sessions")]
    db.close()
    db = connect(path)
    assert [tuple(row) for row in db.execute("SELECT * FROM sessions")] == before
    assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "5"
    assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    db.close()


def cli_project(eligible):
    source, root, lesson = eligible
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    path = root / ".retryloom/retryloom.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as target:
        source.backup(target)
    return root, lesson, path


def test_apply_cli_preview_decline_confirm_and_repeat(eligible):
    root, lesson, path = cli_project(eligible)
    args = ["apply", "--repository", str(root), "--lesson-id", lesson.id]
    before = files(root)
    preview = CliRunner().invoke(app, [*args, "--preview"])
    assert preview.exit_code == 0, preview.output
    assert "Target:" in preview.output and lesson.rule in preview.output
    assert files(root) == before
    decline = CliRunner().invoke(app, args, input="n\n")
    assert decline.exit_code == 0, decline.output
    assert files(root) == before
    success = CliRunner().invoke(app, args, input="y\n")
    assert success.exit_code == 0, success.output
    assert (root / "AGENTS.md").read_text().count(lesson.rule) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM lessons WHERE id=?", (lesson.id,)).fetchone()[0] == "applied"
        assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    after = files(root)
    repeat = CliRunner().invoke(app, args)
    assert repeat.exit_code == 0, repeat.output
    assert "Already applied" in repeat.output
    assert files(root) == after


def test_apply_cli_skills_alias(eligible):
    root, lesson, _ = cli_project(eligible)
    args = ["apply", "--repository", str(root), "--lesson-id", lesson.id, "--target", "skills"]
    preview = CliRunner().invoke(app, [*args, "--preview"])
    assert preview.exit_code == 0, preview.output
    assert not (root / ".retryloom/skills").exists()
    success = CliRunner().invoke(app, args, input="y\n")
    assert success.exit_code == 0, success.output
    files_written = list((root / ".retryloom/skills").glob("*.md"))
    assert len(files_written) == 1
    assert lesson.rule in files_written[0].read_text()


def test_apply_cli_v4_preview_does_not_migrate_database(eligible):
    root, lesson, path = cli_project(eligible)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE applications")
        db.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
    before = files(root)
    args = ["apply", "--repository", str(root), "--lesson-id", lesson.id]
    preview = CliRunner().invoke(app, [*args, "--preview"])
    assert preview.exit_code == 0, preview.output
    assert files(root) == before
    decline = CliRunner().invoke(app, args, input="n\n")
    assert decline.exit_code == 0, decline.output
    assert files(root) == before


def test_apply_default_target_follows_configured_claude_agent(eligible):
    root, lesson, _ = cli_project(eligible)
    (root / ".retryloom/config.toml").write_text('[project]\ndefault_agent = "claude"\n')
    result = CliRunner().invoke(app, ["apply", "--repository", str(root), "--lesson-id", lesson.id], input="y\n")
    assert result.exit_code == 0, result.output
    assert (root / "CLAUDE.md").exists()
    assert not (root / "AGENTS.md").exists()
