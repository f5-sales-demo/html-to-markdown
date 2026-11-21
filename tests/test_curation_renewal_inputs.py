"""Renewal uses complete captured and privacy-stage evidence, never stale admission."""

import json

import pytest
from test_curation_renewal_publication import Client
from test_source_native_boundaries import Client as NativeClient
from test_source_native_boundaries import _capture_with_images
from test_source_review_jobs import LocalClient

import html_to_markdown.enrichment.native_visuals as native
import html_to_markdown.source_boundary_requests as requests
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal import TOPICS
from html_to_markdown.curation_renewal_inputs import prepare_renewals
from html_to_markdown.curation_renewal_jobs import review_batch
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_curation import CurationRequired
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews


def test_complete_curation_exception_can_review_and_replay_without_publishing(
    capture, tmp_path, monkeypatch
):
    def missing_guard(*args):
        raise CurationRequired(CurationRequired.reason)

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    monkeypatch.setattr("html_to_markdown.source_review_jobs.preview_curation", missing_guard)
    source_review = tmp_path / "source-review"
    prepare_reviews(capture, source_review)
    report = run_reviews(capture, source_review, phase="all", journal=tmp_path / "journal")
    assert report["documents"][0]["reason"] == "source_curation_requires_review"
    output = tmp_path / "renewal"
    inventory = prepare_renewals(capture, source_review, output)
    item = inventory["documents"][0]
    context = json.loads((output / item["context_path"]).read_bytes())
    assert context["evidence"]["source_article_html"]
    assert context["evidence"]["original"]["body"] == context["body"]
    assert len(context["evidence"]["policy_topics"]) == 5
    assert "curl --retry 3" in context["body"] and "30 seconds" in context["body"]
    proposal = {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "independent technical procedure",
                "removals": [],
                "media": [],
                "reference_edits": [],
            }
            for topic in TOPICS
        ],
        "unresolved": [],
    }
    client = Client(context, proposal)
    client.close = lambda: None
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_jobs.CodexClient", lambda *args, **kwargs: client
    )
    assert (
        review_batch(output, inventory, "all", tmp_path / "journal")["independently_validated"] == 1
    )
    assert review_batch(output, inventory, "replay", tmp_path / "journal")["exceptions"] == 0
    assert len(client.calls) == 2
    record = json.loads(next((source_review / "documents").glob("*.json")).read_bytes())
    assert record["disposition"] == "queued"
    # Changing raw source content cannot reuse preparation or model decisions.
    article = next((capture / "articles").rglob("index.md"))
    article.write_text(article.read_text().replace("30 seconds", "60 seconds"))
    stale = prepare_renewals(capture, source_review, output)
    assert stale["documents"][0]["reason"] == "source_review_prerequisite_failed"
    assert review_batch(output, stale, "all", tmp_path / "journal")["exceptions"] == 1
    assert len(client.calls) == 2


@pytest.fixture
def complete_native_curation(capture, tmp_path, monkeypatch):
    monkeypatch.setattr(native, "MAX_DECODED_BYTES", 600)
    monkeypatch.setattr(requests, "MAX_DECODED_BYTES", 600)
    item = _capture_with_images(capture)
    client = NativeClient()
    monkeypatch.setattr(
        "html_to_markdown.source_review_jobs.CodexClient", lambda *args, **kwargs: client
    )

    def missing_guard(*args):
        raise CurationRequired(CurationRequired.reason)

    monkeypatch.setattr("html_to_markdown.source_review_jobs.preview_curation", missing_guard)
    review = tmp_path / "native-source-review"
    prepare_reviews(capture, review)
    record = next((review / "documents").glob("*.json"))
    saved = json.loads(record.read_bytes())
    saved["privacy_feedback"] = ["First checked correction", "Second checked correction"]
    record.write_bytes(json_bytes(saved))
    report = run_reviews(capture, review, phase="all", journal=tmp_path / "native-journal")
    assert report["documents"][0]["reason"] == "source_curation_requires_review"
    complete = json.loads(record.read_bytes())
    assert all(complete["phases"][phase]["source_boundary"] for phase in ("relevance", "privacy"))
    assert complete["privacy_feedback"] == saved["privacy_feedback"]

    def forbid_inference(*args, **kwargs):
        pytest.fail("curation preparation must independently replay without new inference")

    monkeypatch.setattr(NativeClient, "execute", forbid_inference)
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.CodexClient.execute", forbid_inference
    )
    monkeypatch.setattr(
        "html_to_markdown.enrichment.queued_client.QueuedClient.execute", forbid_inference
    )
    return review, record, item


def test_native_curation_replays_complete_source_media_and_correction_history(
    complete_native_curation, capture, tmp_path
):
    review, record, item = complete_native_curation
    before = record.read_bytes()
    output = tmp_path / "renewal"
    inventory = prepare_renewals(capture, review, output)
    entry = inventory["documents"][0]
    assert entry["reason"] == "independent_catalog_review_required"
    context = json.loads((output / entry["context_path"]).read_bytes())
    original = context["evidence"]["original"]["body"]
    for media in item["media"]:
        original = original.replace(
            "assets/" + media["sha256"] + ")", "assets/" + media["sha256"] + ".png)"
        )
    assert context["body"] == original
    assert context["body"] == context["evidence"]["privacy_candidate"]["body"]
    assert "curl --retry 3" in context["body"] and "30 seconds" in context["body"]
    assert context["evidence"]["source_article_html"]
    assets = list((output / entry["binding_sha256"] / "inputs").rglob("assets/*"))
    assert {sha(path.read_bytes()) for path in assets} == {
        media["sha256"] for media in item["media"]
    }
    assert len(json.loads((output / entry["visual_path"]).read_bytes())) == 16
    assert prepare_renewals(capture, review, output) == inventory
    assert record.read_bytes() == before


@pytest.mark.parametrize("phase", ["relevance", "privacy"])
@pytest.mark.parametrize(
    "damage", ["missing", "reordered", "altered", "rejected", "stripped", "stale"]
)
def test_native_curation_rejects_unverified_prerequisites_without_mutating_records(
    complete_native_curation, capture, tmp_path, phase, damage
):
    review, record, _ = complete_native_curation
    saved = json.loads(record.read_bytes())
    response = saved["phases"][phase]
    proof = response["source_boundary"]
    boundary = proof.get("boundary") or proof
    assert len(boundary["parts"]) >= 2
    if damage == "missing":
        boundary["parts"].pop()
    elif damage == "reordered":
        boundary["parts"].reverse()
    elif damage == "altered":
        boundary["coverage_plan"]["partitions"][0]["positions"][0]["content_sha256"] = "0" * 64
    elif damage == "rejected":
        validation = boundary["parts"][0]["validation"]
        text = validation["response"]["output"][0]["content"][0]
        decision = json.loads(text["text"])
        decision["approved"] = False
        decision["failures"] = ["Necessary technical fact missing"]
        text["text"] = json.dumps(decision)
        validation["response_sha256"] = sha(
            json.dumps(validation["response"], sort_keys=True, separators=(",", ":"))
        )
    elif damage == "stripped":
        response.pop("source_boundary")
    else:
        proof["logical_request"]["instructions"] += "Changed policy context"
    record.write_bytes(json_bytes(saved))
    before = record.read_bytes()
    output = tmp_path / "rejected-renewal"
    inventory = prepare_renewals(capture, review, output)
    assert inventory["documents"][0]["reason"] == "source_review_prerequisite_failed"
    assert "context_path" not in inventory["documents"][0]
    assert not list(output.glob("*/context.json"))
    assert list((output / "prerequisite-errors").glob("*.json"))
    assert record.read_bytes() == before
