"""Shared complete-source fixtures for admission and publication tests."""

from pathlib import Path
from typing import Any

import pytest

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document


@pytest.fixture
def capture(tmp_path: Path) -> Path:
    root = tmp_path / "capture"
    metadata = PageMetadata(
        sourceId="my-f5-com",
        title="Configure service",
        slug="k000123456",
        url="https://my.f5.com/manage/s/article/K000123456",
        category="knowledge",
    )
    article = serialize_document(
        metadata, "# Configure service\n\nRun `curl --retry 3` within 30 seconds.\n"
    ).encode()
    item: dict[str, Any] = {
        "identity": "my-f5-com:K000123456",
        "sourceId": "my-f5-com",
        "url": metadata.url,
        "capture_complete": True,
        "disposition": "queued",
        "reason": "admission_review_required",
        "source_html_path": "captures/source.html",
        "source_html_sha256": sha(b"captured"),
        "article_path": "articles/content/my-f5-com/K000123456/index.md",
        "article_sha256": sha(article),
        "metadata": metadata.model_dump(by_alias=True),
        "media": [],
    }
    atomic_private(root / item["source_html_path"], b"captured")
    atomic_private(root / item["article_path"], article)
    atomic_private(
        root / "inventory.json",
        json_bytes({"binding": {}, "completeness": {"enumeration": True}, "documents": [item]}),
    )
    return root


@pytest.fixture(autouse=True)
def isolate_installed_policy_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never inherit an operator's active policy installation."""
    monkeypatch.setenv("HTML_TO_MARKDOWN_POLICY_STATE", str(tmp_path / "installed-policy"))
