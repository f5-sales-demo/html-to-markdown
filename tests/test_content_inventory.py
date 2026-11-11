"""Private capture and public queue contracts, including resumed partial discovery."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from html_to_markdown import content_inventory
from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.errors import AuthenticationWallError
from html_to_markdown.fetcher import Fetcher
from html_to_markdown.models import DiscoveredPage, FetchResult

SOURCE = "docs-cloud-f5-com"
URL = "https://docs.cloud.f5.com/docs-v2/platform/how-to/current"


@pytest.mark.asyncio
async def test_pending_review_never_aborts_capture_and_resume_verifies_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    async def discover(self: object, fetcher: object):
        return [DiscoveredPage(source_id=SOURCE, url=URL)]

    async def fetch(self: object, adapter: object, url: str):
        calls.append(url)
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html="<main><h1>Current</h1><h2>Resolution</h2><p>Use a 30 second timeout.</p></main>",
        )

    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    report = await content_inventory.collect_content(tmp_path, source=SOURCE)
    assert report["completeness"]["enumeration"]
    assert report["completeness"]["capture"]
    assert not report["completeness"]["review"]
    assert not report["completeness"]["publication"]
    item = report["documents"][0]
    assert item["disposition"] == "queued"
    assert item["source_html_sha256"] and item["article_sha256"]
    assert not (tmp_path / "content").exists()
    assert not (tmp_path / "manifest.json").exists()
    resumed = await content_inventory.collect_content(tmp_path, source=SOURCE, resume=True)
    assert calls == [URL]
    assert resumed["documents"] == report["documents"]
    (tmp_path / item["article_path"]).write_text("tampered")
    with pytest.raises(ValueError, match="digest"):
        await content_inventory.collect_content(tmp_path, source=SOURCE, resume=True)


@pytest.mark.asyncio
async def test_partial_discovery_is_preserved_and_known_urls_still_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def discover(self: object, fetcher: content_inventory.InventoryFetcher):
        fetcher.record_discovery(source=SOURCE, origin="navigation", urls=[URL], complete=False)
        raise RuntimeError("interrupted before final navigation")

    async def fetch(self: object, adapter: object, url: str):
        raise AuthenticationWallError("identifying diagnostic must remain private")

    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    report = await content_inventory.collect_content(tmp_path, source=SOURCE)
    assert not report["completeness"]["enumeration"]
    assert report["documents"][0]["disposition"] == "access_queue"
    queue = json.loads((tmp_path / "review-queue.json").read_text())
    assert "identifying diagnostic" not in json.dumps(queue)
    assert queue["documents"][0]["url"] == URL
    assert (tmp_path / "inventory.json").is_file()


@pytest.mark.asyncio
async def test_previous_publication_and_aliases_are_captured_even_if_not_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def discover(self: object, fetcher: object):
        return []

    async def fetch(self: object, adapter: object, url: str):
        raise AuthenticationWallError("access required")

    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    previous = tmp_path / "previous.json"
    previous.write_text(
        json.dumps(
            {
                "documents": [{"sourceId": SOURCE, "url": URL}],
                "aliases": [
                    {"path": f"content/{SOURCE}/platform/alias/index.md", "url": URL + "-alias"}
                ],
            }
        )
    )
    report = await content_inventory.collect_content(
        tmp_path / "capture", source=SOURCE, previous_manifest=previous
    )
    assert len(report["documents"]) == 2
    assert all("previous_publication" in item["origins"] for item in report["documents"])
    with pytest.raises(ValueError, match="binding"):
        await content_inventory.collect_content(tmp_path / "capture", source=SOURCE, resume=True)


@pytest.mark.asyncio
async def test_http_access_failure_retains_response_bytes_privately(tmp_path: Path) -> None:

    database = sqlite3.connect(":memory:")
    async with content_inventory.InventoryFetcher(tmp_path, database) as fetcher:
        response = httpx.Response(
            401, content=b"private authentication error evidence", request=httpx.Request("GET", URL)
        )
        await fetcher.preserve_response(response)
    bodies = list((tmp_path / "responses").glob("*.body"))
    assert len(bodies) == 1 and bodies[0].read_bytes() == response.content
    assert bodies[0].stat().st_mode & 0o077 == 0
    assert json.loads(next((tmp_path / "responses").glob("*.json")).read_bytes())["status"] == 401


@pytest.mark.asyncio
async def test_extraction_repair_keeps_old_export_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def discover(self: object, fetcher: object):
        return [DiscoveredPage(source_id=SOURCE, url=URL)]

    async def fetch(self: object, adapter: object, url: str):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html="<main><h1>Current</h1><h2>Resolution</h2><p>Use a 30 second timeout.</p></main>",
        )

    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    report = await content_inventory.collect_content(tmp_path, source=SOURCE)
    old = report["documents"][0]
    old_data = (tmp_path / old["article_path"]).read_bytes()
    render = content_inventory.render_html

    def changed_render(html: str, url: str):
        result = render(html, url)
        result.body += "\nRecovered complete expected results.\n"
        return result

    monkeypatch.setattr(content_inventory, "render_html", changed_render)
    repaired = content_inventory.repair_captured_extraction(tmp_path)
    assert repaired["repaired"] == [old["identity"]]
    fresh = json.loads((tmp_path / "inventory.json").read_bytes())["documents"][0]
    assert fresh["article_path"] != old["article_path"]
    assert fresh["published_path"] == old["published_path"]
    assert (tmp_path / old["article_path"]).read_bytes() == old_data
    content_inventory.verify_capture(tmp_path, old)
    content_inventory.verify_capture(tmp_path, fresh)


def test_concurrent_private_checkpoints_and_symlinked_capture_parent(tmp_path: Path) -> None:

    path = tmp_path / "checkpoint.json"
    values = [json.dumps({"worker": index, "payload": "x" * 1000}).encode() for index in range(24)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: content_inventory.atomic_private(path, value), values))
    assert path.read_bytes() in values
    assert path.stat().st_mode & 0o077 == 0
    assert list(tmp_path.iterdir()) == [path]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.html").write_bytes(b"source")
    (tmp_path / "captures").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        content_inventory.verify_capture(
            tmp_path,
            {"source_html_path": "captures/source.html", "source_html_sha256": sha(b"source")},
        )
