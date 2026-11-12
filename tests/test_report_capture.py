"""Official report redirects enter capture without granting publication admission."""

# pylint: disable=protected-access

import httpx
import pytest

from html_to_markdown.errors import AllowlistError
from html_to_markdown.fetcher import Fetcher
from html_to_markdown.urls import stable_path, validate_source_url

REPORT = "https://www.f5.com/resources/reports/state-of-ai-application-strategy-report"
ORIGINAL = "https://www.f5.com/go/report/state-of-application-strategy-report-api-security"


def test_report_child_route_has_stable_capture_identity() -> None:
    assert validate_source_url("www-f5-com", REPORT) == REPORT
    assert str(stable_path("www-f5-com", REPORT)) == (
        "resources/reports/state-of-ai-application-strategy-report"
    )


@pytest.mark.parametrize(
    "url",
    [
        REPORT.replace("https:", "http:"),
        REPORT.replace("www.f5.com", "www.f5.com.evil.test"),
        REPORT.replace("www.f5.com", "www.f5.com:444"),
        REPORT + "?account=example",
        "https://www.f5.com/resources/reports/../private",
        "https://www.f5.com/resources/reports/%2e%2e/private",
        "https://www.f5.com/resources/reports-elsewhere/article",
        "https://www.f5.com/",
    ],
)
def test_report_capture_preserves_existing_url_restrictions(url: str) -> None:
    with pytest.raises(AllowlistError):
        validate_source_url("www-f5-com", url)


@pytest.mark.asyncio
async def test_official_report_redirect_retains_complete_response_and_origin() -> None:
    document = (
        "<main><h1>Application strategy</h1><p>" + "Complete original text. " * 100 + "</p></main>"
    )
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == ORIGINAL:
            return httpx.Response(302, headers={"location": REPORT}, request=request)
        return httpx.Response(200, text=document, request=request)

    async with Fetcher(retries=1) as fetcher:
        await fetcher._http.aclose()
        fetcher._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await fetcher._http_fetch("www-f5-com", ORIGINAL)
    assert result.url == ORIGINAL
    assert result.final_url == REPORT
    assert result.html == document
    assert calls == [ORIGINAL, REPORT]
