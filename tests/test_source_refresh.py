import json
from pathlib import Path

import pytest

from html_to_markdown.curation import corpus_digest
from html_to_markdown.metadata import enrich_snapshot
from html_to_markdown.models import DiscoveredPage, PageMetadata, PageStatus
from html_to_markdown.package import (
    build_manifest,
    sha256_file,
    write_publication_receipt,
    write_release,
)
from html_to_markdown.refresh import combine_docs, verify_refresh
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.state import StateStore


def prepare(tmp_path: Path) -> tuple[Path, Path, str]:
    baseline = tmp_path / "baseline"
    store = StateStore(baseline / "state.sqlite")
    for source, slug, url in [
        ("docs-cloud-f5-com", "a", "https://docs.cloud.f5.com/docs-v2/a"),
        ("community-f5-com", "t/42", "https://community.f5.com/t/42"),
    ]:
        path = f"content/{source}/{slug}/index.md"
        target = baseline / path
        target.parent.mkdir(parents=True)
        page = PageMetadata(
            sourceId=source,
            title="Article",
            slug=slug,
            url=url,
            category="guide",
            modification_date="2026-10-02",
            canonical_url=url,
        )
        target.write_text(serialize_document(page, "# Article\n\nUse 8 vCPUs and 32 GB RAM.\n"))
        store.discover([DiscoveredPage(source_id=source, url=url)])
        store.mark(url, PageStatus.SUCCESS, output_path=path, digest=page.content_hash)
    enrich_snapshot(baseline, store)
    write_release(
        baseline, build_manifest(baseline, store, "2026-10-02T00:00:00Z", "2026-10-02T00:01:00Z")
    )
    write_publication_receipt(
        baseline,
        tag="content-20261002T000100Z",
        source_commit="a" * 40,
        created_at="2026-10-02T00:00:00Z",
        published_at="2026-10-02T00:01:00Z",
    )
    store.close()
    current = tmp_path / "current"
    for slug in ("a", "new"):
        page = PageMetadata(
            sourceId="docs-cloud-f5-com",
            title="Article",
            slug=slug,
            url="https://docs.cloud.f5.com/docs-v2/" + slug,
            category="guide",
            modification_date="2026-11-10",
            canonical_url="https://docs.cloud.f5.com/docs-v2/" + slug,
        )
        target = current / f"content/docs-cloud-f5-com/{slug}/index.md"
        target.parent.mkdir(parents=True)
        target.write_text(serialize_document(page, "# Article\n\nUse 16 vCPUs and 64 GB RAM.\n"))
    (current / "inventory.json").write_text(
        json.dumps(
            {
                "completed": True,
                "input_corpus_sha256": corpus_digest(current),
                "started_at": "2026-11-10T00:00:00Z",
                "ended_at": "2026-11-10T00:01:00Z",
                "documents": [
                    {
                        "path": path.relative_to(current).as_posix(),
                        "file_sha256": sha256_file(path),
                        "status": "success",
                        "url": split_document(path.read_text())[0]["url"],
                        "captured_at": "2026-11-10T00:00:30Z",
                    }
                    for path in sorted(current.glob("content/*/**/index.md"))
                ],
            }
        )
    )
    return baseline, current, sha256_file(baseline / "publication.json")


def test_fresh_docs_publish_while_verified_community_is_preserved(tmp_path: Path) -> None:
    baseline, current, pin = prepare(tmp_path)
    output = tmp_path / "combined"
    state = combine_docs(baseline, current, output, pin)
    assert len(state["documents"]) == 3
    community = "content/community-f5-com/t/42/index.md"
    assert (output / community).read_bytes() == (baseline / community).read_bytes()
    article = output / "content/docs-cloud-f5-com/a/index.md"
    assert "16 vCPUs and 64 GB RAM" in article.read_text()
    assert split_document(article.read_text())[0]["modification_date"] == "2026-11-10"
    verify_refresh(output)
    from html_to_markdown.adapters.community import validate_community_publication

    with_store = StateStore(output / "state.sqlite")
    validate_community_publication(output, with_store)
    with_store.close()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["schema_version"] == 2
    assert manifest["counts"]["carried_forward"] == 1
    assert manifest["refresh"]["baseline_receipt_sha256"] == pin
    archive_pin = sha256_file(output / "html-to-markdown-content.tar.gz")
    write_release(output, manifest)
    assert sha256_file(output / "html-to-markdown-content.tar.gz") == archive_pin
    article.write_text(article.read_text().replace("16 vCPUs", "32 vCPUs"))
    with pytest.raises(ValueError, match="body or description"):
        verify_refresh(output)


def test_changed_source_cannot_replay_cached_refresh_decisions(tmp_path: Path) -> None:
    baseline, current, pin = prepare(tmp_path)
    output = tmp_path / "combined"
    combine_docs(baseline, current, output, pin)
    source = output / ".refresh/current/content/docs-cloud-f5-com/a/index.md"
    source.write_text(source.read_text().replace("16 vCPUs", "99 vCPUs"))
    with pytest.raises(ValueError, match="captured inputs changed"):
        verify_refresh(output)


def test_rejected_fresh_article_keeps_whole_verified_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from html_to_markdown import refresh

    baseline, current, pin = prepare(tmp_path)
    migrate = refresh.migrate_content

    def guarded(output: Path) -> object:
        if (output / "content/docs-cloud-f5-com/a/index.md").exists():
            raise ValueError("stale source-span decision")
        return migrate(output)

    monkeypatch.setattr(refresh, "migrate_content", guarded)
    output = tmp_path / "combined"
    state = combine_docs(baseline, current, output, pin)
    relative = "content/docs-cloud-f5-com/a/index.md"
    assert (output / relative).read_bytes() == (baseline / relative).read_bytes()
    assert state["documents"][relative]["kind"] == "published"
    assert any(
        item["disposition"] == "fallback" and "stale source-span" in item["reason"]
        for item in state["findings"]
    )


def test_wrong_receipt_prevents_source_refresh(tmp_path: Path) -> None:
    baseline, current, _ = prepare(tmp_path)
    with pytest.raises(ValueError, match="receipt digest"):
        combine_docs(baseline, current, tmp_path / "combined", "0" * 64)


def test_candidate_cannot_introduce_unsupported_prose(tmp_path: Path) -> None:
    baseline, current, pin = prepare(tmp_path)
    output = tmp_path / "combined"
    state = combine_docs(baseline, current, output, pin)
    relative = "content/docs-cloud-f5-com/a/index.md"
    candidate = (
        output / ".refresh/candidates" / state["documents"][relative]["candidate"] / relative
    )
    candidate.write_text(candidate.read_text().replace("16 vCPUs", "99 vCPUs"))
    with pytest.raises(ValueError, match="deterministic source curation"):
        verify_refresh(output)


def test_refresh_community_gate_rejects_new_articles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from html_to_markdown import refresh
    from html_to_markdown.adapters.community import validate_community_publication
    from html_to_markdown.errors import PublicationBlockedError

    (tmp_path / "refresh-state.json").write_text("{}")
    store = StateStore(tmp_path / "state.sqlite")
    store.discover(
        [DiscoveredPage(source_id="community-f5-com", url="https://community.f5.com/t/42")]
    )
    monkeypatch.setattr(
        refresh,
        "verify_refresh",
        lambda output: {
            "documents": {"content/community-f5-com/t/42/index.md": {"kind": "current"}}
        },
    )
    with pytest.raises(PublicationBlockedError, match="unreviewed Community"):
        validate_community_publication(tmp_path, store)
    store.close()


def test_failed_capture_cannot_publish_a_leftover_raw_article(tmp_path: Path) -> None:
    baseline, current, pin = prepare(tmp_path)
    report = json.loads((current / "inventory.json").read_text())
    report["documents"][0]["status"] = "failed"
    (current / "inventory.json").write_text(json.dumps(report))
    output = tmp_path / "combined"
    state = combine_docs(baseline, current, output, pin)
    relative = "content/docs-cloud-f5-com/a/index.md"
    assert state["documents"][relative]["kind"] == "published"
    assert (output / relative).read_bytes() == (baseline / relative).read_bytes()


def test_identical_pinned_inputs_produce_identical_snapshots(tmp_path: Path) -> None:
    baseline, current, pin = prepare(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    combine_docs(baseline, current, first, pin)
    combine_docs(baseline, current, second, pin)
    for name in (
        "manifest.json",
        "curation-audit.json",
        "refresh-state.json",
        "SHA256SUMS",
        "html-to-markdown-content.tar.gz",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
