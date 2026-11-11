"""Admission validates a real curation preview while retaining privacy input bytes."""

from pathlib import Path

import pytest

from html_to_markdown.content_policy import load_content_policy
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document
from html_to_markdown.source_curation import CurationRequired, preview_curation, record_exception

ARTICLE = "articles/content/my-f5-com/K000123456/index.md"


def source(body: str) -> dict[str, bytes]:
    metadata = PageMetadata(
        sourceId="my-f5-com",
        title="Service reference",
        slug="k000123456",
        url="https://my.f5.com/manage/s/article/K000123456",
        category="knowledge",
    )
    return {ARTICLE: serialize_document(metadata, body).encode()}


def test_real_api_preview_retains_upstream_and_original_blank_fields(tmp_path: Path) -> None:
    files = source(
        "# Service reference\n\n"
        "Use the HTTP load balancer Create operation:\n\n"
        "https://docs.cloud.f5.com/docs-v2/api/http-loadbalancer#Create\n\n"
        "| Existing capability | New capability |\n| --- | --- |\n| Optional mapping | |\n"
    )
    original = files[ARTICLE]
    first = preview_curation(tmp_path, files, ARTICLE)
    assert files[ARTICLE] == original
    assert first["source_files_sha256"] == sha(json_bytes({ARTICLE: sha(original)}))
    assert load_content_policy().fallback in first["body"]
    assert "| Optional mapping | |" in first["body"]
    assert first["curation_sha256"] != first["source_files_sha256"]
    assert preview_curation(tmp_path, files, ARTICLE) == first


def test_missing_retirement_review_stays_explicitly_queued(tmp_path: Path) -> None:
    files = source(
        "# Platform mapping\n\n"
        "The HTTP load balancer secures applications and forwards requests to healthy origins.\n\n"
        "The example uses SMSv1 with an aws_vpc_site.\n"
    )
    with pytest.raises(CurationRequired) as failure:
        preview_curation(tmp_path, files, ARTICLE)
    assert failure.value.reason == "source_curation_requires_review"
    assert (tmp_path / "curation-exceptions.json").is_file()
    assert not list(tmp_path.glob("reviewed/content/**/index.md"))


def test_preview_refuses_unsafe_article_or_asset_paths(tmp_path: Path) -> None:
    files = source("# Service\n\nA complete service explanation.\n")
    files["articles/../outside"] = b"unsafe"
    with pytest.raises(ValueError, match="unsafe"):
        preview_curation(tmp_path, files, ARTICLE)


def test_successful_replay_resolves_queue_and_preserves_previous_failure(tmp_path: Path) -> None:
    files = source(
        "# Service\n\nThe origin pool forwards requests to healthy application origins.\n"
    )
    preview = preview_curation(tmp_path, files, ARTICLE)
    record_exception(tmp_path, preview["source_files_sha256"], preview["metadata"])
    replay = preview_curation(tmp_path, files, ARTICLE)
    assert replay == preview
    assert (tmp_path / "curation-exceptions.json").read_bytes() == json_bytes({})
    assert len(list((tmp_path / "curation-history").glob("*.json"))) == 1
