"""Owned F5 redirects remain capture candidates until complete independent admission."""

# Fetcher transport internals provide deterministic redirect responses.
# pylint: disable=protected-access

import httpx
import pytest

from html_to_markdown.errors import AllowlistError
from html_to_markdown.fetcher import Fetcher
from html_to_markdown.urls import infer_source, stable_path, validate_source_url


@pytest.mark.parametrize(
    "path",
    [
        "/ai",
        "/resources",
        "/resources/reports",
        "/resources/articles/increase-agility-in-your-enterprise-environment",
        "/resources/solution-guides/identity-aware-proxy-and-zero-trust-solution-overview",
        "/go/ebook/enterprise-architecture-for-digital-business",
        "/go/report/forrester-tei-report-ecommerce-and-retail-fraud-prevention",
    ],
)
@pytest.mark.asyncio
async def test_solution_redirect_fetches_complete_current_owned_source(path):
    start = "https://www.f5.com/solutions/ai-delivery-and-security"
    calls = []
    article = "<main><h1>F5 AI</h1><p>Distributed Cloud application security.</p></main>"

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.startswith("/solutions/"):
            return httpx.Response(301, headers={"location": path})
        return httpx.Response(200, text=article)

    async with Fetcher(retries=1) as fetcher:
        await fetcher._http.aclose()
        fetcher._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await fetcher._http_fetch("www-f5-com", start)
        assert result.final_url == "https://www.f5.com" + path
        assert result.html == article
        assert infer_source(result.final_url) == "www-f5-com"
        assert stable_path("www-f5-com", result.final_url).as_posix() == path.strip("/")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://www.f5.com/ai",
        "https://www.f5.com.example.com/ai",
        "https://example.com/ai",
        "https://www.f5.com:8443/ai",
        "https://user@example.com@www.f5.com/ai",
        "https://www.f5.com/ai?token=example",
        "https://www.f5.com/resources/articles/%2e%2e/company/other",
        "https://www.f5.com/go/report/../company/other",
        "https://www.f5.com/company/other",
        "https://www.f5.com/products/other",
        "https://www.f5.com/go/unrelated",
        "https://www.f5.com/resources/unrelated",
        "https://www.f5.com/airandom",
    ],
)
def test_redirect_scope_never_grants_arbitrary_host_authority_or_route(url):
    with pytest.raises(AllowlistError):
        validate_source_url("www-f5-com", url)
