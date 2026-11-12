"""Exercise the exact option and stdin interfaces used by the authenticated transport."""

import json

import pytest
from typer.testing import CliRunner

from html_to_markdown.cli import app


@pytest.mark.parametrize("command", ["export", "activate", "confirm"])
def test_runtime_commands_accept_transport_options_and_exact_candidate_stdin(
    tmp_path, monkeypatch, command
):
    repository = tmp_path / "repo"
    state = tmp_path / "policy"
    queue = tmp_path / "queue"
    candidate = {"bundle_sha256": "a" * 64}
    calls = []

    def export(repo, root):
        calls.append((repo, root))
        return {"state": "policy_current"}

    def activation(repo, root, proposed):
        calls.append((repo, root, proposed))
        return proposed

    target = "export_policy" if command == "export" else "remote_" + command
    monkeypatch.setattr(
        "html_to_markdown.policy_transport." + target, export if command == "export" else activation
    )
    args = [
        command + "-policy-runtime",
        "--repository",
        str(repository),
        "--state-root",
        str(state),
    ]
    if command != "export":
        args.extend(["--queue-root", str(queue)])
    result = CliRunner().invoke(app, args, input=json.dumps(candidate))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == (
        {"state": "policy_current"} if command == "export" else candidate
    )
    expected_calls = (
        [(repository, state)] if command == "export" else [(repository, state, candidate)]
    )
    assert calls == expected_calls
