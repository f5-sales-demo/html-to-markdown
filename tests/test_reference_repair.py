"""Exact reference repairs cannot consume editorial corrections or bypass source checks."""

import io
import json
import subprocess
import sys

import pytest
from PIL import Image
from test_curation_section_validation import Client as CurationClient
from test_source_review_jobs import LocalClient

import html_to_markdown.reference_repair_inputs as repair_inputs
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal import TOPICS, proposal_context
from html_to_markdown.curation_renewal_inputs import prepare_renewals
from html_to_markdown.curation_renewal_jobs import replay_renewal
from html_to_markdown.curation_renewal_review import review_request
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.models import PageMetadata
from html_to_markdown.reference_repair_inputs import (
    prepare_reference_repair,
    reference_delta,
    safe_file,
)
from html_to_markdown.reference_repair_review import project_range, validate_reference_repair
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.source_curation import CurationRequired
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews


def evidence(body, value):
    response = {
        "status": "completed",
        "model": body["model"],
        "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}],
    }
    return ResponseEvidence(
        request_sha256=request_hash(body),
        response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
        model=body["model"],
        reasoning="high",
        prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1].strip(),
        request=body,
        response=response,
        error=None,
        transport="codex_litellm",
        runtime_version="test",
    )


class RepairClient(CurationClient):
    def __init__(self, failure=None):
        super().__init__()
        self.repair_failure = failure

    def execute(self, body):
        if body["text"]["format"]["name"] != "referencerepairvalidation":
            return super().execute(body)
        self.calls.append(body)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        value = {
            "identity": payload["source"]["identity"],
            "binding_sha256": payload["reference_repair"]["binding_sha256"],
            "output_sha256": payload["candidate"]["output_sha256"],
            "approved": False,
            "retirement_valid": False,
            "relevance_valid": True,
            "privacy_valid": True,
            "extraction_valid": True,
            "technical_relationships_preserved": True,
            "media_valid": True,
            "complete_document_reviewed": True,
            "complete_media_reviewed": True,
            "approved_safe_findings": [],
            "failures": [],
            "unresolved": [],
            "source_reference_repair_valid": True,
            "remaining_conditions_source_only": True,
            "resolved_original_condition_indices": [0],
        }
        if self.repair_failure:
            value.update(self.repair_failure)
        return evidence(body, value)


# Preserve the complete synthetic capture and failed-source ledger together.
# pylint: disable-next=too-many-locals
def prepared(capture, tmp_path, monkeypatch):
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(image, format="PNG")
    raw = image.getvalue()
    digest = sha(raw)
    reference = "assets/" + digest + ".png"
    body = (
        "# Configure service\n\nUse port 8443 before configuration.\n\n![Controls]("
        + reference
        + ")\n\nExpect healthy origins within 30 seconds.\n"
    )
    article = serialize_document(PageMetadata.model_validate(item["metadata"]), body).encode()
    item.update(
        http_status=200,
        article_sha256=sha(article),
        media=[
            {
                "path": "assets/" + digest + ".png",
                "url": "https://my.f5.com/image.png",
                "sha256": digest,
                "media_type": "image/png",
            }
        ],
    )
    atomic_private(capture / item["media"][0]["path"], raw)
    atomic_private(capture / item["article_path"], article)
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)

    def missing_guard(*_args):
        raise CurationRequired(CurationRequired.reason)

    monkeypatch.setattr("html_to_markdown.source_review_jobs.preview_curation", missing_guard)
    review = tmp_path / "review"
    prepare_reviews(capture, review)
    run_reviews(capture, review, phase="all", journal=tmp_path / "journal")
    output = tmp_path / "curation"
    prepared_inventory = prepare_renewals(capture, review, output)
    current_item = prepared_inventory["documents"][0]
    current_dir = output / current_item["binding_sha256"]
    current = json.loads((current_dir / "context.json").read_bytes())
    bad_body = current["body"].replace(reference, reference + ".png")
    old_article = serialize_document(
        PageMetadata.model_validate(current["metadata"]), bad_body
    ).encode()
    old_meta, bad_body = split_document(old_article.decode())
    files = {
        p.relative_to(current_dir / "inputs").as_posix(): p.read_bytes()
        for p in (current_dir / "inputs").rglob("*")
        if p.is_file()
    }
    files[item["article_path"]] = old_article
    old = proposal_context(
        old_meta,
        bad_body,
        current["media"],
        sha(json_bytes({name: sha(data) for name, data in sorted(files.items())})),
        current["policy_sha256"],
        current["evidence"],
    )
    old_dir = output / old["binding_sha256"]
    for name, data in files.items():
        atomic_private(old_dir / "inputs" / name, data)
    atomic_private(old_dir / "context.json", json_bytes(old))
    visual = json.loads((current_dir / "visual.json").read_bytes())
    atomic_private(old_dir / "visual.json", json_bytes(visual))
    decision = {
        "identity": old["identity"],
        "binding_sha256": old["binding_sha256"],
        "unresolved": [
            "The necessary media placement has a duplicated extension and does not resolve."
        ],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "Independent useful procedure.",
                "removals": [],
                "reference_edits": [],
                "media": [
                    {
                        "sha256": pin,
                        "disposition": "keep",
                        "reason": "Preserve the source controls.",
                        "masks": [],
                    }
                    for pin in old["media"]
                ],
            }
            for topic in TOPICS
        ],
    }
    atomic_private(
        old_dir / "attempt-002/editor.json",
        json_bytes(
            evidence(
                review_request(old, visual, feedback=["original extraction failure"]), decision
            ).model_dump(mode="json")
        ),
    )
    atomic_private(
        old_dir / "state.json",
        json_bytes(
            {
                "attempt": 3,
                "status": "queued",
                "retryable": False,
                "feedback": ["original extraction failure"],
            }
        ),
    )
    record = review / "documents" / (sha(item["identity"]) + ".json")
    return output, old, old_dir, record


def test_exact_source_repair_preserves_exhausted_ledger_and_replays(capture, tmp_path, monkeypatch):
    output, old, old_dir, record = prepared(capture, tmp_path, monkeypatch)
    original_files = {
        p.relative_to(old_dir).as_posix(): sha(p.read_bytes())
        for p in old_dir.rglob("*")
        if p.is_file()
    }
    repair = prepare_reference_repair(output, old["binding_sha256"], 2, capture, record)
    assert len(repair["changes"]) == 1 and repair["original_editor_budget"] == 3
    client = RepairClient()
    result = validate_reference_repair(output, repair["binding_sha256"], client)
    assert ".png.png" not in result["candidate"]["body"]
    assert "8443" in result["candidate"]["body"] and "30 seconds" in result["candidate"]["body"]
    assert all(body["metadata"]["content_review_role"] == "validator" for body in client.calls)
    current = json.loads((output / result["binding_sha256"] / "context.json").read_bytes())
    visual = json.loads((output / result["binding_sha256"] / "visual.json").read_bytes())
    assert replay_renewal(current, visual, output) == result
    assert {
        p.relative_to(old_dir).as_posix(): sha(p.read_bytes())
        for p in old_dir.rglob("*")
        if p.is_file()
    } == original_files
    assert (
        json.loads((output / result["binding_sha256"] / "state.json").read_bytes())["attempt"] == 3
    )


@pytest.mark.parametrize(
    "failure",
    [
        {"remaining_conditions_source_only": False},
        {"source_reference_repair_valid": False},
        {"resolved_original_condition_indices": []},
        {"resolved_original_condition_indices": [0, 0]},
        {"privacy_valid": False},
        {"complete_media_reviewed": False},
        {"approved": True},
        {"output_sha256": "a" * 64},
    ],
)
def test_failed_source_resolution_cannot_reach_curation(capture, tmp_path, monkeypatch, failure):
    output, old, _, record = prepared(capture, tmp_path, monkeypatch)
    repair = prepare_reference_repair(output, old["binding_sha256"], 2, capture, record)
    client = RepairClient(failure)
    with pytest.raises(ValueError):
        validate_reference_repair(output, repair["binding_sha256"], client)
    assert len(client.calls) == 1


def test_reference_repair_rejects_unrelated_text_and_unavailable_assets():
    raw = b"image"
    digest = sha(raw)
    name = "assets/" + digest + ".png"
    before = "Use port 8443. ![UI](" + name + ".png)"
    after = before.replace(name + ".png", name)
    files = {"articles/content/docs/a/assets/" + digest + ".png": raw}
    assert len(reference_delta(before, after, files, "articles/content/docs/a/index.md")) == 1
    with pytest.raises(ValueError, match="unrelated"):
        reference_delta(
            before, after.replace("8443", "443"), files, "articles/content/docs/a/index.md"
        )
    with pytest.raises(ValueError, match="unavailable"):
        reference_delta(before, after, {}, "articles/content/docs/a/index.md")


def test_source_repair_cannot_rebase_partial_reference_deletion():
    changes = [{"start": 10, "end": 20, "before": "reference1", "after": "ref1"}]
    assert project_range(0, 30, changes) == (0, 24)
    with pytest.raises(ValueError, match="boundary"):
        project_range(15, 30, changes)


def test_source_repair_cannot_read_traversal_or_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("private")
    (tmp_path / "alias").symlink_to(target)
    for name in ["../target", "alias", str(target)]:
        with pytest.raises(ValueError, match="unsafe"):
            safe_file(tmp_path, name)


@pytest.mark.parametrize(
    "module",
    [
        "html_to_markdown.cli",
        "html_to_markdown.curation_renewal_jobs",
        "html_to_markdown.reference_repair_review",
    ],
)
def test_reference_repair_cold_imports_have_no_cycle(module):
    result = subprocess.run(
        [sys.executable, "-c", "import importlib; importlib.import_module(" + repr(module) + ")"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("field", ["source_html_path", "article_path", "media"])
def test_changed_capture_cannot_become_reference_repair(capture, tmp_path, monkeypatch, field):
    output, old, _, record = prepared(capture, tmp_path, monkeypatch)
    saved = json.loads(record.read_bytes())
    name = saved["item"][field] if field != "media" else saved["item"]["media"][0]["path"]
    (capture / name).write_bytes((capture / name).read_bytes() + b"changed")
    with pytest.raises(ValueError, match="changed"):
        prepare_reference_repair(output, old["binding_sha256"], 2, capture, record)


def test_repaired_file_tampering_rejects_before_another_inference(capture, tmp_path, monkeypatch):
    output, old, _, record = prepared(capture, tmp_path, monkeypatch)
    repair = prepare_reference_repair(output, old["binding_sha256"], 2, capture, record)
    article = next(
        (output / "reference-repairs" / repair["binding_sha256"] / "repaired").rglob("index.md")
    )
    article.write_text(article.read_text().replace("8443", "443"))
    client = RepairClient()
    with pytest.raises(ValueError, match="output bytes changed"):
        validate_reference_repair(output, repair["binding_sha256"], client)
    assert not client.calls


def test_original_ledger_change_invalidates_repair(capture, tmp_path, monkeypatch):
    output, old, original, record = prepared(capture, tmp_path, monkeypatch)
    repair = prepare_reference_repair(output, old["binding_sha256"], 2, capture, record)
    state = json.loads((original / "state.json").read_bytes())
    state["feedback"].append("a different source condition")
    (original / "state.json").write_bytes(json_bytes(state))
    client = RepairClient()
    with pytest.raises(ValueError, match="ledger changed"):
        validate_reference_repair(output, repair["binding_sha256"], client)
    assert not client.calls


def test_metadata_change_is_not_a_reference_repair(capture, tmp_path, monkeypatch):
    output, old, _, record = prepared(capture, tmp_path, monkeypatch)
    original = repair_inputs.compile_source

    def changed_metadata(*args):
        candidate, visual, privacy, files, source = original(*args)
        candidate["metadata"]["title"] = "An unrelated article"
        return candidate, visual, privacy, files, source

    monkeypatch.setattr(repair_inputs, "compile_source", changed_metadata)
    with pytest.raises(ValueError, match="metadata or source dates"):
        prepare_reference_repair(output, old["binding_sha256"], 2, capture, record)


@pytest.mark.parametrize(
    "event", [{"type": "context.compacted"}, {"item": {"type": "command_execution"}}]
)
def test_source_repair_refuses_compacted_or_tool_using_model(capture, tmp_path, monkeypatch, event):
    output, old, _, record = prepared(capture, tmp_path, monkeypatch)
    repair = prepare_reference_repair(output, old["binding_sha256"], 2, capture, record)

    class UntrustedClient(RepairClient):
        def execute(self, body):
            result = super().execute(body)
            result.response["events"] = [event]
            result.response_sha256 = sha(
                json.dumps(result.response, sort_keys=True, separators=(",", ":"))
            )
            return result

    client = UntrustedClient()
    with pytest.raises(ValueError, match="tool or lost complete input"):
        validate_reference_repair(output, repair["binding_sha256"], client)
    assert len(client.calls) == 1
