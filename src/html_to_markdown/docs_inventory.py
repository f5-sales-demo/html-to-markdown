"""Fresh official documentation collection, independent of other-source reviews."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .curation import corpus_digest
from .fetcher import Fetcher
from .models import DiscoveredPage, FetchResult, PageStatus, utc_now
from .pipeline import Pipeline
from .reconcile import include_previous_urls
from .render import split_document

SOURCE = "docs-cloud-f5-com"


class CapturingFetcher(Fetcher):
    """Preserve each current response even if extraction or curation rejects it."""

    def __init__(self, directory: Path, *, timeout: float, retries: int) -> None:
        super().__init__(timeout=timeout, retries=retries)
        self.directory = directory
        self.responses: dict[str, dict[str, Any]] = {}
        self.active: set[str] = set()

    def record_navigation(self, response: FetchResult, routes: list[str]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        data = response.html.encode()
        (self.directory / "navigation.html").write_bytes(data)
        (self.directory.parent / "navigation.json").write_text(
            json.dumps(
                {
                    "url": response.final_url,
                    "captured_at": utc_now(),
                    "source_html_sha256": hashlib.sha256(data).hexdigest(),
                    "routes": routes,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    async def fetch(self, adapter: Any, url: str, *, browser: bool = False) -> FetchResult:
        self.active.add(url)
        try:
            result = await super().fetch(adapter, url, browser=browser)
            self.record_response(url, result)
            return result
        finally:
            self.active.discard(url)

    async def _http_fetch(self, source_id: str, url: str) -> FetchResult:
        result = await super()._http_fetch(source_id, url)
        if url in self.active:
            self.record_response(url, result)
        return result

    def record_response(self, url: str, result: FetchResult) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        data = result.html.encode("utf-8")
        name = hashlib.sha256(url.encode()).hexdigest() + ".html"
        (self.directory / name).write_bytes(data)
        self.responses[url] = {
            "source_html_sha256": hashlib.sha256(data).hexdigest(),
            "source_html_path": "captures/" + name,
            "captured_at": utc_now(),
            "http_status": result.status_code,
            "final_url": result.final_url,
        }


def inventory_report(
    output: Path,
    previous: dict[str, Any],
    rows: list[Any],
    responses: dict[str, dict[str, Any]],
    *,
    started_at: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Compare retained content without labeling a fallback as newly fetched."""
    prior = {d["url"]: d for d in previous.get("documents", []) if d["sourceId"] == SOURCE}
    origins: dict[str, list[str]] = {}
    database = output / "state.sqlite"
    if database.exists():
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='content_exclusions'"
            ).fetchone():
                for url, origin in connection.execute(
                    "SELECT url,origin FROM content_exclusions ORDER BY url,origin"
                ):
                    origins.setdefault(url, []).append(origin)
        finally:
            connection.close()
    items: list[dict[str, Any]] = []
    present: set[str] = set()
    for row in rows:
        url = str(row["canonical_url"])
        present.add(url)
        item: dict[str, Any] = {
            "url": url,
            "status": row["status"],
            "last_success_at": row["last_success_at"],
            "error_class": row["error_class"],
            "error": row["error_message"],
            **responses.get(url, {}),
        }
        path = output / row["output_path"] if row["output_path"] else None
        if path is not None and path.is_file():
            data = path.read_bytes()
            metadata, _ = split_document(data.decode())
            digest = hashlib.sha256(data).hexdigest()
            item.update(
                {
                    "path": row["output_path"],
                    "file_sha256": digest,
                    "body_sha256": metadata["content_hash"],
                    "source_modified_at": metadata.get("modification_date"),
                }
            )
            if row["status"] == PageStatus.SUCCESS and url in responses:
                item["change"] = (
                    "new"
                    if url not in prior
                    else ("unchanged" if digest == prior[url]["file_sha256"] else "changed")
                )
            else:
                item["change"] = "fallback"
        else:
            item["change"] = "unavailable"
        items.append(item)
    # Curation deliberately removes state for excluded articles. Captures keep
    # those decisions visible rather than silently shrinking the route inventory.
    for url in sorted(set(responses) - present):
        items.append(
            {
                "url": url,
                "status": "policy_excluded",
                "change": "policy_excluded",
                "origins": origins.get(url, []),
                "needs_review": "unreviewed_authored_legacy_input" in origins.get(url, []),
                **responses[url],
            }
        )
    exclusions = output / "discovery-exclusions.json"
    if exclusions.exists():
        items.extend(json.loads(exclusions.read_text()))
    items.sort(key=lambda item: item["url"])
    counts = {
        key: sum(item["change"] == key for item in items)
        for key in ("new", "changed", "unchanged", "fallback", "unavailable", "policy_excluded")
    }
    result = {
        "schema_version": 1,
        "source": SOURCE,
        "input_corpus_sha256": corpus_digest(output),
        "started_at": started_at,
        "ended_at": utc_now(),
        "completed": error is None,
        "fresh_complete": error is None
        and all(
            item["change"] not in {"fallback", "unavailable"}
            or item["status"] == PageStatus.REMOVED_NAVIGATION
            for item in items
        ),
        "error": error,
        "previous_snapshot_ended_at": previous.get("ended_at"),
        "counts": counts,
        "review_required": [item["url"] for item in items if item.get("needs_review")],
        "documents": items,
    }
    (output / "inventory.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Official documentation inventory",
        "",
        f"Completed: {result['completed']}",
        f"Started: {started_at}; ended: {result['ended_at']}",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in counts.items())
    if error:
        lines.extend(["", "Collection error: " + error])
    (output / "inventory.md").write_text("\n".join(lines) + "\n")
    return result


async def collect_docs(
    output: Path,
    *,
    previous_manifest: Path | None = None,
    concurrency: int = 4,
    retries: int = 3,
    timeout: float = 30,
) -> dict[str, Any]:
    """Fetch every known/current route on every pass; never reuse stale successes."""
    previous = json.loads(previous_manifest.read_text()) if previous_manifest else {}
    started = utc_now()
    output.mkdir(parents=True, exist_ok=True)
    for name in (
        "inventory.json",
        "inventory.md",
        "html-to-markdown-content.tar.gz",
        "html-to-markdown-content.tar.gz.sha256",
        "navigation.json",
        "discovery-exclusions.json",
    ):
        (output / name).unlink(missing_ok=True)
    async with Pipeline(
        output, concurrency=concurrency, retries=retries, timeout=timeout
    ) as pipeline:
        await pipeline.fetcher.close()
        capture = CapturingFetcher(output / "captures", timeout=timeout, retries=retries)
        pipeline.fetcher = capture
        try:
            await pipeline.discover(SOURCE)
            include_previous_urls(pipeline.store, previous_manifest, {SOURCE})
            pipeline.store.discover(
                [
                    DiscoveredPage(source_id=SOURCE, url=alias["url"])
                    for alias in previous.get(
                        "aliases", previous.get("enrichment", {}).get("aliases", [])
                    )
                    if alias["path"].startswith("content/" + SOURCE + "/")
                ]
            )
            await pipeline.scrape(SOURCE, force=True)
            # Captured inputs are review evidence. Per-article curation and
            # last-verified fallbacks belong to the separate publication stage.
        except Exception as failure:
            inventory_report(
                output,
                previous,
                pipeline.store.rows([SOURCE]),
                capture.responses,
                started_at=started,
                error=f"{type(failure).__name__}: {failure}",
            )
            raise
        return inventory_report(
            output, previous, pipeline.store.rows([SOURCE]), capture.responses, started_at=started
        )
