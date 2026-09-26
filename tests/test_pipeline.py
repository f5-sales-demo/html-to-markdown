from pathlib import Path

import pytest

from html_to_markdown.errors import AuthenticationWallError, NavigationOnlyError, NotFoundError
from html_to_markdown.models import DiscoveredPage, FetchResult, PageStatus
from html_to_markdown.pipeline import Pipeline, re_safe_suffix, run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"


class FakeFetcher:
    async def fetch(self, adapter: object, url: str) -> FetchResult:
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html=(FIXTURES / "docs_page.html").read_text(),
        )

    async def fetch_asset(self, url: str) -> tuple[bytes, str]:
        return b"meaningful image", "image/png"

    async def _http_fetch(self, source_id: str, url: str) -> FetchResult:
        return FetchResult(url=url, final_url=url + "-resolved", status_code=200, html="ok")

    async def resolve_redirect(self, source_id: str, url: str) -> str:
        return url + "-resolved"

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_discovery_to_document_asset_and_resume(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = FakeFetcher()  # type: ignore[assignment]
    url = "https://docs.cloud.f5.com/docs-v2/platform/how-to/configure"
    pipeline.store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url)])
    records = await pipeline.scrape("docs-cloud-f5-com")
    assert len(records) == 1
    document = records[0].output_path.read_text()
    assert "assets/" in document
    assert "-resolved" in document
    assert pipeline.store.counts()[PageStatus.SUCCESS] >= 1
    assert await pipeline.scrape("docs-cloud-f5-com") == []
    pipeline.store.close()


@pytest.mark.asyncio
async def test_focused_discovery_enforces_source(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    count = await pipeline.discover(
        "docs-cloud-f5-com", "https://docs.cloud.f5.com/docs-v2/platform"
    )
    assert count == 1
    with pytest.raises(ValueError, match="explicit"):
        await pipeline.discover("all", "https://docs.cloud.f5.com/docs-v2/platform")
    await pipeline.fetcher.close()
    pipeline.store.close()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (NotFoundError("gone"), PageStatus.REMOVED_NOT_FOUND),
        (NavigationOnlyError("navigation"), PageStatus.REMOVED_NAVIGATION),
        (AuthenticationWallError("login"), PageStatus.AUTHENTICATION_WALL),
        (RuntimeError("unknown"), PageStatus.FAILED),
    ],
)
@pytest.mark.asyncio
async def test_pipeline_classifies_failures(
    tmp_path: Path, error: Exception, status: PageStatus
) -> None:
    class ErrorFetcher(FakeFetcher):
        async def fetch(self, adapter: object, url: str) -> FetchResult:
            raise error

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = ErrorFetcher()  # type: ignore[assignment]
    url = "https://docs.cloud.f5.com/docs-v2/error"
    pipeline.store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url)])
    assert await pipeline.scrape("docs-cloud-f5-com") == []
    assert pipeline.store.rows()[0]["status"] == status
    pipeline.store.close()


def test_pipeline_configuration_and_suffixes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 1 and 20"):
        Pipeline(tmp_path, concurrency=0)
    with pytest.raises(ValueError, match="unknown source"):
        Pipeline.adapters("third-party")
    assert len(Pipeline.adapters("all")) == 2
    assert re_safe_suffix(".png")
    assert not re_safe_suffix(".not-a-safe-extension")
    assert (
        Pipeline._asset_extension("https://example.test/file.unsafe-long", "unknown/type") == ".bin"
    )


@pytest.mark.asyncio
async def test_pipeline_context_discovery_validate_and_package(tmp_path: Path, monkeypatch) -> None:
    from html_to_markdown import pipeline as module
    from html_to_markdown.adapters.base import SourceAdapter
    from html_to_markdown.models import ExtractedPage, PageMetadata

    class Adapter(SourceAdapter):
        source_id = "fixture"
        root_url = "https://fixture.test/root"

        async def discover(self, fetcher: object) -> list[DiscoveredPage]:
            return []

        def readiness_script(self) -> str | None:
            return None

        def classify_page(self, page: FetchResult) -> None:
            return None

        async def rendered_html(self, playwright_page: object) -> str:
            return ""

        def extract(self, page: FetchResult) -> ExtractedPage:
            return ExtractedPage(
                metadata=PageMetadata(
                    sourceId="fixture",
                    title="x",
                    slug="x",
                    url=page.url,
                    category="x",
                    tags=[],
                ),
                html="<p>x</p>",
            )

        def normalize_metadata(self, page: FetchResult, html: str) -> PageMetadata:
            return self.extract(page).metadata

    monkeypatch.setattr(module, "ADAPTERS", {"fixture": Adapter})
    async with Pipeline(tmp_path) as pipeline:
        assert await pipeline.discover("all") == 0
        pipeline.validate()
        assert pipeline.package("a", "b").exists()


@pytest.mark.asyncio
async def test_redirect_resolution_keeps_external_and_failed_internal(tmp_path: Path) -> None:
    class FailingFetcher(FakeFetcher):
        async def resolve_redirect(self, source_id: str, url: str) -> str:
            raise RuntimeError("no response")

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = FailingFetcher()  # type: ignore[assignment]
    html = (
        '<a>none</a><a href="https://example.test/x">external</a><a href="/docs-v2/x">internal</a>'
    )
    result = await pipeline._resolve_redirects(
        Pipeline.adapters("docs-cloud-f5-com")[0],
        "https://docs.cloud.f5.com/docs-v2/base",
        html,
    )
    assert "https://example.test/x" in result
    assert "https://docs.cloud.f5.com/docs-v2/x" in result
    pipeline.store.close()


@pytest.mark.asyncio
async def test_run_pipeline_orchestration(tmp_path: Path, monkeypatch) -> None:
    async def no_discover(self, source: str, url: str | None = None) -> int:
        return 0

    async def no_scrape(self, source: str, force: bool = False) -> list[object]:
        return []

    monkeypatch.setattr(Pipeline, "discover", no_discover)
    monkeypatch.setattr(Pipeline, "scrape", no_scrape)
    archive = await run_pipeline(source="all", output=tmp_path)
    assert archive.exists()
