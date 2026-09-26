from pathlib import Path

from typer.testing import CliRunner

from html_to_markdown.cli import app


def test_help_and_empty_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "discover" in result.stdout
    result = runner.invoke(app, ["status", "--output", str(tmp_path)])
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"


def test_empty_validation(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["validate", "--output", str(tmp_path)])
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_quality_command_writes_both_reports(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["quality", "--candidate", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "quality-report.json").is_file()
    assert (tmp_path / "quality-report.md").is_file()
