from typer.testing import CliRunner

from reflex.main import app


runner = CliRunner()


def test_help_lists_all_phase_zero_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("init", "scan", "learn", "replay", "apply", "stats"):
        assert command in result.stdout


def test_command_is_callable(tmp_path) -> None:
    import subprocess
    subprocess.run(["git", "init", "--template=", str(tmp_path)], check=True, capture_output=True)
    result = runner.invoke(app, ["init", "--repository", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Initialized Reflex" in result.stdout
    assert (tmp_path / ".reflex/reflex.db").is_file()

