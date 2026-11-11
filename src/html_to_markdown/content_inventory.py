"""Resumable private source capture, independent of publication admission.

SQLite commits each discovery event and captured article atomically. The exported
inventory and exception queue are receipts, never a publishable corpus.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from .adapters import ADAPTERS
from .adapters.community import (
    BASE,
    CATEGORIES,
    CATEGORY_PATHS,
    clean_html,
    first_post,
    topic_id,
    validate_topic,
)
from .content_policy import load_content_policy
from .curation import json_bytes
from .enrichment.analysis import sha
from .errors import AuthenticationWallError
from .fetcher import Fetcher
from .models import DiscoveredPage, FetchResult, utc_now
from .render import render_html, serialize_document
from .urls import stable_path, validate_source_url


def atomic_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        prefix=path.name + ".", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class InventoryFetcher(Fetcher):
    def __init__(self, output: Path, database: sqlite3.Connection, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.output = output
        self.database = database
        self.community_client.discovery_recorder = self.record_discovery
        self._http.event_hooks["response"].append(self.preserve_response)

    async def preserve_response(self, response: httpx.Response) -> None:
        await response.aread()
        body = response.content
        atomic_private(self.output / "responses" / (sha(body) + ".body"), body)
        receipt = {
            "url": str(response.url),
            "status": response.status_code,
            "headers": dict(response.headers),
            "body_sha256": sha(body),
            "captured_at": utc_now(),
        }
        atomic_private(
            self.output / "responses" / (sha(json_bytes(receipt)) + ".json"), json_bytes(receipt)
        )

    @asynccontextmanager
    async def browser_page(self) -> AsyncIterator[Any]:
        async with super().browser_page() as page:
            try:
                yield page
            finally:
                try:
                    html = (await page.content()).encode()
                    atomic_private(self.output / "browser-evidence" / (sha(html) + ".html"), html)
                    receipt = {"url": page.url, "html_sha256": sha(html), "captured_at": utc_now()}
                    atomic_private(
                        self.output / "browser-evidence" / (sha(json_bytes(receipt)) + ".json"),
                        json_bytes(receipt),
                    )
                except Exception as failure:  # pylint: disable=broad-exception-caught
                    receipt = {"error_class": type(failure).__name__, "captured_at": utc_now()}
                    atomic_private(
                        self.output / "browser-evidence" / (sha(json_bytes(receipt)) + ".json"),
                        json_bytes(receipt),
                    )

    def record_discovery(
        self, *, source: str, origin: str, urls: list[str], complete: bool = False, **details: Any
    ) -> None:
        event = {
            "source": source,
            "origin": origin,
            "urls": urls,
            "complete": complete,
            "captured_at": utc_now(),
            **details,
        }
        with self.database:
            self.database.execute("INSERT INTO events(data) VALUES (?)", (json.dumps(event),))
            for url in urls:
                canonical = validate_source_url(source, url)
                existing = self.database.execute(
                    "SELECT data FROM documents WHERE url=?", (canonical,)
                ).fetchone()
                item = (
                    json.loads(existing[0])
                    if existing
                    else {
                        "identity": source + ":" + stable_path(source, canonical).as_posix(),
                        "sourceId": source,
                        "url": canonical,
                        "origins": [],
                        "discovered_at": utc_now(),
                        "disposition": "queued",
                        "reason": "capture_pending",
                    }
                )
                item["origins"] = sorted(set(item["origins"]) | {origin})
                self.database.execute(
                    "INSERT OR REPLACE INTO documents VALUES (?,?)", (canonical, json.dumps(item))
                )

    def record_navigation(self, response: FetchResult, routes: list[str]) -> None:
        data = response.html.encode()
        relative = "discovery/" + sha(data) + ".html"
        atomic_private(self.output / relative, data)
        self.record_discovery(
            source="docs-cloud-f5-com",
            origin=response.final_url,
            urls=routes,
            complete=True,
            source_html_path=relative,
            source_html_sha256=sha(data),
        )


def _documents(database: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        json.loads(row[0]) for row in database.execute("SELECT data FROM documents ORDER BY url")
    ]


def verify_capture(output: Path, item: dict[str, Any]) -> None:
    """Never resume or review bytes that have drifted from their capture receipt."""
    pairs = [
        (item.get("source_html_path"), item.get("source_html_sha256")),
        (item.get("article_path"), item.get("article_sha256")),
    ]
    pairs.extend((media.get("path"), media.get("sha256")) for media in item.get("media", []))
    for relative, digest in pairs:
        if relative is None or digest is None:
            continue
        path = Path(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or any((output / parent).is_symlink() for parent in [path, *path.parents])
        ):
            raise ValueError("unsafe capture receipt path")
        if not (output / path).is_file() or sha((output / path).read_bytes()) != digest:
            raise ValueError("capture receipt digest mismatch")


async def _community_discovery(fetcher: InventoryFetcher) -> list[DiscoveredPage]:
    client = fetcher.community_client
    listings: dict[int, Any] = {}
    for identifier, slug in CATEGORY_PATHS.items():
        listings.update(
            await client.listing(f"{BASE}/c/articles/{slug}/{identifier}.json", identifier)
        )
    tagged = await client.listing(f"{BASE}/tag/f5-distributed-cloud.json")
    tag_articles = {key for key, value in tagged.items() if value["category_id"] in CATEGORIES}
    if tag_articles - listings.keys():
        raise ValueError("tag articles absent from exhausted category enumeration")
    sitemap = await client.sitemap_ids()
    fetcher.record_discovery(
        source="community-f5-com",
        origin=f"{BASE}/sitemap.xml",
        urls=[f"{BASE}/t/{key}" for key in sorted(listings.keys() & sitemap)],
        complete=True,
        sitemap_topic_count=len(sitemap),
        listing_missing_sitemap=sorted(listings.keys() - sitemap),
    )
    return [
        DiscoveredPage(source_id="community-f5-com", url=f"{BASE}/t/{key}")
        for key in sorted(listings)
    ]


async def _capture(fetcher: InventoryFetcher, item: dict[str, Any]) -> dict[str, Any]:
    adapter = ADAPTERS[item["sourceId"]]()
    result = dict(item)
    try:
        fetched = await fetcher.fetch(adapter, item["url"])
        data = fetched.html.encode()
        relative = "captures/" + sha(data) + ".html"
        atomic_private(fetcher.output / relative, data)
        result.update(
            source_html_path=relative,
            source_html_sha256=sha(data),
            captured_at=utc_now(),
            http_status=fetched.status_code,
            final_url=fetched.final_url,
        )
        if adapter.source_id == "community-f5-com":
            value = validate_topic(json.loads(fetched.html), topic_id(item["url"]))
            html = clean_html(value)
            metadata = adapter.normalize_metadata(fetched, html)
            result["first_post_updated_at"] = first_post(value)["updated_at"]
        else:
            extracted = adapter.extract(fetched)
            html, metadata = extracted.html, extracted.metadata
        rendered = render_html(html, fetched.final_url)
        metadata.url = item["url"]
        metadata.canonical_url = fetched.final_url
        article = (
            Path("articles/content")
            / adapter.source_id
            / stable_path(adapter.source_id, item["url"])
            / "index.md"
        )
        media: list[dict[str, Any]] = []
        result["media"] = media
        for asset in rendered.assets:
            data, media_type = await fetcher.fetch_asset(asset.url)
            digest = sha(data)
            asset_path = article.parent / "assets" / digest
            atomic_private(fetcher.output / asset_path, data)
            rendered.body = rendered.body.replace(asset.placeholder, "assets/" + digest)
            media.append(
                {
                    "url": asset.url,
                    "sha256": digest,
                    "path": asset_path.as_posix(),
                    "media_type": media_type,
                }
            )
        data = serialize_document(metadata, rendered.body).encode()
        extracted_path = Path("extractions") / sha(data) / "index.md"
        atomic_private(fetcher.output / extracted_path, data)
        excluded = load_content_policy().excludes(item["url"])
        result.update(
            article_path=extracted_path.as_posix(),
            published_path=article.as_posix(),
            article_sha256=sha(data),
            metadata=metadata.model_dump(by_alias=True),
            source_published_at=metadata.publication_date,
            source_modified_at=metadata.modification_date,
            disposition="policy_excluded" if excluded else "queued",
            reason="exact_retired_route_policy" if excluded else "admission_review_required",
            capture_complete=True,
        )
    except Exception as failure:
        error = {"type": type(failure).__name__, "message": str(failure), "at": utc_now()}
        atomic_private(fetcher.output / "errors" / (sha(item["url"]) + ".json"), json_bytes(error))
        result.update(
            disposition="access_queue"
            if isinstance(failure, AuthenticationWallError)
            else "capture_error",
            reason=type(failure).__name__,
            capture_complete=False,
        )
    return result


def _report(
    output: Path, database: sqlite3.Connection, binding: dict[str, Any], phases: dict[str, Any]
) -> dict[str, Any]:
    documents = _documents(database)
    report = {
        "schema_version": 1,
        "binding": binding,
        "ended_at": utc_now(),
        "sources": phases,
        "documents": documents,
        "completeness": {
            "enumeration": all(
                value.get("enumeration_complete", False) for value in phases.values()
            ),
            "capture": all(item.get("capture_complete", False) for item in documents),
            "review": bool(documents)
            and all(item["disposition"] == "policy_excluded" for item in documents),
            "publication": False,
        },
    }
    report["counts"] = {
        state: sum(item["disposition"] == state for item in documents)
        for state in ("queued", "policy_excluded", "capture_error", "access_queue")
    }
    atomic_private(output / "inventory.json", json_bytes(report))
    # No article title, content, identifying value, OCR, or transport diagnostics.
    safe = [
        {
            key: item[key]
            for key in ("identity", "sourceId", "url", "disposition", "reason", "discovered_at")
        }
        for item in documents
        if item["disposition"] != "policy_excluded"
    ]
    atomic_private(
        output / "review-queue.json",
        json_bytes(
            {
                "schema_version": 1,
                "documents": safe,
                "completeness": report["completeness"],
                "counts": report["counts"],
            }
        ),
    )
    return report


async def collect_content(
    output: Path,
    *,
    source: str = "all",
    previous_manifest: Path | None = None,
    resume: bool = False,
    full: bool = False,
    concurrency: int = 4,
    retries: int = 3,
    timeout: float = 30,
) -> dict[str, Any]:
    if source != "all" and source not in ADAPTERS:
        raise ValueError("unknown source")
    if not 1 <= concurrency <= 20:
        raise ValueError("concurrency must be between 1 and 20")
    previous_bytes = previous_manifest.read_bytes() if previous_manifest else b"{}"
    previous = json.loads(previous_bytes)
    binding = {"source": source, "full": full, "previous_manifest_sha256": sha(previous_bytes)}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding_path = output / "capture-binding.json"
    if binding_path.exists():
        if not resume:
            raise ValueError("existing capture requires --resume or a fresh output directory")
        saved = json.loads(binding_path.read_bytes())
        if saved["request"] != binding:
            raise ValueError("resume binding does not match captured source/baseline/full mode")
    else:
        if resume:
            raise ValueError("resume requires an existing capture binding")
        atomic_private(binding_path, json_bytes({"request": binding, "started_at": utc_now()}))
    database = sqlite3.connect(output / "inventory.sqlite")
    database.executescript(
        "CREATE TABLE IF NOT EXISTS documents(url TEXT PRIMARY KEY,data TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,data TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS phases(source TEXT PRIMARY KEY,data TEXT NOT NULL);"
    )
    phases = {
        row[0]: json.loads(row[1]) for row in database.execute("SELECT source,data FROM phases")
    }
    sources = sorted(ADAPTERS) if source == "all" else [source]
    for name in sources:
        phases.setdefault(name, {"enumeration_complete": False, "full": full})
    try:
        async with InventoryFetcher(output, database, timeout=timeout, retries=retries) as fetcher:
            for item in _documents(database):
                verify_capture(output, item)
            for name in sources:
                urls = [
                    item["url"]
                    for item in previous.get("documents", [])
                    if item["sourceId"] == name
                ]
                urls += [
                    item["url"]
                    for item in previous.get(
                        "aliases", previous.get("enrichment", {}).get("aliases", [])
                    )
                    if item["path"].startswith("content/" + name + "/")
                ]
                fetcher.record_discovery(
                    source=name, origin="previous_publication", urls=urls, complete=True
                )
                if phases[name]["enumeration_complete"]:
                    continue
                try:
                    adapter = ADAPTERS[name]()
                    pages = (
                        await _community_discovery(fetcher)
                        if name == "community-f5-com"
                        else await adapter.discover(fetcher)
                    )
                    fetcher.record_discovery(
                        source=name,
                        origin="authoritative_enumeration",
                        urls=[page.url for page in pages],
                        complete=True,
                    )
                    phases[name] = {
                        "enumeration_complete": True,
                        "full": full,
                        "completed_at": utc_now(),
                    }
                except Exception as failure:
                    phases[name] = {
                        "enumeration_complete": False,
                        "full": full,
                        "error_class": type(failure).__name__,
                    }
                    atomic_private(
                        output / "errors" / (name + "-discovery.json"),
                        json_bytes({"error": str(failure)}),
                    )
                with database:
                    database.execute(
                        "INSERT OR REPLACE INTO phases VALUES (?,?)",
                        (name, json.dumps(phases[name])),
                    )
                _report(output, database, binding, phases)
            semaphore = asyncio.Semaphore(concurrency)

            async def capture(item: dict[str, Any]) -> None:
                if item.get("capture_complete"):
                    return
                async with semaphore:
                    result = await _capture(fetcher, item)
                    with database:
                        database.execute(
                            "INSERT OR REPLACE INTO documents VALUES (?,?)",
                            (item["url"], json.dumps(result)),
                        )

            await asyncio.gather(*(capture(item) for item in _documents(database)))
    finally:
        report = _report(output, database, binding, phases)
        database.close()
    return report


def repair_captured_extraction(output: Path) -> dict[str, Any]:
    """Re-render captured source bytes offline, preserving previous extracted files."""
    database = sqlite3.connect(output / "inventory.sqlite")
    repaired = []
    try:
        for item in _documents(database):
            if not item.get("capture_complete"):
                continue
            verify_capture(output, item)
            adapter = ADAPTERS[item["sourceId"]]()
            fetched = FetchResult(
                url=item["url"],
                final_url=item["final_url"],
                status_code=item["http_status"],
                html=(output / item["source_html_path"]).read_text(),
            )
            if item["sourceId"] == "community-f5-com":
                value = validate_topic(json.loads(fetched.html), topic_id(item["url"]))
                html = clean_html(value)
                metadata = adapter.normalize_metadata(fetched, html)
            else:
                extracted = adapter.extract(fetched)
                html, metadata = extracted.html, extracted.metadata
            metadata.url = item["url"]
            metadata.canonical_url = item["final_url"]
            rendered = render_html(html, item["final_url"])
            media = {entry["url"]: entry for entry in item["media"]}
            for asset in rendered.assets:
                if asset.url not in media:
                    raise ValueError("repaired extraction introduced an uncaptured asset")
                rendered.body = rendered.body.replace(
                    asset.placeholder, "assets/" + Path(media[asset.url]["path"]).name
                )
            data = serialize_document(metadata, rendered.body).encode()
            if sha(data) == item["article_sha256"]:
                continue
            old = output / item["article_path"]
            atomic_private(
                output / "extraction-history" / (item["article_sha256"] + ".md"), old.read_bytes()
            )
            extracted_path = Path("extractions") / sha(data) / "index.md"
            atomic_private(output / extracted_path, data)
            item.update(
                published_path=item.get("published_path", item["article_path"]),
                article_path=extracted_path.as_posix(),
                article_sha256=sha(data),
                metadata=metadata.model_dump(by_alias=True),
                extraction_repaired=True,
            )
            with database:
                database.execute(
                    "UPDATE documents SET data=? WHERE url=?", (json.dumps(item), item["url"])
                )
            repaired.append(item["identity"])
        inventory = json.loads((output / "inventory.json").read_bytes())
        inventory["documents"] = _documents(database)
        inventory["extraction_repairs"] = repaired
        atomic_private(output / "inventory.json", json_bytes(inventory))
    finally:
        database.close()
    return {"repaired": repaired}
