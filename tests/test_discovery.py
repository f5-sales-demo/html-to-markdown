import io
import zipfile
from contextlib import asynccontextmanager

import pytest

from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
from html_to_markdown.adapters.my_f5 import MyF5Adapter


class Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class Http:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def get(self, url: str) -> Response:
        return Response(self.content)


class DocsPage:
    def __init__(self) -> None:
        self.select_calls = 0
        self.expand_calls = 0

    async def goto(self, url: str, wait_until: str) -> None:
        return None

    async def evaluate(self, script: str) -> int:
        self.expand_calls += 1
        return 2 if self.expand_calls == 1 else 0

    async def eval_on_selector_all(self, selector: str, script: str) -> list[str]:
        self.select_calls += 1
        if self.select_calls == 1:
            return ["https://docs.cloud.f5.com/docs-v2/platform"]
        return ["https://docs.cloud.f5.com/docs-v2/platform/guide"]


class DocsFetcher:
    def __init__(self, archive: bytes, page: DocsPage | None = None) -> None:
        self._http = Http(archive)
        self.page = page or DocsPage()

    @asynccontextmanager
    async def browser_page(self):
        yield self.page


@pytest.mark.asyncio
async def test_docs_discovery_combines_tree_and_openapi() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("README.txt", "ignored")
        archive.writestr(
            "docs-cloud-f5-com.1.public.ves.io.schema.app.firewall.ves-swagger.json", "{}"
        )
    fetcher = DocsFetcher(payload.getvalue())
    pages = await DocsCloudAdapter().discover(fetcher)
    urls = {page.url for page in pages}
    assert "https://docs.cloud.f5.com/docs-v2/platform/guide" in urls
    assert "https://docs.cloud.f5.com/docs-v2/platform" in urls
    assert DocsCloudAdapter.root_url in urls
    assert fetcher.page.expand_calls >= 2
    assert any("ves-io-schema-app_firewall-api-create" in url for url in urls)


@pytest.mark.asyncio
async def test_docs_discovery_handles_empty_tree_and_bad_archive() -> None:
    class EmptyPage(DocsPage):
        async def eval_on_selector_all(self, selector: str, script: str) -> list[str]:
            self.select_calls += 1
            return (
                ["https://docs.cloud.f5.com/docs-v2/standalone"] if self.select_calls == 1 else []
            )

    pages = await DocsCloudAdapter().discover(DocsFetcher(b"not a zip", EmptyPage()))
    assert [page.url for page in pages] == [
        "https://docs.cloud.f5.com/docs-v2",
        "https://docs.cloud.f5.com/docs-v2/standalone",
    ]


@pytest.mark.asyncio
async def test_docs_discovery_skips_bad_service() -> None:
    class BrokenPage(DocsPage):
        async def goto(self, url: str, wait_until: str) -> None:
            if url != DocsCloudAdapter.root_url:
                raise RuntimeError("unavailable")

    pages = await DocsCloudAdapter().discover(DocsFetcher(b"not a zip", BrokenPage()))
    assert [page.url for page in pages] == [
        "https://docs.cloud.f5.com/docs-v2",
        "https://docs.cloud.f5.com/docs-v2/platform",
    ]


class Locator:
    def __init__(self, page: "MyPage", body: bool = False) -> None:
        self.page = page
        self.body = body

    @property
    def last(self) -> "Locator":
        return self

    async def inner_text(self) -> str:
        return "K000123456 K000123456 K000123457"

    async def count(self) -> int:
        return 1

    async def is_disabled(self) -> bool:
        return True

    async def click(self) -> None:
        self.page.clicked += 1

    async def get_attribute(self, name: str) -> str:
        return "disabled"


class MyPage:
    clicked = 0

    async def goto(self, url: str, wait_until: str) -> None:
        assert "f-f5_document_type=Support%20Solution" not in url

    async def wait_for_function(self, script: str) -> None:
        return None

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector == "body")

    async def wait_for_timeout(self, timeout: int) -> None:
        return None


class MyFetcher:
    @asynccontextmanager
    async def browser_page(self):
        yield MyPage()


@pytest.mark.asyncio
async def test_my_f5_discovery_deduplicates_k_numbers() -> None:
    pages = await MyF5Adapter().discover(MyFetcher())
    assert [page.url for page in pages] == [
        "https://my.f5.com/manage/s/article/K000123456",
        "https://my.f5.com/manage/s/article/K000123457",
    ]


@pytest.mark.asyncio
async def test_my_f5_pagination_stops_when_results_do_not_change() -> None:
    class EnabledLocator(Locator):
        async def is_disabled(self) -> bool:
            return False

    class PagingPage(MyPage):
        def locator(self, selector: str) -> Locator:
            return EnabledLocator(self, selector == "body")

    class PagingFetcher:
        @asynccontextmanager
        async def browser_page(self):
            yield PagingPage()

    pages = await MyF5Adapter().discover(PagingFetcher())
    assert len(pages) == 2


class EvaluatePage:
    async def evaluate(self, script: str) -> str:
        return "<article><p>flattened shadow content</p></article>"


@pytest.mark.asyncio
async def test_rendered_html_contracts() -> None:
    page = EvaluatePage()
    assert "flattened" in await MyF5Adapter().rendered_html(page)

    class ContentPage:
        async def content(self) -> str:
            return "<main>docs</main>"

    assert await DocsCloudAdapter().rendered_html(ContentPage()) == "<main>docs</main>"
