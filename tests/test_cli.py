import shutil
from pathlib import Path

from test_release_contract_v2 import prepared_snapshot
from typer.testing import CliRunner

from html_to_markdown.cli import app
from html_to_markdown.package import (
    build_manifest,
    sha256_file,
    write_publication_receipt,
    write_release,
)


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


def test_verify_publication_binds_an_optional_receipt_digest(tmp_path: Path) -> None:

    output, store = prepared_snapshot(tmp_path)
    write_release(
        output, build_manifest(output, store, "2026-10-02T00:00:00Z", "2026-10-02T00:01:00Z")
    )
    write_publication_receipt(
        output,
        tag="content-20261002T000100Z",
        source_commit="a" * 40,
        created_at="2026-10-02T00:00:00Z",
        published_at="2026-10-02T00:01:00Z",
    )
    store.close()
    args = [
        "verify-publication",
        "--output",
        str(output),
        "--release-tag",
        "content-20261002T000100Z",
        "--publication-sha256",
    ]
    # The CLI verifies the closed release download, before extracted payloads.

    shutil.rmtree(output / "content")
    for name in ("state.sqlite", "curation-audit.json", "SHA256SUMS"):
        (output / name).unlink(missing_ok=True)
    valid = CliRunner().invoke(app, [*args, sha256_file(output / "publication.json")])
    assert valid.exit_code == 0, valid.output
    invalid = CliRunner().invoke(app, [*args, "0" * 64])
    assert invalid.exit_code != 0
