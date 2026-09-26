import json
from pathlib import Path

from html_to_markdown.models import DiscoveredPage, PageMetadata, PageStatus
from html_to_markdown.package import build_manifest, write_release
from html_to_markdown.reconcile import (
    include_inventory_urls,
    include_previous_urls,
    reconcile_previous,
)
from html_to_markdown.render import serialize_document
from html_to_markdown.state import StateStore

URL = "https://docs.cloud.f5.com/docs-v2/carried"
PATH = "content/docs-cloud-f5-com/carried/index.md"


def previous_snapshot(tmp_path: Path, *, freshness: str = "fresh", terminal: int = 0) -> Path:
    previous = tmp_path / "previous"
    target = previous / PATH
    target.parent.mkdir(parents=True)
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Carried",
        slug="carried",
        url=URL,
        category="guide",
        tags=[],
    )
    target.write_text(
        serialize_document(metadata, "# Carried\n\nLast known good body."), encoding="utf-8"
    )
    assets = target.parent / "assets"
    assets.mkdir()
    (assets / "image.png").write_bytes(b"png")
    manifest = previous / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "sourceId": "docs-cloud-f5-com",
                        "url": URL,
                        "path": PATH,
                        "sha256": metadata.content_hash,
                        "provenance": {
                            "freshness": freshness,
                            "last_success_at": "2026-09-01T00:00:00Z",
                            "consecutive_failure_count": 0,
                            "terminal_confirmation_count": terminal,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_transient_failure_carries_document_assets_and_provenance(tmp_path: Path) -> None:
    prior = previous_snapshot(tmp_path)
    output = tmp_path / "out"
    store = StateStore(output / "state.sqlite")
    include_previous_urls(store, prior, {"docs-cloud-f5-com"})
    store.mark(URL, PageStatus.FAILED, error=RuntimeError("timeout"))
    reconcile_previous(output, store, prior)
    row = store.rows()[0]
    assert row["status"] == PageStatus.CARRIED_FORWARD
    assert (output / PATH).exists()
    assert (output / Path(PATH).parent / "assets/image.png").read_bytes() == b"png"
    manifest = build_manifest(output, store, "start", "end")
    counts = manifest["counts"]
    documents = manifest["documents"]
    assert isinstance(counts, dict)
    assert isinstance(documents, list)
    document = documents[0]
    assert isinstance(document, dict)
    provenance = document["provenance"]
    assert isinstance(provenance, dict)
    assert counts["carried_forward"] == 1
    assert provenance["freshness"] == "carried_forward"
    assert provenance["current_failure"]["classification"] == "failed"
    assert provenance["consecutive_failure_count"] == 1
    assert provenance["last_success_at"] == "2026-09-01T00:00:00Z"
    store.close()


def test_reconciliation_is_idempotent_for_resumed_output(tmp_path: Path) -> None:
    prior = previous_snapshot(tmp_path)
    output = tmp_path / "out"
    store = StateStore(output / "state.sqlite")
    include_previous_urls(store, prior, {"docs-cloud-f5-com"})
    store.mark(URL, PageStatus.FAILED, error=RuntimeError("timeout"))
    reconcile_previous(output, store, prior)
    first = build_manifest(output, store, "start", "end")

    include_previous_urls(store, prior, {"docs-cloud-f5-com"})
    reconcile_previous(output, store, prior)
    second = build_manifest(output, store, "start", "end")

    assert second == first
    store.close()


def test_terminal_removal_requires_two_consecutive_crawls(tmp_path: Path) -> None:
    first_prior = previous_snapshot(tmp_path)
    first_output = tmp_path / "first"
    first_store = StateStore(first_output / "state.sqlite")
    include_previous_urls(first_store, first_prior, {"docs-cloud-f5-com"})
    first_store.mark(URL, PageStatus.REMOVED_NOT_FOUND, error=RuntimeError("404"))
    reconcile_previous(first_output, first_store, first_prior)
    assert first_store.rows()[0]["status"] == PageStatus.REMOVAL_CANDIDATE
    first_manifest = build_manifest(first_output, first_store, "start", "end")
    first_manifest_path = first_output / "manifest.json"
    first_manifest_path.write_text(json.dumps(first_manifest), encoding="utf-8")
    first_counts = first_manifest["counts"]
    assert isinstance(first_counts, dict)
    assert first_counts["removal_candidate"] == 1
    first_store.close()

    second_output = tmp_path / "second"
    second_store = StateStore(second_output / "state.sqlite")
    include_previous_urls(second_store, first_manifest_path, {"docs-cloud-f5-com"})
    second_store.mark(URL, PageStatus.REMOVED_NOT_FOUND, error=RuntimeError("404"))
    reconcile_previous(second_output, second_store, first_manifest_path)
    assert second_store.rows()[0]["status"] == PageStatus.CONFIRMED_REMOVAL
    second_manifest = build_manifest(second_output, second_store, "start", "end")
    second_counts = second_manifest["counts"]
    second_documents = second_manifest["documents"]
    assert isinstance(second_counts, dict)
    assert isinstance(second_documents, list)
    assert second_counts["confirmed_removal"] == 1
    assert not second_documents
    second_store.close()


def test_first_baseline_failure_is_unavailable_but_packages(tmp_path: Path) -> None:
    output = tmp_path / "out"
    store = StateStore(output / "state.sqlite")
    url = "https://my.f5.com/manage/s/article/K000000001"
    store.discover([DiscoveredPage(source_id="my-f5-com", url=url)])
    store.mark(url, PageStatus.AUTHENTICATION_WALL, error=RuntimeError("login"))
    reconcile_previous(output, store, None)
    assert store.rows()[0]["status"] == PageStatus.UNAVAILABLE
    manifest = build_manifest(output, store, "start", "end")
    counts = manifest["counts"]
    assert isinstance(counts, dict)
    assert counts["unavailable"] == 1
    (output / "quality-report.json").write_text("{}\n", encoding="utf-8")
    (output / "quality-report.md").write_text("# Quality\n", encoding="utf-8")
    assert write_release(output, manifest).exists()
    store.close()


def test_inventory_urls_are_seeded_for_explicit_reconciliation(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    urls = [URL, "https://my.f5.com/manage/s/article/K000123456"]
    inventory.write_text(json.dumps({"version": 1, "urls": urls}), encoding="utf-8")
    store = StateStore(tmp_path / "state.sqlite")
    include_inventory_urls(store, inventory, {"docs-cloud-f5-com", "my-f5-com"})
    assert [row["canonical_url"] for row in store.rows()] == sorted(urls)
    store.close()
