"""Shared asynchronous discovery-to-package pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import structlog
from bs4 import BeautifulSoup

from .adapters import ADAPTERS
from .adapters.base import SourceAdapter
from .errors import AuthenticationWallError, NavigationOnlyError, NotFoundError
from .fetcher import Fetcher
from .models import DiscoveredPage, DocumentRecord, PageStatus, utc_now
from .package import build_manifest, write_release
from .render import render_html, serialize_document
from .state import StateStore
from .urls import stable_path, validate_source_url
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
        self.store.discover(pages)
        log.info(
            "discovery_complete",
            sources=[adapter.source_id for adapter in adapters],
            pages=len(pages),
        )
        return len(pages)

    async def scrape(self, source: str, *, force: bool = False) -> list[DocumentRecord]:
        adapters = {adapter.source_id: adapter for adapter in self.adapters(source)}
        rows = self.store.pending(list(adapters), force=force)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def bounded(row: object) -> DocumentRecord | None:
            async with semaphore:
                return await self._scrape_one(adapters[row["source"]], row["canonical_url"])  # type: ignore[index]

        results = await asyncio.gather(*(bounded(row) for row in rows))
        records = [result for result in results if result is not None]
        log.info("scrape_complete", attempted=len(rows), succeeded=len(records))
        return records

    async def _scrape_one(self, adapter: SourceAdapter, url: str) -> DocumentRecord | None:
        self.store.mark_fetching(url)
        status: PageStatus
        failure: Exception
        try:
            fetched = await self.fetcher.fetch(adapter, url)
            extracted = adapter.extract(fetched)
            extracted.html = await self._resolve_redirects(
                adapter,
                fetched.final_url,
                extracted.html,
            )
            rendered = render_html(extracted.html, fetched.final_url)
            relative_path = stable_path(adapter.source_id, fetched.final_url)
            page_dir = self.output / "content" / adapter.source_id / relative_path
            page_dir.mkdir(parents=True, exist_ok=True)
            asset_hashes: list[str] = []
            for asset in rendered.assets:
                content, media_type = await self.fetcher.fetch_asset(asset.url)
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
            try:
                final_url = await self.fetcher.resolve_redirect(adapter.source_id, canonical)
                resolved[canonical] = final_url
                anchor["href"] = final_url
            except Exception:  # pylint: disable=broad-exception-caught
                resolved[canonical] = canonical
                anchor["href"] = canonical
        return str(soup)

    @staticmethod
    def _asset_extension(url: str, media_type: str) -> str:
        media = media_type.split(";", 1)[0].strip().lower()
        suffix = mimetypes.guess_extension(media) or Path(urlsplit(url).path).suffix.lower()
        return suffix if re_safe_suffix(suffix) else ".bin"

    def validate(
        self,
        previous_manifest: Path | None = None,
        acknowledge_page_drop: bool = False,
    ) -> None:
        validate_snapshot(
            self.output,
            self.store,
            previous_manifest=previous_manifest,
            acknowledge_page_drop=acknowledge_page_drop,
        )

    def package(self, started_at: str, ended_at: str) -> Path:
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
    acknowledge_page_drop: bool = False,
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
        await pipeline.discover(source, url)
        await pipeline.scrape(source, force=force)
        pipeline.validate(previous_manifest, acknowledge_page_drop)
        return pipeline.package(started, utc_now())
