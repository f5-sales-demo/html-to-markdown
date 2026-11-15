"""An existing proposal is projected through current guards without resetting its ledger."""

import json

import pytest
from typer.testing import CliRunner
import test_curation_renewal as fixtures
from test_curation_section_validation import Client

from html_to_markdown.cli import app
from html_to_markdown.curation import json_bytes, load_curation_policy
from html_to_markdown.curation_existing_validation import validate_existing_curation
from html_to_markdown.curation_renewal import proposal_context
from html_to_markdown.curation_renewal_jobs import replay_renewal, run_renewal
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document


def prepared(tmp_path):
    original = fixtures.context()
    body = (
        original["body"]
        + "\n## Additional limits\n\n"
        + "\n\n".join(
            f"Keep limit {i} and port 8443 before configuring the load balancer."
            for i in range(100)
        )
        + "\n"
    )
    article = serialize_document(PageMetadata.model_validate(original["metadata"]), body)
    metadata, body = split_document(article)
    name = "articles/content/my-f5-com/K000123456/index.md"
    context = proposal_context(
        metadata, body, {}, sha(json_bytes({name: sha(article)})), load_curation_policy().sha256
    )
    directory = tmp_path / context["binding_sha256"]
    path = directory / "inputs" / name
    path.parent.mkdir(parents=True)
    path.write_text(article)
    (directory / "visual.json").write_bytes(json_bytes([]))
    with pytest.raises(ValueError, match="incomplete"):
        run_renewal(context, [], tmp_path, Client("incomplete"))
    state = json.loads((directory / "state.json").read_bytes())
    state.update(attempt=3, feedback=["old syntax failure", "old refusal", "incomplete fact check"])
    (directory / "state.json").write_bytes(json_bytes(state))
    return context, directory, path


def test_existing_selection_preserves_exhausted_ledger_and_replays(tmp_path):
    context, directory, _ = prepared(tmp_path)
    originals = {
        p.relative_to(directory).as_posix(): sha(p.read_bytes())
        for p in directory.rglob("*")
        if p.is_file()
    }
    client = Client()
    result = validate_existing_curation(tmp_path, context["binding_sha256"], 0, client)
    assert not any(c["metadata"]["content_review_role"] == "editor" for c in client.calls)
    assert all(sha((directory / name).read_bytes()) == digest for name, digest in originals.items())
    selected = tmp_path / result["binding_sha256"]
    state = json.loads((selected / "state.json").read_bytes())
    assert state["attempt"] == 3 and len(state["feedback"]) == 3
    current = json.loads((selected / "context.json").read_bytes())
    assert (
        result["candidate"]["body"]
        == json.loads((directory / "attempt-000/candidate.json").read_bytes())["body"]
    )
    assert replay_renewal(current, [], tmp_path) == result


def test_modified_original_input_fails_before_new_inference(tmp_path):
    context, _, article = prepared(tmp_path)
    article.write_text(article.read_text().replace("8443", "443"))
    client = Client()
    with pytest.raises(ValueError, match="input files"):
        validate_existing_curation(tmp_path, context["binding_sha256"], 0, client)
    assert not client.calls


def test_replay_rejects_modified_selected_input_instead_of_repairing_it(tmp_path):
    context, _, _ = prepared(tmp_path)
    result = validate_existing_curation(tmp_path, context["binding_sha256"], 0, Client())
    selected = tmp_path / result["binding_sha256"]
    article = next((selected / "inputs").rglob("index.md"))
    article.write_text(article.read_text().replace("8443", "443"))
    with pytest.raises(ValueError, match="changed"):
        validate_existing_curation(tmp_path, context["binding_sha256"], 0)


def test_cli_replay_reports_only_safe_summary(tmp_path):

    context, _, _ = prepared(tmp_path)
    validate_existing_curation(tmp_path, context["binding_sha256"], 0, Client())
    result = CliRunner().invoke(
        app,
        [
            "validate-existing-curation",
            "--output",
            str(tmp_path),
            "--original-binding",
            context["binding_sha256"],
            "--attempt",
            "0",
            "--replay",
        ],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["facts_checked"] == 105 and summary["new_editor_proposals"] == 0
    assert summary["publication_approved"] is False
    assert "8443" not in result.output
