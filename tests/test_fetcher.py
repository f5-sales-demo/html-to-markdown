"""Fetcher tests."""

# pylint: disable=protected-access

from contextlib import asynccontextmanager

import httpx
import pytest

from html_to_markdown.adapters.base import SourceAdapter
from html_to_markdown.errors import (
    AllowlistError,
    AuthenticationWallError,
    NotFoundError,
    ScrapeError,
)
from html_to_markdown.fetcher import Fetcher
from html_to_markdown.models import DiscoveredPage, ExtractedPage, FetchResult, PageMetadata


class Adapter(SourceAdapter):
    source_id = "docs-cloud-f5-com"
    root_url = "https://docs.cloud.f5.com/docs-v2"

    async def discover(self, fetcher: Fetcher) -> list[DiscoveredPage]:
        return []

    def readiness_script(self) -> str | None:
        return "() => true"

    def classify_page(self, page: FetchResult) -> None:
        if "bad" in page.html:
            raise ValueError("bad")

    async def rendered_html(self, playwright_page: object) -> str:
        return "<main>rendered content long enough</main>"

    def extract(self, page: FetchResult) -> ExtractedPage:
        raise NotImplementedError

    def normalize_metadata(self, page: FetchResult, html: str) -> PageMetadata:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_http_redirect_success_and_asset() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/start"):
            return httpx.Response(302, headers={"location": "/docs-v2/end"})
        return httpx.Response(200, text="x" * 600, headers={"content-type": "image/png"})

    fetcher = Fetcher(retries=1)
    await fetcher._http.aclose()
    fetcher._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetcher.fetch(Adapter(), "https://docs.cloud.f5.com/docs-v2/start")
    assert result.final_url.endswith("/end")
    content, media = await fetcher.fetch_asset("https://docs.cloud.f5.com/docs-v2/end")
    assert content == b"x" * 600
    assert media == "image/png"
    assert len(calls) == 3
    await fetcher.close()


@pytest.mark.asyncio
async def test_http_errors_retry_and_redirect_escape() -> None:
    attempts = 0

    def server_error(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    fetcher = Fetcher(retries=2)
    await fetcher._http.aclose()
    fetcher._http = httpx.AsyncClient(transport=httpx.MockTransport(server_error))
    with pytest.raises(ScrapeError, match="after 2 attempts"):
        await fetcher._http_fetch("docs-cloud-f5-com", "https://docs.cloud.f5.com/docs-v2/a")
    assert attempts == 2
    await fetcher.close()

    def redirect_escape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.test/a"}, request=request)

    fetcher = Fetcher(retries=1)
    await fetcher._http.aclose()
    fetcher._http = httpx.AsyncClient(transport=httpx.MockTransport(redirect_escape))
    with pytest.raises(AllowlistError):
        await fetcher._http_fetch("docs-cloud-f5-com", "https://docs.cloud.f5.com/docs-v2/a")
    await fetcher.close()


@pytest.mark.asyncio
async def test_hard_404() -> None:
    fetcher = Fetcher(retries=1)
    await fetcher._http.aclose()
    fetcher._http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, request=request))
    )
    with pytest.raises(NotFoundError):
        await fetcher._http_fetch("docs-cloud-f5-com", "https://docs.cloud.f5.com/docs-v2/a")
    await fetcher.close()


class BrowserResponse:
    status = 200

    async def all_headers(self) -> dict[str, str]:
        return {"x-test": "yes"}


class BrowserPage:
    url = "https://docs.cloud.f5.com/docs-v2/a"
    waited = False

    async def goto(self, url: str, wait_until: str) -> BrowserResponse:
        self.url = url
        return BrowserResponse()

    async def wait_for_function(self, script: str) -> None:
        self.waited = True


@pytest.mark.asyncio
async def test_browser_fetch_uses_readiness_and_rendered_html() -> None:
    page = BrowserPage()
    fetcher = Fetcher(retries=1)

    @asynccontextmanager
    async def browser_page():
        yield page

    fetcher.browser_page = browser_page  # type: ignore[method-assign]
    result = await fetcher.fetch(Adapter(), page.url, browser=True)
    assert page.waited
    assert "rendered content" in result.html
    await fetcher.close()


@pytest.mark.asyncio
async def test_browser_fetch_preserves_authentication_wall() -> None:
    class AuthenticationAdapter(Adapter):
        def classify_page(self, page: FetchResult) -> None:
            raise AuthenticationWallError(f"authentication wall: {page.final_url}")

    page = BrowserPage()
    fetcher = Fetcher(retries=1)

    @asynccontextmanager
    async def browser_page():
        yield page

    fetcher.browser_page = browser_page  # type: ignore[method-assign]
    with pytest.raises(AuthenticationWallError, match="authentication wall"):
        await fetcher.fetch(AuthenticationAdapter(), page.url, browser=True)
    await fetcher.close()


@pytest.mark.asyncio
async def test_redirect_resolution_uses_head_and_rejects_escape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/a"):
            return httpx.Response(308, headers={"location": "/docs-v2/b"}, request=request)
        return httpx.Response(200, request=request)

    fetcher = Fetcher(retries=1)
    await fetcher._http.aclose()
    fetcher._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert (
        await fetcher.resolve_redirect("docs-cloud-f5-com", "https://docs.cloud.f5.com/docs-v2/a")
        == "https://docs.cloud.f5.com/docs-v2/b"
    )
    await fetcher.close()


@pytest.mark.asyncio
async def test_marketing_redirect_cannot_escape_exact_solution_or_product_prefix() -> None:
    from html_to_markdown.adapters.www_f5 import WwwF5Adapter

    start = "https://www.f5.com/solutions/web-app-and-api-protection"
    destinations = [
        "/solutions/web-app-and-api-protection/other",
        "/products/other",
        "https://example.com/",
    ]
    for destination in destinations:
        fetcher = Fetcher(retries=1)
        await fetcher._http.aclose()
        fetcher._http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, location=destination: httpx.Response(
                    302, headers={"location": location}, request=request
                )
            )
        )
        with pytest.raises(AllowlistError):
            await fetcher.fetch(WwwF5Adapter(), start)
        with pytest.raises(AllowlistError):
            await fetcher.resolve_redirect("www-f5-com", start)
        await fetcher.close()
