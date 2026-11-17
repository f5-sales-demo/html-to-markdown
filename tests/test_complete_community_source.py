"""Nested resource cards cannot replace the complete Community first post."""

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document
from html_to_markdown.source_review_jobs import _article_html, prepare_reviews

FIRST = '<h1>DNS failover</h1><p>Prerequisite: configure the primary pool.</p><article class="onebox-body"><p>Promotional resource</p></article><h2>Expected results</h2><pre><code>dig example.test\nstatus: NOERROR</code></pre><p>Failover preserves the fallback pool.</p>'


def _raw(capture: Path, item: dict, cooked: str) -> None:
    raw = json_bytes(
        {
            "post_stream": {
                "posts": [
                    {"post_number": 1, "cooked": cooked},
                    {"post_number": 2, "cooked": "REPLY_ONLY_CONTENT"},
                ]
            }
        }
    )
    item.update(source_html_path="captures/" + sha(raw) + ".html", source_html_sha256=sha(raw))
    atomic_private(capture / item["source_html_path"], raw)


def test_complete_first_post_and_transcript_survive_nested_onebox(tmp_path: Path) -> None:
    item = {"sourceId": "community-f5-com"}
    _raw(tmp_path, item, FIRST)
    actual = _article_html(tmp_path, item)
    assert actual == str(BeautifulSoup(FIRST, "html.parser"))
    assert "Prerequisite: configure the primary pool." in actual
    assert "dig example.test\nstatus: NOERROR" in actual
    assert "Failover preserves the fallback pool." in actual
    assert "REPLY_ONLY_CONTENT" not in actual


def test_outside_card_edit_invalidates_complete_source_binding(
    capture: Path, tmp_path: Path
) -> None:
    metadata = PageMetadata(
        sourceId="community-f5-com",
        title="DNS failover",
        slug="123456",
        url="https://community.f5.com/t/123456",
        category="technical-articles",
    )
    article = serialize_document(
        metadata, "# DNS failover\n\nConfigure the primary pool.\n"
    ).encode()
    item = {
        "identity": "community-f5-com:t/123456",
        "sourceId": "community-f5-com",
        "url": metadata.url,
        "capture_complete": True,
        "disposition": "queued",
        "reason": "admission_review_required",
        "metadata": metadata.model_dump(by_alias=True),
        "article_path": "articles/content/community-f5-com/t/123456/index.md",
        "article_sha256": sha(article),
        "media": [],
    }
    atomic_private(capture / item["article_path"], article)
    _raw(capture, item, FIRST)
    inventory = {
        "binding": {"source": "community-f5-com"},
        "completeness": {"enumeration": True},
        "documents": [item],
    }
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    review = tmp_path / "review"
    initial = prepare_reviews(capture, review)
    old = initial["documents"][0]["binding_sha256"]
    _raw(capture, item, FIRST.replace("fallback pool", "secondary fallback pool"))
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    changed = prepare_reviews(capture, review)
    assert changed["documents"][0]["binding_sha256"] != old
    record = json.loads((review / changed["documents"][0]["path"]).read_bytes())
    assert record["disposition"] == "queued" and not record["phases"]
    assert (capture / item["article_path"]).read_bytes() == article


@pytest.mark.parametrize("source", ["docs-cloud-f5-com", "my-f5-com", "www-f5-com"])
def test_other_sources_keep_primary_scope_and_exclude_navigation(
    tmp_path: Path, source: str
) -> None:
    raw = "<header>NAVIGATION_ONLY</header><main>" + FIRST + "</main><footer>FOOTER_ONLY</footer>"
    atomic_private(tmp_path / "page.html", raw.encode())
    actual = _article_html(tmp_path, {"sourceId": source, "source_html_path": "page.html"})
    assert "Prerequisite: configure" in actual and "Expected results" in actual
    assert "NAVIGATION_ONLY" not in actual and "FOOTER_ONLY" not in actual
