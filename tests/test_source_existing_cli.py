"""Manual commands preserve tool-free replay and reject the wrong worker owner."""

import json

from test_source_existing_delivery import admitted
from typer.testing import CliRunner

from html_to_markdown import source_existing_cli as cli
from html_to_markdown.cli import app

runner = CliRunner()


def test_manual_replay_never_opens_an_inference_client(capture, tmp_path, monkeypatch):
    _, _, _, journal, validation, _, _, _, _ = admitted(capture, tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline replay must not invoke an inference worker")

    monkeypatch.setattr(cli, "client_for_host", forbidden)
    result = runner.invoke(
        app,
        [
            "review-existing-source",
            "--input-journal",
            str(journal),
            "--output",
            str(validation),
            "--replay",
        ],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["new_editor_proposals"] == 0
    assert summary["admission_approved"] is False


def test_manual_admission_rejects_foreign_worker_root(capture, tmp_path):
    current, _, _, journal, validation, _, _, _, _ = admitted(capture, tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    before = current.read_bytes()
    result = runner.invoke(
        app,
        [
            "admit-existing-source",
            "--capture",
            str(capture),
            "--review",
            str(current.parent.parent),
            "--record",
            str(current),
            "--input-journal",
            str(journal),
            "--validation-output",
            str(validation),
            "--worker-root",
            str(foreign),
        ],
    )
    assert result.exit_code != 0
    assert "owning Ubuntu worker root" in result.output
    assert current.read_bytes() == before
