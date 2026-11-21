"""An existing proposal is projected through current guards without resetting its ledger."""

import json

import pytest
import test_curation_renewal as fixtures
from test_curation_section_validation import Client
from typer.testing import CliRunner

from html_to_markdown.cli import app
from html_to_markdown.curation import json_bytes, load_curation_policy
from html_to_markdown.curation_existing_validation import validate_existing_curation
from html_to_markdown.curation_renewal import proposal_context
from html_to_markdown.curation_renewal_jobs import replay_renewal, review_batch, run_renewal
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


def test_original_compiler_failure_selects_only_independent_validation(tmp_path):
    context, directory, _ = prepared(tmp_path)
    (directory / "attempt-000/candidate.json").unlink()
    (directory / "attempt-000/failure.json").write_bytes(
        json_bytes({"error": "ambiguous or changed renewal span after preceding topic"})
    )
    originals = {
        p.relative_to(directory).as_posix(): sha(p.read_bytes())
        for p in directory.rglob("*")
        if p.is_file()
    }
    client = Client()
    result = validate_existing_curation(tmp_path, context["binding_sha256"], 0, client)
    assert not any(c["metadata"]["content_review_role"] == "editor" for c in client.calls)
    origin = result["selected_existing_proposal"]
    assert origin["candidate_sha256"] is None and origin["new_editor_proposals"] == 0
    assert origin["original_compilation_failure_sha256"] == originals["attempt-000/failure.json"]
    assert all(sha((directory / name).read_bytes()) == digest for name, digest in originals.items())
    selected = tmp_path / result["binding_sha256"]
    state = json.loads((selected / "state.json").read_bytes())
    assert state["attempt"] == 3 and len(state["feedback"]) == 3
    current = json.loads((selected / "context.json").read_bytes())
    assert replay_renewal(current, [], tmp_path) == result


@pytest.mark.parametrize(
    "failure",
    [None, "model refusal", "original renewal structural span changed after preceding topic"],
)
def test_missing_candidate_requires_qualified_original_compiler_failure(tmp_path, failure):
    context, directory, _ = prepared(tmp_path)
    (directory / "attempt-000/candidate.json").unlink()
    path = directory / "attempt-000/failure.json"
    if failure is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(json_bytes({"error": failure}))
    client = Client()
    with pytest.raises(ValueError, match="qualified compiler failure"):
        validate_existing_curation(tmp_path, context["binding_sha256"], 0, client)
    assert not client.calls


def test_normal_batch_replays_selected_validation_without_new_inference(tmp_path, monkeypatch):
    context, _, _ = prepared(tmp_path)
    result = validate_existing_curation(tmp_path, context["binding_sha256"], 0, Client())

    class OfflineClient:
        def execute(self, _):
            raise AssertionError("Selected validation must replay without new inference")

        def close(self):
            pass

    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_jobs.client_for_host", lambda *a, **k: OfflineClient()
    )
    pin = result["binding_sha256"]
    report = review_batch(
        tmp_path,
        {
            "documents": [
                {
                    "identity": context["identity"],
                    "binding_sha256": pin,
                    "context_path": pin + "/context.json",
                    "visual_path": pin + "/visual.json",
                }
            ]
        },
        "all",
        tmp_path / "journal",
    )
    assert report["independently_validated"] == 1 and report["exceptions"] == 0


def test_previous_attempt_exception_keeps_its_distinct_reason(tmp_path):
    reason = "previous_curation_attempts_require_validation"
    report = review_batch(
        tmp_path,
        {"documents": [{"identity": "my-f5-com:K000123456", "reason": reason}]},
        "all",
        tmp_path / "journal",
    )
    assert report["documents"][0]["reason"] == reason and report["exceptions"] == 1
