import json
from pathlib import Path

import pytest

from html_to_markdown.docs_inventory import inventory_report
from html_to_markdown.models import DiscoveredPage, PageMetadata, PageStatus
from html_to_markdown.render import serialize_document
from html_to_markdown.state import StateStore


def test_changed_source_date_and_interrupted_capture_are_pending(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    url = "https://docs.cloud.f5.com/docs-v2/ai-assistant/how-to/use-ai-assistant"
    store.discover(
        [DiscoveredPage(source_id="docs-cloud-f5-com", url=url, source_last_modified="2026-10-02")]
    )
    store.mark(url, PageStatus.SUCCESS)
    store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url)])
    assert store.rows()[0]["source_last_modified"] == "2026-10-02"
    assert not store.pending(["docs-cloud-f5-com"])
    store.discover(
        [DiscoveredPage(source_id="docs-cloud-f5-com", url=url, source_last_modified="2026-11-10")]
    )
    assert len(store.pending(["docs-cloud-f5-com"])) == 1
    store.mark_fetching(url)
    store.close()
    reopened = StateStore(tmp_path / "state.sqlite")
    assert len(reopened.pending(["docs-cloud-f5-com"])) == 1
    reopened.close()


def test_inventory_reports_new_changed_fallback_and_policy_excluded(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    urls = [
        "https://docs.cloud.f5.com/docs-v2/platform/" + x for x in ("new", "changed", "fallback")
    ]
    store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url) for url in urls])
    for url in urls:
        relative = "content/docs-cloud-f5-com/platform/" + url.rsplit("/", 1)[1] + "/index.md"
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        page = PageMetadata(
            sourceId="docs-cloud-f5-com",
            title="Article",
            slug="article",
            url=url,
            category="platform",
            modification_date="2026-11-10",
        )
        path.write_text(serialize_document(page, "# Article\n\nTechnical guidance.\n"))
        store.mark(url, PageStatus.SUCCESS, output_path=relative)
    previous = {
        "documents": [{"url": urls[1], "sourceId": "docs-cloud-f5-com", "file_sha256": "0" * 64}]
    }
    # An existing success without a fresh response must never count as fresh.
    responses = {url: {"source_html_sha256": "1" * 64} for url in urls[:2]}
    responses["https://docs.cloud.f5.com/docs-v2/retired"] = {"source_html_sha256": "2" * 64}
    report = inventory_report(
        tmp_path, previous, store.rows(), responses, started_at="2026-11-10T00:00:00Z"
    )
    assert report["counts"] == {
        "new": 1,
        "changed": 1,
        "unchanged": 0,
        "fallback": 1,
        "unavailable": 0,
        "policy_excluded": 1,
    }
    assert all(
        item.get("source_modified_at") == "2026-11-10"
        for item in report["documents"]
        if "path" in item
    )
    assert json.loads((tmp_path / "inventory.json").read_text()) == report
    failed = inventory_report(
        tmp_path, {}, store.rows(), {}, started_at="2026-11-10T00:00:00Z", error="interrupted"
    )
    assert not failed["completed"]
    assert failed["error"] == "interrupted"
    store.close()


def test_daily_inventory_is_separate_and_keeps_failure_evidence() -> None:
    text = (Path(__file__).parents[1] / ".github/workflows/inventory-docs.yml").read_text()
    assert "inventory-docs --output capture" in text
    assert "combine-content --baseline previous" in text
    assert "verify-refresh --output build" in text
    assert "if: always()" in text
    assert "publication-sha256" in text
    assert "verify-publication" in text
    assert 'cron: "23 4 * * *"' in text
    assert "releases/latest" not in text
    assert "run --source all" not in text


@pytest.mark.asyncio
async def test_collection_failure_still_records_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from html_to_markdown import docs_inventory

    async def broken_discovery(self: object, source: str) -> int:
        raise RuntimeError("navigation capture interrupted")

    monkeypatch.setattr(docs_inventory.Pipeline, "discover", broken_discovery)
    with pytest.raises(RuntimeError, match="interrupted"):
        await docs_inventory.collect_docs(tmp_path)
    report = json.loads((tmp_path / "inventory.json").read_text())
    assert not report["completed"]
    assert "interrupted" in report["error"]


@pytest.mark.asyncio
async def test_capture_keeps_current_html_and_rechecks_successful_articles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
    from html_to_markdown.docs_inventory import CapturingFetcher
    from html_to_markdown.fetcher import Fetcher
    from html_to_markdown.models import FetchResult

    calls = []

    async def response(self: object, source: str, url: str) -> FetchResult:
        calls.append(url)
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html="<main><h1>Article</h1><p>" + "Current article. " * 100 + "</p></main>",
        )

    monkeypatch.setattr(Fetcher, "_http_fetch", response)
    async with CapturingFetcher(tmp_path / "captures", timeout=30, retries=3) as fetcher:
        url = "https://docs.cloud.f5.com/docs-v2/platform/concepts/new"
        for _ in range(2):
            page = await fetcher.fetch(DocsCloudAdapter(), url)
        assert calls == [url, url]
        evidence = fetcher.responses[url]
        assert (tmp_path / evidence["source_html_path"]).read_text() == page.html
        fetcher.record_navigation(page, [url])
        assert json.loads((tmp_path / "navigation.json").read_text())["routes"] == [url]
