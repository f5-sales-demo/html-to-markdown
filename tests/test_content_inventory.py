"""Private capture and public queue contracts, including resumed partial discovery."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from html_to_markdown import content_inventory
from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
from html_to_markdown.content_inventory import _immutable_route_excluded
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
    inventory_bytes = (tmp_path / "inventory.json").read_bytes()
    repeated = content_inventory.repair_captured_extraction(tmp_path)
    assert isinstance(repeated["repaired"], list)
    assert not repeated["repaired"]
    assert (tmp_path / "inventory.json").read_bytes() == inventory_bytes


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


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content", "media", "metadata"])
async def test_old_content_bound_exclusion_queues_current_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async def discover(self: object, fetcher: object):
        return [DiscoveredPage(source_id=SOURCE, url=URL)]

    async def fetch(self: object, adapter: object, url: str):
        media = (
            '<img src="https://docs.cloud.f5.com/current.png" alt="Current diagram">'
            if change == "media"
            else ""
        )
        title = "Updated current procedure" if change == "metadata" else "Current procedure"
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html=f"<main><h1>{title}</h1><p>Use current networking on port 8443.</p>{media}</main>",
        )

    async def asset(self: object, url: str):
        return b"new captured media bytes", "image/png"

    # The old URL-level answer includes a content-bound reviewed omission;
    # no immutable route exclusion applies to this current capture.
    policy = SimpleNamespace(raw={"exclusions": []}, excludes=lambda url: True)
    monkeypatch.setattr(content_inventory, "load_content_policy", lambda: policy)
    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    monkeypatch.setattr(Fetcher, "fetch_asset", asset)
    report = await content_inventory.collect_content(tmp_path, source=SOURCE)
    item = report["documents"][0]
    assert item["capture_complete"]
    assert item["disposition"] == "queued"
    assert item["reason"] == "admission_review_required"
    assert not report["completeness"]["review"]
    content_inventory.verify_capture(tmp_path, item)


@pytest.mark.asyncio
async def test_resume_reconciles_old_exclusion_without_recapture_and_preserves_record(
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
            html="<main><h1>Current</h1><p>Preserve the current route policy.</p></main>",
        )

    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    first = await content_inventory.collect_content(tmp_path, source=SOURCE)
    old = {
        **first["documents"][0],
        "disposition": "policy_excluded",
        "reason": "exact_retired_route_policy",
    }
    with sqlite3.connect(tmp_path / "inventory.sqlite") as database:
        database.execute("UPDATE documents SET data=? WHERE url=?", (json.dumps(old), URL))
    monkeypatch.setattr(
        content_inventory,
        "load_content_policy",
        lambda: SimpleNamespace(raw={"exclusions": []}, excludes=lambda url: True),
    )
    resumed = await content_inventory.collect_content(tmp_path, source=SOURCE, resume=True)
    current = resumed["documents"][0]
    assert calls == [URL]
    assert current["disposition"] == "queued"
    assert current["article_sha256"] == old["article_sha256"]
    history = [
        json.loads(path.read_bytes()) for path in (tmp_path / "inventory-history").glob("*.json")
    ]
    assert old in history
    original_bytes = json.dumps(old).encode()
    assert (
        tmp_path / "inventory-history" / (sha(original_bytes) + ".json")
    ).read_bytes() == original_bytes
    content_inventory.verify_capture(tmp_path, old)
    content_inventory.verify_capture(tmp_path, current)
    resumed_again = await content_inventory.collect_content(tmp_path, source=SOURCE, resume=True)
    assert resumed_again["documents"] == resumed["documents"]
    assert calls == [URL]
    assert len(list((tmp_path / "inventory-history").glob("*.json"))) == len(history)


@pytest.mark.asyncio
async def test_declared_immutable_route_remains_policy_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def discover(self: object, fetcher: object):
        return [DiscoveredPage(source_id=SOURCE, url=URL)]

    async def fetch(self: object, adapter: object, url: str):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html="<main><h1>Retired</h1><p>Captured source remains private.</p></main>",
        )

    monkeypatch.setattr(
        content_inventory,
        "load_content_policy",
        lambda: SimpleNamespace(raw={"exclusions": [URL]}, excludes=lambda url: True),
    )
    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    report = await content_inventory.collect_content(tmp_path, source=SOURCE)
    assert report["documents"][0]["disposition"] == "policy_excluded"


@pytest.mark.asyncio
async def test_immutable_canonical_redirect_is_excluded_without_losing_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = URL + "-retired"

    async def discover(self: object, fetcher: object):
        return [DiscoveredPage(source_id=SOURCE, url=URL)]

    async def fetch(self: object, adapter: object, url: str):
        return FetchResult(
            url=url,
            final_url=canonical,
            status_code=200,
            html="<main><h1>Retired</h1><p>Preserve captured evidence.</p></main>",
        )

    monkeypatch.setattr(
        content_inventory,
        "load_content_policy",
        lambda: SimpleNamespace(
            raw={"exclusions": [canonical]}, excludes=lambda url: url == canonical
        ),
    )
    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    report = await content_inventory.collect_content(tmp_path, source=SOURCE)
    item = report["documents"][0]
    assert item["url"] == URL and item["final_url"] == canonical
    assert item["disposition"] == "policy_excluded"
    resumed = await content_inventory.collect_content(tmp_path, source=SOURCE, resume=True)
    assert resumed["documents"] == report["documents"]


def test_content_bound_community_omission_is_not_an_immutable_route() -> None:
    assert not _immutable_route_excluded("https://community.f5.com/t/66432")
    assert _immutable_route_excluded("https://docs.cloud.f5.com/docs-v2/api/endpoint")
