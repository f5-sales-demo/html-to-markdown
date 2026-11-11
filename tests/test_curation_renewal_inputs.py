"""Renewal uses complete captured and privacy-stage evidence, never stale admission."""

import json

from test_curation_renewal_publication import Client
from test_source_review_jobs import LocalClient

from html_to_markdown.curation_renewal import TOPICS
from html_to_markdown.curation_renewal_inputs import prepare_renewals
from html_to_markdown.curation_renewal_jobs import review_batch
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
