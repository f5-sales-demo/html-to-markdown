"""Marketing source discovery, scope, extraction, and stable output contracts."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from html_to_markdown.adapters.www_f5 import WwwF5Adapter
from html_to_markdown.errors import AllowlistError, NavigationOnlyError, NotFoundError, ScrapeError
from html_to_markdown.models import FetchResult
from html_to_markdown.render import render_html
from html_to_markdown.urls import (
    WWW_F5_SOLUTION_URLS,
    infer_source,
    stable_path,
    validate_asset_url,
    validate_source_url,
)

ROOT = Path(__file__).parent
BASE = "https://www.f5.com"
PRODUCT = f"{BASE}/products/distributed-cloud-services"
SITEMAP = "http://www.sitemaps.org/schemas/sitemap/0.9"


class SitemapFetcher:
    def __init__(self, content: bytes, status: int = 200, url: str = WwwF5Adapter.sitemap_url):
        self._http = self
        self.content = content
        self.status = status
        self.url = url

    async def get(self, url: str, *, headers: dict[str, str]):
        assert url == WwwF5Adapter.sitemap_url
        assert headers["User-Agent"]
        return SimpleNamespace(status_code=self.status, url=self.url, content=self.content)


def page(kind: str) -> FetchResult:
    path = (
        "products/distributed-cloud-services/api-security"
        if kind == "product"
        else "solutions/use-cases/multi-cloud-networking"
    )
    url = f"{BASE}/{path}"
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        html=(ROOT / "fixtures" / f"www_f5_{kind}.html").read_text(),
    )


@pytest.mark.asyncio
async def test_discovery_is_scoped_ordered_and_captures_lastmod() -> None:
    xml = f"""<urlset xmlns="{SITEMAP}">
      <url><loc>{PRODUCT}/z</loc><lastmod>2026-09-27T10:00:00Z</lastmod></url>
      <url><loc>{BASE}/products/other</loc></url>
      <url><loc>{PRODUCT}</loc><lastmod>2026-09-26</lastmod></url>
      <url><loc>{PRODUCT}/a</loc></url>
    </urlset>""".encode()
    pages = await WwwF5Adapter().discover(SitemapFetcher(xml))
    urls = [item.url for item in pages]
    assert urls == sorted({PRODUCT, f"{PRODUCT}/a", f"{PRODUCT}/z"} | WWW_F5_SOLUTION_URLS)
    assert len(pages) == 6
    assert next(item for item in pages if item.url == f"{PRODUCT}/z").source_last_modified == (
        "2026-09-27T10:00:00Z"
    )
    assert next(item for item in pages if item.url == PRODUCT).source_last_modified == "2026-09-26"
    assert all(item.source_id == "www-f5-com" for item in pages)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,status", [(b"<bad/>", 200), (b"<urlset", 200), (b"", 503)])
async def test_discovery_fails_closed(content: bytes, status: int) -> None:
    with pytest.raises(ScrapeError):
        await WwwF5Adapter().discover(SitemapFetcher(content, status))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        f'<urlset xmlns="{SITEMAP}"><url><loc>{BASE}/products/other</loc></url></urlset>',
        f'<urlset xmlns="{SITEMAP}"><url><loc></loc></url></urlset>',
        f'<urlset xmlns="{SITEMAP}"><bad/></urlset>',
        f'<urlset xmlns="{SITEMAP}"><url><loc>{PRODUCT}/child</loc></url></urlset>',
        f'<urlset xmlns="{SITEMAP}"><url><loc>{PRODUCT}/%2e%2e/other</loc></url></urlset>',
    ],
)
async def test_discovery_rejects_bad_entries_or_missing_product_root(body: str) -> None:
    with pytest.raises((ScrapeError, AllowlistError)):
        await WwwF5Adapter().discover(SitemapFetcher(body.encode()))


@pytest.mark.asyncio
async def test_discovery_rejects_redirected_sitemap() -> None:
    with pytest.raises(ScrapeError):
        await WwwF5Adapter().discover(
            SitemapFetcher(b"", url="https://www.f5.com/other-sitemap.xml")
        )


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/products/other",
        f"{PRODUCT}-other",
        f"{BASE}/solutions/other",
        f"{BASE}/solutions/web-app-and-api-protection/other",
        f"{PRODUCT}/a?campaign=x",
        f"{PRODUCT}/%2e%2e/other",
        "https://user@www.f5.com/products/distributed-cloud-services",
        "https://www.f5.com:443/products/distributed-cloud-services",
        "http://www.f5.com/products/distributed-cloud-services",
        "https://example.com/products/distributed-cloud-services",
    ],
)
def test_unrelated_marketing_urls_are_rejected(url: str) -> None:
    with pytest.raises(AllowlistError):
        validate_source_url("www-f5-com", url)
    assert infer_source(url) is None


def test_exact_solutions_infer_source_and_stable_paths_do_not_collide() -> None:
    urls = [PRODUCT, f"{PRODUCT}/api-security", *sorted(WWW_F5_SOLUTION_URLS)]
    paths = [str(stable_path("www-f5-com", url)) for url in urls]
    assert len(set(paths)) == len(urls)
    assert paths[0] == "products/distributed-cloud-services"
    assert paths[1] == "products/distributed-cloud-services/api-security"
    assert "solutions/web-app-and-api-protection" in paths
    assert all(infer_source(url) == "www-f5-com" for url in urls)
    assert validate_asset_url("https://cdn.studio.f5.com/images/api.svg")


@pytest.mark.parametrize("kind", ["product", "solution"])
def test_extraction_matches_golden_and_metadata(kind: str) -> None:
    result = WwwF5Adapter().extract(page(kind))
    rendered = render_html(result.html, result.metadata.url)
    assert rendered.body == (ROOT / "golden" / f"www-f5-{kind}.md").read_text()
    assert result.metadata.subcategory is None
    assert "f5-distributed-cloud" in result.metadata.tags
    if kind == "product":
        assert result.metadata.category == "Products"
        assert result.metadata.title == "F5 Distributed Cloud API Security"
        assert result.metadata.description == "Protect every API."
        assert result.metadata.publication_date == "2025-09-10"
        assert result.metadata.modification_date == "2026-06-24"
        assert result.metadata.breadcrumb == [
            "Products",
            "Distributed Cloud Services",
            "Api Security",
        ]
        assert result.metadata.tags == ["f5-distributed-cloud", "product"]
        assert len(rendered.assets) == 1
        assert rendered.assets[0].url == "https://cdn.studio.f5.com/images/api.svg"
        assert "platform overview" in rendered.body
        assert "Try our platform" not in rendered.body
        assert "Accept cookies" not in rendered.body
        assert "Jump links" not in rendered.body
    else:
        assert result.metadata.category == "Solutions"
        assert result.metadata.publication_date is None
        assert result.metadata.modification_date == "2026-07-16"
        assert result.metadata.tags == ["f5-distributed-cloud", "solution"]
        assert "WAAP solution" in rendered.body
        assert "Next steps" not in rendered.body


def test_soft_404_and_empty_main() -> None:
    base = page("product")
    base.html = "<html><title>Page not found</title><main>Page not found</main></html>"
    with pytest.raises(NotFoundError):
        WwwF5Adapter().extract(base)
    base.html = "<html><body><nav>Navigation only</nav></body></html>"
    with pytest.raises(NavigationOnlyError):
        WwwF5Adapter().extract(base)


def test_metadata_fallbacks_and_duplicate_image_removal() -> None:
    product = page("product")
    product.html = """<html><head><title>Fallback title</title>
      <script type="application/ld+json">not valid JSON</script>
      <script type="application/ld+json">{"datePublished":"not-a-date"}</script>
      </head><body><main><h1>Fallback heading</h1>
      <p>Substantive description of this F5 Distributed Cloud product and how it works for teams.</p>
      <img src="https://cdn.studio.f5.com/images/a.svg" alt="One">
      <img src="https://cdn.studio.f5.com/images/a.svg" alt="Duplicate">
      </main></body></html>"""
    result = WwwF5Adapter().extract(product)
    assert result.metadata.title == "Fallback title"
    assert result.metadata.publication_date is None
    assert result.metadata.modification_date is None
    assert len(render_html(result.html, product.url).assets) == 1
