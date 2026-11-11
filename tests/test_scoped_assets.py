"""Public source media routes never grant unrelated endpoints or redirect hosts."""

import httpx
import pytest

from html_to_markdown.errors import AllowlistError
from html_to_markdown.fetcher import Fetcher
from html_to_markdown.urls import validate_asset_url

SLACK = (
    "https://a.slack-edge.com/production-standard-emoji-assets/14.0/apple-medium/2699-fe0f@2x.png"
)
ENZOIC = "https://www.enzoic.com/wp-content/uploads/Credential-Stuffing-vs-Brute-Force-Attacks.png"


@pytest.mark.parametrize("url", [SLACK, ENZOIC])
def test_verified_public_route_is_eligible_for_capture_only(url):
    assert validate_asset_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        ENZOIC + "?download=1",
        ENZOIC + "#fragment",
        ENZOIC.replace("https://", "http://"),
        ENZOIC.replace("www.enzoic.com", "demo@example.com@www.enzoic.com"),
        ENZOIC.replace("www.enzoic.com", "www.enzoic.com:443"),
        ENZOIC.replace("wp-content/uploads", "private"),
        ENZOIC.replace("Credential-Stuffing", "%43redential-Stuffing"),
        ENZOIC.replace("wp-content/uploads", "wp-content//uploads"),
        ENZOIC.replace("wp-content/uploads", "wp-content/uploads/../uploads"),
        ENZOIC.replace(".png", ".php"),
        SLACK.replace("14.0", "15.0"),
        SLACK.replace("apple-medium", "other"),
        SLACK.replace("a.slack-edge.com", "a.slack-edge.com.example.invalid"),
        "https://f5.lightning.force.com/content/session?url=example",
        "https://gitlab.com/-/project/15708862/uploads/example/image.png",
    ],
)
def test_other_paths_encodings_authorities_and_parameters_stay_rejected(url):
    with pytest.raises(AllowlistError):
        validate_asset_url(url)


@pytest.mark.asyncio
async def test_scoped_asset_redirects_still_validate_each_destination():
    def respond(request):
        return httpx.Response(302, headers={"Location": "https://unrelated.invalid/image.png"})

    async with Fetcher() as fetcher:
        # Test the real client redirect boundary with an isolated HTTP transport.
        # pylint: disable=protected-access
        await fetcher._http.aclose()
        fetcher._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with pytest.raises(AllowlistError):
            await fetcher.fetch_asset(SLACK)
