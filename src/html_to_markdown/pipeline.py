"""Shared asynchronous discovery-to-package pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from pathlib import Path
from sqlite3 import Row
from urllib.parse import urljoin, urlsplit

import structlog
from bs4 import BeautifulSoup

from .adapters import ADAPTERS
from .adapters.base import SourceAdapter
from .adapters.community import validate_community_publication
from .community_media import reviewed_media
from .content_policy import (
    load_content_policy,
    migrate_content,
    purge_excluded_state,
    record_exclusion,
)
from .curation import load_curation_policy
from .errors import (
    AllowlistError,
    AuthenticationWallError,
    NavigationOnlyError,
    NotFoundError,
)
from .fetcher import Fetcher
from .metadata import enrich_snapshot, normalize_source_date
from .models import DiscoveredPage, DocumentRecord, PageStatus, utc_now
from .package import build_manifest, write_release
from .quality import write_quality_reports
from .reconcile import include_inventory_urls, include_previous_urls, reconcile_previous
from .render import render_html, serialize_document
from .state import StateStore
from .urls import infer_source, stable_path, validate_source_url
from .validation import require_valid_document, validate_snapshot

log = structlog.get_logger()


class Pipeline:
    def __init__(
        self,
        output: Path,
        *,
        concurrency: int = 8,
        retries: int = 3,
        timeout: float = 30,
        headed: bool = False,
    ) -> None:
        if concurrency < 1 or concurrency > 20:
            raise ValueError("concurrency must be between 1 and 20")
        self.output = output
        self.concurrency = concurrency
        self.store = StateStore(output / "state.sqlite")
        self.fetcher = Fetcher(timeout=timeout, retries=retries, headed=headed)
        self.fetcher.community_inventory_dir = output / "community-inventory"

    async def __aenter__(self) -> Pipeline:
        await self.fetcher.__aenter__()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.fetcher.close()
        self.store.close()

    @staticmethod
    def adapters(source: str) -> list[SourceAdapter]:
        names = sorted(ADAPTERS) if source == "all" else [source]
        try:
            return [ADAPTERS[name]() for name in names]
        except KeyError as error:
            raise ValueError(f"unknown source: {source}") from error

    async def discover(self, source: str, url: str | None = None) -> int:
        adapters = self.adapters(source)
        if url:
            if len(adapters) != 1:
                raise ValueError("--url requires one explicit --source")
            page = DiscoveredPage(
                source_id=adapters[0].source_id,
                url=validate_source_url(adapters[0].source_id, url),
            )
            pages = [page]
        else:
            groups = await asyncio.gather(*(adapter.discover(self.fetcher) for adapter in adapters))
            pages = [page for group in groups for page in group]
        retained = []
        for page in pages:
            if load_content_policy().excludes(page.url):
                record_exclusion(self.store, page.url, "discovery")
            else:
                retained.append(page)
        pages = retained
        self.store.discover(pages)
        log.info(
            "discovery_complete",
            sources=[adapter.source_id for adapter in adapters],
            pages=len(pages),
        )
        return len(pages)

    async def scrape(self, source: str, *, force: bool = False) -> list[DocumentRecord]:
        adapters = {adapter.source_id: adapter for adapter in self.adapters(source)}
        purge_excluded_state(self.store)
        rows = self.store.pending(list(adapters), force=force)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def bounded(row: Row) -> DocumentRecord | None:
            async with semaphore:
                return await self._scrape_one(
                    adapters[row["source"]],
                    row["canonical_url"],
                    row["source_last_modified"],
                )

        results = await asyncio.gather(*(bounded(row) for row in rows))
        records = [result for result in results if result is not None]
        log.info("scrape_complete", attempted=len(rows), succeeded=len(records))
        return records

    # Asset and source metadata are committed with the document as one operation.
    # pylint: disable-next=too-many-locals,too-many-statements
    async def _scrape_one(
        self, adapter: SourceAdapter, url: str, source_last_modified: str | None = None
    ) -> DocumentRecord | None:
        self.store.mark_fetching(url)
        status: PageStatus
        failure: Exception
        try:
            fetched = await self.fetcher.fetch(adapter, url)
            extracted = adapter.extract(fetched)
            self.store.replace_candidate_links(
                url, self._candidate_links(extracted.candidate_links, fetched.final_url)
            )
            extracted.metadata.canonical_url = validate_source_url(
                adapter.source_id, fetched.final_url
            )
            if load_content_policy().excludes(extracted.metadata.canonical_url):
                record_exclusion(self.store, url, "canonical_redirect")
                with self.store.connection:
                    self.store.connection.execute("DELETE FROM pages WHERE canonical_url=?", (url,))
                return None
            retirement = load_curation_policy().vesctl
            if retirement is not None and retirement.original_match(
                extracted.metadata.model_dump(by_alias=True), extracted.html
            ):
                target = self.output / "content" / adapter.source_id
                target /= stable_path(adapter.source_id, url)
                (target / "index.md").unlink(missing_ok=True)
                record_exclusion(self.store, url, "whole_document_retirement")
                with self.store.connection:
                    self.store.connection.execute("DELETE FROM pages WHERE canonical_url=?", (url,))
                return None
            extracted.html = await self._resolve_redirects(
                adapter,
                fetched.final_url,
                extracted.html,
            )
            rendered = render_html(extracted.html, fetched.final_url)
            extracted.metadata.url = url
            if extracted.metadata.modification_date is None:
                extracted.metadata.last_updated = normalize_source_date(
                    source_last_modified or fetched.headers.get("last-modified")
                )
            page_dir = self.output / "content" / adapter.source_id
            page_dir /= stable_path(adapter.source_id, url)
            page_dir.mkdir(parents=True, exist_ok=True)
            asset_hashes: list[str] = []
            for asset in rendered.assets:
                try:
                    content, media_type = await self.fetcher.fetch_asset(asset.url)
                except AllowlistError:
                    if adapter.source_id == "community-f5-com":
                        raise
                    rendered.body = rendered.body.replace(asset.placeholder, asset.url)
                    continue
                if adapter.source_id == "community-f5-com":
                    content, media_type = self._reviewed_asset(
                        content,
                        media_type,
                        asset.url,
                        json.loads(fetched.headers.get("community-review", "{}")),
                    )
                digest = hashlib.sha256(content).hexdigest()
                suffix = self._asset_extension(asset.url, media_type)
                asset_dir = page_dir / "assets"
                asset_dir.mkdir(exist_ok=True)
                target = asset_dir / f"{digest}{suffix}"
                if not target.exists():
                    target.write_bytes(content)
                rendered.body = rendered.body.replace(asset.placeholder, f"assets/{target.name}")
                asset_hashes.append(digest)
            document = serialize_document(extracted.metadata, rendered.body)
            target = page_dir / "index.md"
            target.write_text(document, encoding="utf-8", newline="\n")
            require_valid_document(target)
            relative = target.relative_to(self.output).as_posix()
            self.store.mark(
                url,
                PageStatus.SUCCESS,
                output_path=relative,
                digest=extracted.metadata.content_hash,
            )
            log.info("page_success", source=adapter.source_id, url=url, path=relative)
            return DocumentRecord(
                source_id=adapter.source_id,
                url=url,
                output_path=target,
                content_hash=extracted.metadata.content_hash,
                asset_hashes=asset_hashes,
            )
        except NotFoundError as error:
            status = PageStatus.REMOVED_NOT_FOUND
            failure = error
        except NavigationOnlyError as error:
            status = PageStatus.REMOVED_NAVIGATION
            failure = error
        except AuthenticationWallError as error:
            status = PageStatus.AUTHENTICATION_WALL
            failure = error
        except Exception as error:  # pylint: disable=broad-exception-caught
            status = PageStatus.FAILED
            failure = error
        self.store.mark(url, status, error=failure)
        log.error(
            "page_failed",
            source=adapter.source_id,
            url=url,
            status=status,
            error_class=type(failure).__name__,
            error_message=str(failure),
        )
        return None

    @staticmethod
    def _candidate_links(hrefs: list[str], base_url: str) -> list[str]:
        candidates: set[str] = set()
        for href in hrefs:
            if not href.strip():
                continue
            candidate = urljoin(base_url, href)
            try:
                source_id = infer_source(candidate)
                if source_id is not None:
                    candidates.add(validate_source_url(source_id, candidate))
            except Exception:  # pylint: disable=broad-exception-caught  # nosec B112
                continue
        return sorted(candidates)

    async def _resolve_redirects(self, adapter: SourceAdapter, base_url: str, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        resolved: dict[str, str] = {}
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if not href:
                continue
            candidate = urljoin(base_url, href)
            try:
                canonical = validate_source_url(adapter.source_id, candidate)
            except Exception:  # pylint: disable=broad-exception-caught  # nosec B112
                continue
            if canonical in resolved:
                anchor["href"] = resolved[canonical]
                continue
            if len(resolved) >= 100:
                anchor["href"] = canonical
                continue
            if load_content_policy().excludes(canonical):
                anchor["href"] = canonical
                continue
            try:
                final_url = await self.fetcher.resolve_redirect(adapter.source_id, canonical)
                resolved[canonical] = (
                    canonical if load_content_policy().excludes(final_url) else final_url
                )
                anchor["href"] = resolved[canonical]
            except Exception:  # pylint: disable=broad-exception-caught
                resolved[canonical] = canonical
                anchor["href"] = canonical
        return str(soup)

    @staticmethod
    def _reviewed_asset(
        content: bytes, media_type: str, url: str, review: dict[str, object]
    ) -> tuple[bytes, str]:
        content, replacement_type = reviewed_media(content, url, review)
        return content, replacement_type or media_type

    @staticmethod
    def _asset_extension(url: str, media_type: str) -> str:
        media = media_type.split(";", 1)[0].strip().lower()
        suffix = mimetypes.guess_extension(media) or Path(urlsplit(url).path).suffix.lower()
        return suffix if re_safe_suffix(suffix) else ".bin"

    def validate(self) -> None:
        migrate_content(self.output, self.store)
        enrich_snapshot(self.output, self.store)
        validate_community_publication(self.output, self.store)
        validate_snapshot(self.output, self.store)

    def package(self, started_at: str, ended_at: str) -> Path:
        migrate_content(self.output, self.store)
        enrich_snapshot(self.output, self.store)
        validate_community_publication(self.output, self.store)
        if not (self.output / "quality-report.md").is_file():
            write_quality_reports(self.output)
        manifest = build_manifest(self.output, self.store, started_at, ended_at)
        return write_release(self.output, manifest)


def re_safe_suffix(suffix: str) -> bool:
    return bool(suffix and len(suffix) <= 10 and suffix.startswith(".") and suffix[1:].isalnum())


async def run_pipeline(
    *,
    source: str,
    output: Path,
    url: str | None = None,
    concurrency: int = 8,
    retries: int = 3,
    timeout: float = 30,
    headed: bool = False,
    force: bool = False,
    previous_manifest: Path | None = None,
    quality_reference: Path | None = None,
    benchmark: Path | None = None,
    inventory_only: bool = False,
    retain_previous: bool = False,
) -> Path:
    # pylint: disable=too-many-arguments
    started = utc_now()
    async with Pipeline(
        output,
        concurrency=concurrency,
        retries=retries,
        timeout=timeout,
        headed=headed,
    ) as pipeline:
        if inventory_only:
            if benchmark is None:
                raise ValueError("--inventory-only requires --benchmark")
            if url is not None:
                raise ValueError("--inventory-only cannot be combined with --url")
        else:
            await pipeline.discover(source, url)
        include_previous_urls(
            pipeline.store,
            previous_manifest,
            {adapter.source_id for adapter in pipeline.adapters(source)},
        )
        include_inventory_urls(
            pipeline.store,
            benchmark,
            {adapter.source_id for adapter in pipeline.adapters(source)},
        )
        await pipeline.scrape(source, force=force)
        reconcile_previous(
            output, pipeline.store, previous_manifest, retain_previous=retain_previous
        )
        migrate_content(output, pipeline.store)
        enrich_snapshot(output, pipeline.store)
        write_quality_reports(output, reference=quality_reference, benchmark=benchmark)
        pipeline.validate()
        return pipeline.package(started, utc_now())
