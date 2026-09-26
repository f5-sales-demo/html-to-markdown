from pathlib import Path

import pytest

from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
from html_to_markdown.adapters.my_f5 import MyF5Adapter
from html_to_markdown.errors import AuthenticationWallError, NavigationOnlyError, NotFoundError
from html_to_markdown.models import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"


def fetched(name: str, url: str) -> FetchResult:
    return FetchResult(url=url, final_url=url, status_code=200, html=(FIXTURES / name).read_text())


def test_docs_extraction_and_metadata() -> None:
    adapter = DocsCloudAdapter()
    page = fetched("docs_page.html", "https://docs.cloud.f5.com/docs-v2/platform/how-to/configure")
    adapter.classify_page(page)
    result = adapter.extract(page)
    assert result.metadata.title == "Configure a Service"
    assert result.metadata.category == "platform"
    assert result.metadata.subcategory == "how-to"
    assert result.metadata.publication_date == "2025-01-02"
    assert result.metadata.modification_date == "2025-02-03"
    assert "Global F5 header" not in result.html
    assert "Select Service" not in result.html


def test_docs_navigation_only_and_soft_404() -> None:
    adapter = DocsCloudAdapter()
    nav = fetched("docs_navigation.html", "https://docs.cloud.f5.com/docs-v2/a")
    with pytest.raises(NavigationOnlyError):
        adapter.extract(nav)
    soft = FetchResult(
        url=nav.url,
        final_url=nav.url,
        status_code=200,
        html="<html><head><title>Page not found</title></head><body>Page not found</body></html>",
    )
    with pytest.raises(NotFoundError):
        adapter.classify_page(soft)


def test_docs_auth_missing_container_and_metadata_fallbacks() -> None:
    adapter = DocsCloudAdapter()
    auth = FetchResult(
        url=adapter.root_url,
        final_url=adapter.root_url,
        status_code=200,
        html="<body>Authentication required. Sign in to your account.</body>",
    )
    with pytest.raises(AuthenticationWallError):
        adapter.classify_page(auth)
    empty = FetchResult(
        url=adapter.root_url,
        final_url=adapter.root_url,
        status_code=200,
        html="<html><body>empty</body></html>",
    )
    with pytest.raises(ValueError, match="no docs-cloud"):
        adapter.extract(empty)
    metadata = adapter.normalize_metadata(empty, empty.html)
    assert metadata.title == "Index"
    assert metadata.category == "documentation"
    assert metadata.publication_date is None
    assert adapter._date("Published Noday 99, 2025", r"Published\s+(.+)") is None
    title_fallback = FetchResult(
        url="https://docs.cloud.f5.com/docs-v2/a",
        final_url="https://docs.cloud.f5.com/docs-v2/a",
        status_code=200,
        html=(
            "<html><head><title>Fallback title</title></head>"
            "<body><main><p>Body</p></main></body></html>"
        ),
    )
    assert adapter.normalize_metadata(title_fallback, title_fallback.html).title == "Fallback title"
    text_soft_404 = FetchResult(
        url=adapter.root_url,
        final_url=adapter.root_url,
        status_code=200,
        html=(
            "<html><head><title>Unexpected</title></head>"
            "<body>The page you requested could not be found</body></html>"
        ),
    )
    with pytest.raises(NotFoundError):
        adapter.classify_page(text_soft_404)


def test_docs_api_shell_requires_rendered_browser_content() -> None:
    adapter = DocsCloudAdapter()
    server_shell = (
        "<html><head><title>F5 Distributed Cloud Services API for fleet</title></head>"
        f"<body><main>{'<li>Shared API navigation</li>' * 30}</main></body></html>"
    )
    rendered = (
        "<html><head><title>F5 Distributed Cloud Services API for fleet</title></head>"
        f"<body><main><div class='api-info'><h1>Fleet</h1><p>{'Fleet configuration. ' * 30}"
        "</p></div></main></body></html>"
    )
    assert adapter.needs_browser(server_shell)
    assert not adapter.needs_browser(rendered)


def test_my_f5_extraction_retains_related_and_removes_recommendations() -> None:
    adapter = MyF5Adapter()
    page = fetched("my_f5_page.html", "https://my.f5.com/manage/s/article/K000123456")
    result = adapter.extract(page)
    assert result.metadata.slug == "k000123456"
    assert result.metadata.category == "support-solution"
    assert result.metadata.publication_date == "2025-09-01"
    assert "Related Content" in result.html
    assert "Recommendation chrome" not in result.html


def test_my_f5_metadata_fallbacks_and_no_truncation() -> None:
    adapter = MyF5Adapter()
    page = FetchResult(
        url="https://my.f5.com/manage/s/article/not-a-k-number",
        final_url="https://my.f5.com/manage/s/article/not-a-k-number",
        status_code=200,
        html=(
            "<html><head><title>Plain article</title></head><body><article>"
            "<h2>Related Content</h2><p>Keep it.</p></article></body></html>"
        ),
    )
    result = adapter.extract(page)
    assert result.metadata.slug == "article"
    assert result.metadata.title == "Plain article"
    assert result.metadata.category == "knowledge"
    assert result.metadata.publication_date is None
    assert "Keep it" in result.html
    assert adapter._date("Published Date: Invalid 99, 2020", "Published Date") is None


def test_my_f5_removes_ui_and_promotional_cards_but_keeps_related_content() -> None:
    page = FetchResult(
        url="https://my.f5.com/manage/s/article/K000123456",
        final_url="https://my.f5.com/manage/s/article/K000123456",
        status_code=200,
        html="""<article>
          <h1>K000123456: Configure a service</h1>
          <p>Published Date: Sep 1, 2025</p>
          <section><h2>Resolution</h2><p>Keep these technical steps.</p></section>
          <section class="promo-card"><h2>Discover F5</h2><a href="/products">Learn More</a></section>
          <section><h2>Related Content</h2><a href="/manage/s/article/K000654321">Related article</a></section>
          <p>Was this information helpful?</p>
        </article>""",
    )
    result = MyF5Adapter().extract(page)
    assert "technical steps" in result.html
    assert "Related Content" in result.html
    assert "Related article" in result.html
    assert "Learn More" not in result.html
    assert "Published Date" not in result.html
    assert "Was this information helpful" not in result.html


@pytest.mark.parametrize(
    ("html", "error"),
    [
        ("<body>Log in Forgot your password</body>", AuthenticationWallError),
        ("<body>We can't find the page you requested</body>", NotFoundError),
        ("<body>Sorry to interrupt CSS Error</body>", ValueError),
    ],
)
def test_my_f5_error_classification(html: str, error: type[Exception]) -> None:
    page = FetchResult(
        url="https://my.f5.com/manage/s/article/K000999999",
        final_url="https://my.f5.com/manage/s/article/K000999999",
        status_code=200,
        html=html,
    )
    with pytest.raises(error):
        MyF5Adapter().classify_page(page)


def test_readiness_and_shadow_dom_scripts_are_explicit() -> None:
    assert "markers" in (MyF5Adapter().readiness_script() or "")
    assert "querySelector" in (DocsCloudAdapter().readiness_script() or "")
