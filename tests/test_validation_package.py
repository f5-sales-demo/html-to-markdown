import tarfile
from pathlib import Path

import pytest

from html_to_markdown.models import DiscoveredPage, PageMetadata, PageStatus
from html_to_markdown.package import build_manifest, sha256_file, write_release
from html_to_markdown.render import serialize_document
from html_to_markdown.state import StateStore
from html_to_markdown.validation import validate_document, validate_snapshot


def prepared_snapshot(tmp_path: Path) -> tuple[Path, StateStore]:
    output = tmp_path / "out"
    target = output / "content/docs-cloud-f5-com/a/index.md"
    target.parent.mkdir(parents=True)
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="A",
        slug="a",
        url="https://docs.cloud.f5.com/docs-v2/a",
        category="guide",
        tags=[],
    )
    target.write_text(serialize_document(metadata, "# A\n\nUseful body."), encoding="utf-8")
    store = StateStore(output / "state.sqlite")
    store.discover([DiscoveredPage(source_id=metadata.source_id, url=metadata.url)])
    store.mark(
        metadata.url,
        PageStatus.SUCCESS,
        output_path="content/docs-cloud-f5-com/a/index.md",
        digest=metadata.content_hash,
    )
    return output, store


def test_document_and_snapshot_validation(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    assert not validate_document(output / "content/docs-cloud-f5-com/a/index.md")
    validate_snapshot(output, store)
    store.close()


def test_failed_state_is_advisory_not_an_integrity_failure(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    other = "https://docs.cloud.f5.com/docs-v2/failure"
    store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=other)])
    store.mark(other, PageStatus.FAILED, error=RuntimeError("boom"))
    validate_snapshot(output, store)
    store.close()


def test_authentication_wall_is_advisory_not_an_integrity_failure(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    protected = "https://my.f5.com/manage/s/article/K000147377"
    store.discover([DiscoveredPage(source_id="my-f5-com", url=protected)])
    store.mark(
        protected,
        PageStatus.AUTHENTICATION_WALL,
        error=RuntimeError("authentication wall"),
    )
    validate_snapshot(output, store)
    store.close()


def test_document_validation_reports_malformed_hash_chrome_and_asset(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.md"
    malformed.write_text("plain markdown", encoding="utf-8")
    assert "no YAML" in validate_document(malformed)[0]
    broken = tmp_path / "broken.md"
    broken.write_text(
        """---
sourceId: docs-cloud-f5-com
title: Broken
slug: broken
url: https://docs.cloud.f5.com/docs-v2/broken
category: guide
publication_date: null
modification_date: null
content_hash: wrong
tags: [z, a, a]
---

Return to Top

![missing](assets/missing.png)
""",
        encoding="utf-8",
    )
    errors = validate_document(broken)
    assert any("content_hash mismatch" in item for item in errors)
    assert any("tags are not sorted" in item for item in errors)
    assert any("site chrome" in item for item in errors)
    assert any("broken local asset" in item for item in errors)


def test_archive_is_deterministic_and_checksummed(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    manifest = build_manifest(output, store, "2025-01-01T00:00:00Z", "2025-01-01T00:01:00Z")
    archive = write_release(output, manifest)
    first = sha256_file(archive)
    changed_times = dict(
        manifest,
        started_at="2025-02-01T00:00:00Z",
        ended_at="2025-02-01T00:01:00Z",
    )
    archive = write_release(output, changed_times)
    assert sha256_file(archive) == first
    assert archive.with_name(f"{archive.name}.sha256").exists()
    with tarfile.open(archive, "r:gz") as packaged:
        names = packaged.getnames()
    assert "quality-report.json" in names
    assert "quality-report.md" in names
    assert "manifest.json" in names
    assert "SHA256SUMS" in names
    store.close()


def test_manifest_document_mismatch_fails_packaging(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    manifest = build_manifest(output, store, "start", "end")
    documents = manifest["documents"]
    assert isinstance(documents, list)
    document = documents[0]
    assert isinstance(document, dict)
    document["sha256"] = "tampered"
    with pytest.raises(ValueError, match="manifest document hash mismatch"):
        write_release(output, manifest)
    store.close()


def test_duplicate_manifest_document_path_fails_packaging(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    manifest = build_manifest(output, store, "start", "end")
    documents = manifest["documents"]
    assert isinstance(documents, list)
    document = documents[0]
    assert isinstance(document, dict)
    documents.append(dict(document))
    with pytest.raises(ValueError, match="duplicate manifest document path"):
        write_release(output, manifest)
    store.close()
