"""Shared HTTP-first and Playwright retrieval."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.async_api import Browser, Playwright, async_playwright

from .adapters.base import SourceAdapter
from .adapters.community import BASE, CommunityClient, topic_id
from .errors import AuthenticationWallError, NotFoundError, ScrapeError
from .models import FetchResult
from .urls import validate_asset_url, validate_source_url

USER_AGENT_HEADERS = {"User-Agent": "f5-html-to-markdown/1.0"}


class Fetcher:
    """Reuse one Chromium process while isolating each page in its own context."""

    def __init__(self, *, timeout: float = 30, retries: int = 3, headed: bool = False) -> None:
        self.timeout = timeout
        self.retries = retries
        self.headed = headed
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self.community_client = CommunityClient(self._http, retries=max(1, retries))
        self.community_inventory_dir = Path("build/community-inventory")
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        await self._http.aclose()

    async def _ensure_browser(self) -> Browser:
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=not self.headed)
        return self._browser

    @asynccontextmanager
    async def browser_page(self) -> AsyncIterator[Any]:
        browser = await self._ensure_browser()
        context = await browser.new_context(service_workers="block")
        page = await context.new_page()
        page.set_default_timeout(self.timeout * 1000)
        try:
            yield page
        finally:
            await context.close()

    async def fetch(
        self,
        adapter: SourceAdapter,
        url: str,
        *,
        browser: bool = False,
    ) -> FetchResult:
        canonical = validate_source_url(adapter.source_id, url)
        if adapter.source_id == "community-f5-com":
            identifier = topic_id(canonical)
            value = await self.community_client.topic(identifier)
            reviews_path = self.community_inventory_dir / "reviews.json"
            baseline = Path(__file__).with_name("adapters") / "community_reviews.json"
            reviews = json.loads(baseline.read_text()) if baseline.exists() else {}
            if reviews_path.exists():
                reviews.update(json.loads(reviews_path.read_text()))
            return FetchResult(
                url=canonical,
                final_url=f"{BASE}/t/{identifier}",
                status_code=200,
                html=json.dumps(value),
                headers={"community-review": json.dumps(reviews.get(str(identifier), {}))},
            )
        if adapter.browser_only or browser:
            return await self._browser_fetch(adapter, canonical)
        result = await self._http_fetch(adapter.source_id, canonical)
        if adapter.needs_browser(result.html):
            return await self._browser_fetch(adapter, canonical)
        adapter.classify_page(result)
        return result

    async def _http_fetch(self, source_id: str, url: str) -> FetchResult:
        current = url
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                for _ in range(10):
                    response = await self._http.get(current, headers=USER_AGENT_HEADERS)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ScrapeError(f"redirect without Location: {current}")
                        current = validate_source_url(source_id, urljoin(current, location))
                        continue
                    if response.status_code == 404:
                        raise NotFoundError(f"HTTP 404: {current}")
                    if response.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"HTTP {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    return FetchResult(
                        url=url,
                        final_url=validate_source_url(source_id, str(response.url)),
                        status_code=response.status_code,
                        html=response.text,
                        headers={key.lower(): value for key, value in response.headers.items()},
                    )
                raise ScrapeError(f"too many redirects: {url}")
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as error:
                last_error = error
                if attempt + 1 < self.retries:
                    await asyncio.sleep(2**attempt)
        message = f"HTTP retrieval failed after {self.retries} attempts: {url}"
        raise ScrapeError(message) from last_error

    async def _browser_fetch(self, adapter: SourceAdapter, url: str) -> FetchResult:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self.browser_page() as page:
                    response = await page.goto(url, wait_until="domcontentloaded")
                    status = response.status if response else 200
                    if status == 404:
                        raise NotFoundError(f"HTTP 404: {url}")
                    final_url = validate_source_url(adapter.source_id, page.url)
                    readiness = adapter.readiness_script()
                    if readiness:
                        await page.wait_for_function(readiness)
                    html = await adapter.rendered_html(page)
                    result = FetchResult(
                        url=url,
                        final_url=final_url,
                        status_code=status,
                        html=html,
                        headers=dict(await response.all_headers()) if response else {},
                    )
                    adapter.classify_page(result)
                    return result
            except (AuthenticationWallError, NotFoundError, ValueError):
                raise
            except Exception as error:  # pylint: disable=broad-exception-caught
                # Playwright exposes several transient subclasses.
                last_error = error
                if attempt + 1 < self.retries:
                    await asyncio.sleep(2**attempt)
        message = f"browser retrieval failed after {self.retries} attempts: {url}"
        raise ScrapeError(message) from last_error

    async def fetch_asset(self, url: str) -> tuple[bytes, str]:
        canonical = validate_asset_url(url)
        if urlsplit(canonical).hostname in {"community.f5.com", "d20hrnpixdzcsd.cloudfront.net"}:
            response = await self.community_client.request(canonical, asset=True)
            return response.content, response.headers.get(
                "content-type", "application/octet-stream"
            )
        for _ in range(10):
            response = await self._http.get(canonical, headers=USER_AGENT_HEADERS)
            if response.status_code in {301, 302, 303, 307, 308}:
                canonical = validate_asset_url(
                    urljoin(canonical, response.headers.get("location", ""))
                )
                continue
            response.raise_for_status()
            return response.content, response.headers.get(
                "content-type", "application/octet-stream"
            )
        raise ScrapeError("asset redirect limit exceeded")

    async def resolve_redirect(self, source_id: str, url: str) -> str:
        """Resolve a source link without downloading each destination body."""
        current = validate_source_url(source_id, url)
        if source_id == "community-f5-com":
            return current
        for _ in range(10):
            response = await self._http.head(current, headers=USER_AGENT_HEADERS)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ScrapeError(f"redirect without Location: {current}")
                current = validate_source_url(source_id, urljoin(current, location))
                continue
            if response.status_code == 405:
                return (await self._http_fetch(source_id, current)).final_url
            if response.status_code >= 400:
                return current
            return validate_source_url(source_id, str(response.url))
        raise ScrapeError(f"too many redirects: {url}")
