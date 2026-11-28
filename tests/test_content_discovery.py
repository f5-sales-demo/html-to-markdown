"""Discovery is complete before admission and records observable pagination evidence."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from html_to_markdown.adapters.my_f5 import MyF5Adapter
from html_to_markdown.adapters.www_f5 import WwwF5Adapter
from html_to_markdown.errors import AllowlistError, NotFoundError, ScrapeError
from html_to_markdown.models import FetchResult
from html_to_markdown.urls import validate_source_url


class SearchPage:
    def __init__(self, texts: list[str], *, terminal: bool = True):
        self.texts = texts
        self.position = 0
        self.terminal = terminal
        self.waited = False
        self.last = self

    async def goto(self, url: str, **kwargs: object) -> None:
        self.position = 0

    def locator(self, selector: str) -> "SearchPage":
        return self

    async def inner_text(self) -> str:
        return self.texts[self.position]

    async def count(self) -> int:
        return 1

    async def is_disabled(self) -> bool:
        return self.terminal and self.position == len(self.texts) - 1

    async def get_attribute(self, name: str) -> str | None:
        return None

    async def click(self) -> None:
        self.position = min(self.position + 1, len(self.texts) - 1)

    async def wait_for_function(self, script: str, *, arg: object = None) -> None:
        if arg is not None:
            self.waited = True
            assert "innerText" in script
            if self.texts[self.position] == arg:
                raise TimeoutError("result rendering did not change")


class SearchFetcher:
    def __init__(self, page: SearchPage):
        self.page = page
        self.events: list[dict[str, object]] = []

    @asynccontextmanager
    async def browser_page(self):
        yield self.page

    def record_discovery(self, **event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_terminal_page_is_read_after_observable_render_change() -> None:
    page = SearchPage(["K000123456", "K000123457"])
    fetcher = SearchFetcher(page)
    found = await MyF5Adapter().discover(fetcher)
    assert {item.url.rsplit("/", 1)[-1] for item in found} == {"K000123456", "K000123457"}
    assert page.waited
    assert any(event.get("complete") for event in fetcher.events)
    assert len({event["origin"] for event in fetcher.events}) >= 3


@pytest.mark.asyncio
async def test_loop_or_unchanged_render_is_visible_with_partial_evidence() -> None:
    fetcher = SearchFetcher(SearchPage(["K000123456"], terminal=False))
    with pytest.raises(ScrapeError, match="pagination"):
        await MyF5Adapter().discover(fetcher)
    assert fetcher.events and not any(event.get("complete") for event in fetcher.events)


@pytest.mark.asyncio
async def test_empty_later_page_is_not_completion() -> None:
    with pytest.raises(ScrapeError, match="empty"):
        await MyF5Adapter().discover(SearchFetcher(SearchPage(["K000123456", "No results"])))


@pytest.mark.asyncio
async def test_missing_terminal_control_requires_result_range_evidence() -> None:
    class MissingNext(SearchPage):
        async def count(self) -> int:
            return 0

    with pytest.raises(ScrapeError, match="completion"):
        await MyF5Adapter().discover(SearchFetcher(MissingNext(["K000123456"])))
    found = await MyF5Adapter().discover(
        SearchFetcher(MissingNext(["Results 1-1 of 1 K000123456"]))
    )
    assert len(found) == 1


def test_useful_short_answer_readiness_has_no_character_quota() -> None:
    script = MyF5Adapter().readiness_script() or ""
    assert "text.length > 1000" not in script
    assert "shadowRoot" in script and "Answer" in script


@pytest.mark.asyncio
async def test_link_discovery_continues_after_scoped_out_redirect() -> None:

    root = WwwF5Adapter.root_url
    redirected = "https://www.f5.com/solutions/old-corporate-route"
    integration = "https://www.f5.com/solutions/new-cloud-integration"

    class Fetcher:
        def __init__(self) -> None:
            self._http = self

        events: list[dict] = []

        async def get(self, url: str, **kwargs: object):
            return SimpleNamespace(
                status_code=200,
                url=url,
                content=(
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{root}</loc></url><url><loc>{redirected}</loc></url></urlset>"
                ).encode(),
            )

        async def _http_fetch(self, source: str, url: str):
            if url == redirected:
                raise AllowlistError("redirect outside source scope")
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                html=f'<main><nav><a href="https://www.f5.com/industries/unrelated">Corporate</a></nav><a href="https://unrelated.invalid/">External</a><a href="{integration}">Integration</a></main>',
            )

        def record_discovery(self, **event: object):
            self.events.append(event)

    fetcher = Fetcher()
    pages = await WwwF5Adapter().discover(fetcher)
    assert {page.url for page in pages} == {root, redirected, integration}
    assert any(event.get("disposition") == "outside_discovery_scope" for event in fetcher.events)
    assert validate_source_url("www-f5-com", "https://www.f5.com/industries/banking")


@pytest.mark.asyncio
async def test_sitemap_discovers_solutions_without_fixed_seeds() -> None:
    root = WwwF5Adapter.root_url
    solution = "https://www.f5.com/solutions/new-distributed-cloud-integration"

    class Fetcher:
        def __init__(self) -> None:
            self._http = self

        async def get(self, url: str, **kwargs: object):
            return SimpleNamespace(
                status_code=200,
                url=url,
                content=(
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{root}</loc></url><url><loc>{solution}</loc></url>"
                    "</urlset>"
                ).encode(),
            )

    fetcher = Fetcher()
    pages = await WwwF5Adapter().discover(fetcher)
    assert {p.url for p in pages} == {root, solution}
    assert validate_source_url("www-f5-com", solution) == solution


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http404", "soft404", "transport"])
async def test_missing_sitemap_article_does_not_hide_later_article_links(failure: str) -> None:
    root = WwwF5Adapter.root_url
    missing = "https://www.f5.com/solutions/a-missing-article"
    later = "https://www.f5.com/solutions/z-live-article"
    linked = "https://www.f5.com/solutions/new-distributed-cloud-integration"

    class Fetcher:
        def __init__(self) -> None:
            self._http = self
            self.events: list[dict[str, object]] = []
            self.visited: list[str] = []

        async def get(self, url: str, **kwargs: object):
            return SimpleNamespace(
                status_code=200,
                url=url,
                content=(
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{root}</loc></url><url><loc>{missing}</loc></url>"
                    f"<url><loc>{later}</loc></url></urlset>"
                ).encode(),
            )

        async def _http_fetch(self, source: str, url: str):
            self.visited.append(url)
            if url == missing:
                if failure == "http404":
                    raise NotFoundError("HTTP 404")
                if failure == "transport":
                    raise ScrapeError("Unresolved transport failure")
                html = "<title>Page not found</title><main>Page not found</main>"
            else:
                html = (
                    f'<main><a href="{linked}">Integration</a></main>'
                    if url == later
                    else "<main></main>"
                )
            return FetchResult(url=url, final_url=url, status_code=200, html=html)

        def record_discovery(self, **event: object) -> None:
            self.events.append(event)

    fetcher = Fetcher()
    if failure == "transport":
        with pytest.raises(ScrapeError, match="Unresolved transport"):
            await WwwF5Adapter().discover(fetcher)
        assert later not in fetcher.visited
        assert not any(
            event.get("disposition") == "unavailable_discovery_reference"
            for event in fetcher.events
        )
        return
    pages = await WwwF5Adapter().discover(fetcher)
    assert {page.url for page in pages} == {root, missing, later, linked}
    assert fetcher.visited.index(later) > fetcher.visited.index(missing)
    assert any(
        event.get("origin") == missing
        and event.get("disposition") == "unavailable_discovery_reference"
        and event.get("error_class") == "NotFoundError"
        and event.get("complete") is True
        for event in fetcher.events
    )
