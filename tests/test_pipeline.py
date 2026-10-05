from pathlib import Path

import pytest

from html_to_markdown import pipeline as module
from html_to_markdown.adapters.base import SourceAdapter
from html_to_markdown.errors import (
    AllowlistError,
    AuthenticationWallError,
    NavigationOnlyError,
    NotFoundError,
)
from html_to_markdown.models import (
    DiscoveredPage,
    ExtractedPage,
    FetchResult,
    PageMetadata,
    PageStatus,
)
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
        final_url = url + "-resolved"
        return FetchResult(url=url, final_url=final_url, status_code=200, html="ok")

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
async def test_relationship_candidates_are_scoped_to_selected_content(tmp_path: Path) -> None:
    class LinkFetcher(FakeFetcher):
        async def fetch(self, adapter: object, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                html=(
                    '<main><nav><a href="/docs-v2/navigation">Navigation</a></nav>'
                    "<h1>Page</h1><p>Technical content.</p>"
                    '<a href="/docs-v2/related">Related</a></main>'
                ),
            )

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = LinkFetcher()  # type: ignore[assignment]
    url = "https://docs.cloud.f5.com/docs-v2/page"
    pipeline.store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url)])

    assert len(await pipeline.scrape("docs-cloud-f5-com")) == 1
    assert pipeline.store.candidate_links(url) == ["https://docs.cloud.f5.com/docs-v2/related"]
    pipeline.store.close()


@pytest.mark.asyncio
async def test_multi_digit_asset_placeholders_do_not_collide(tmp_path: Path) -> None:
    class ManyImageFetcher(FakeFetcher):
        async def fetch(self, adapter: object, url: str) -> FetchResult:
            image_tags = [f'<img src="/i{index}.png">' for index in range(12)]
            images = "".join(image_tags)
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                html=f"<html><head><title>Images</title></head><body><main>{images}</main></body></html>",
            )

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = ManyImageFetcher()  # type: ignore[assignment]
    url = "https://docs.cloud.f5.com/docs-v2/images"
    pipeline.store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url)])

    records = await pipeline.scrape("docs-cloud-f5-com")

    assert len(records) == 1
    document = records[0].output_path.read_text()
    assert document.count("assets/") == 12
    assert "asset://" not in document
    assert pipeline.store.rows()[0]["status"] == PageStatus.SUCCESS
    pipeline.store.close()


@pytest.mark.asyncio
async def test_external_image_is_preserved_without_fetching(tmp_path: Path) -> None:
    class ExternalImageFetcher(FakeFetcher):
        async def fetch(self, adapter: object, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                html=(
                    "<html><head><title>External figure</title></head><body><main>"
                    "<h1>External figure</h1><p>Technical body.</p>"
                    '<figure><img src="https://example.com/diagram.png" alt="Diagram">'
                    "</figure></main></body></html>"
                ),
            )

        async def fetch_asset(self, url: str) -> tuple[bytes, str]:
            raise AllowlistError(f"outside allowlist: {url}")

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = ExternalImageFetcher()  # type: ignore[assignment]
    url = "https://docs.cloud.f5.com/docs-v2/external-figure"
    pipeline.store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url)])
    records = await pipeline.scrape("docs-cloud-f5-com")
    assert len(records) == 1
    assert "![Diagram](https://example.com/diagram.png)" in records[0].output_path.read_text()
    assert records[0].asset_hashes == []
    pipeline.store.close()


@pytest.mark.asyncio
async def test_redirect_aliases_do_not_overwrite_canonical_documents(tmp_path: Path) -> None:
    canonical = "https://docs.cloud.f5.com/docs-v2/how-to/user"
    alias = "https://docs.cloud.f5.com/docs-v2/docs/how-to/user"

    class RedirectFetcher(FakeFetcher):
        async def fetch(self, adapter: object, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                final_url=canonical,
                status_code=200,
                html=(
                    "<html><head><title>User API</title></head><body><main>"
                    f"<h1>User API</h1><p>Fetched from {url}.</p>"
                    "</main></body></html>"
                ),
            )

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = RedirectFetcher()  # type: ignore[assignment]
    pipeline.store.discover(
        [
            DiscoveredPage(source_id="docs-cloud-f5-com", url=canonical),
            DiscoveredPage(source_id="docs-cloud-f5-com", url=alias),
        ]
    )

    records = await pipeline.scrape("docs-cloud-f5-com")

    assert len({record.output_path for record in records}) == 2
    for record in records:
        document = record.output_path.read_text(encoding="utf-8")
        assert f"url: {record.url}" in document
        assert f"canonical_url: {canonical}" in document
        assert record.content_hash in document
    pipeline.store.close()


@pytest.mark.asyncio
async def test_focused_discovery_enforces_source(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    url = "https://docs.cloud.f5.com/docs-v2/platform"
    count = await pipeline.discover("docs-cloud-f5-com", url)
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
async def test_classifies_failures(tmp_path: Path, error: Exception, status: PageStatus) -> None:
    class ErrorFetcher(FakeFetcher):
        async def fetch(self, adapter: object, url: str) -> FetchResult:
            raise error

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = ErrorFetcher()  # type: ignore[assignment]
    url = "https://docs.cloud.f5.com/docs-v2/error"
    pipeline.store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url)])
    assert await pipeline.scrape("docs-cloud-f5-com") == []
    row = pipeline.store.rows()[0]
    assert row["status"] == status
    assert row["error_class"] == type(error).__name__
    assert row["error_message"] == str(error)
    pipeline.store.close()


def test_pipeline_configuration_and_suffixes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 1 and 20"):
        Pipeline(tmp_path, concurrency=0)
    with pytest.raises(ValueError, match="unknown source"):
        Pipeline.adapters("third-party")
    assert len(Pipeline.adapters("all")) == 4
    assert re_safe_suffix(".png")
    assert not re_safe_suffix(".not-a-safe-extension")
    url = "https://example.test/file.unsafe-long"
    extension = Pipeline._asset_extension(url, "unknown/type")  # pylint: disable=protected-access
    assert extension == ".bin"


@pytest.mark.asyncio
async def test_pipeline_context_and_package(tmp_path: Path, monkeypatch) -> None:
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
async def test_redirect_resolution_keeps_external_and_failed_internal(
    tmp_path: Path,
) -> None:
    class FailingFetcher(FakeFetcher):
        async def resolve_redirect(self, source_id: str, url: str) -> str:
            raise RuntimeError("no response")

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = FailingFetcher()  # type: ignore[assignment]
    external = '<a href="https://example.test/x">external</a>'
    internal = '<a href="/docs-v2/x">internal</a>'
    html = f"<a>none</a>{external}{internal}"
    result = await pipeline._resolve_redirects(  # pylint: disable=protected-access
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


@pytest.mark.asyncio
async def test_inventory_only_requires_benchmark_and_skips_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    async def unexpected_discovery(self, source: str, url: str | None = None) -> int:
        raise AssertionError("live discovery must not run")

    async def no_scrape(self, source: str, force: bool = False) -> list[object]:
        return []

    monkeypatch.setattr(Pipeline, "discover", unexpected_discovery)
    monkeypatch.setattr(Pipeline, "scrape", no_scrape)
    with pytest.raises(ValueError, match="requires --benchmark"):
        await run_pipeline(source="all", output=tmp_path, inventory_only=True)
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text('{"version": 1, "urls": []}', encoding="utf-8")
    archive = await run_pipeline(
        source="all", output=tmp_path, benchmark=benchmark, inventory_only=True
    )
    assert archive.exists()


@pytest.mark.asyncio
async def test_fresh_extraction_curates_appstack_and_builds_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from html_to_markdown import state as state_module
    from html_to_markdown.render import split_document

    monkeypatch.setattr(state_module, "utc_now", lambda: "2026-10-05T00:00:00Z")

    class MixedFetcher(FakeFetcher):
        async def fetch(self, adapter: object, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                html=(
                    "<html><head><title>Mesh guide</title></head><body><main>"
                    "<h1>Mesh guide</h1>"
                    "<p>Use Mesh routing to connect the current sites.</p>"
                    "<p>Deploy AppStack on a Customer Edge site.</p>"
                    "</main></body></html>"
                ),
            )

    async def build(output: Path) -> dict[str, bytes]:
        pipeline = Pipeline(output)
        await pipeline.fetcher.close()
        pipeline.fetcher = MixedFetcher()  # type: ignore[assignment]
        url = "https://docs.cloud.f5.com/docs-v2/how-to/mixed-mesh-guide"
        pipeline.store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=url)])
        records = await pipeline.scrape("docs-cloud-f5-com")
        assert len(records) == 1
        assert "AppStack" in records[0].output_path.read_text()
        pipeline.validate()
        _, body = split_document(records[0].output_path.read_text())
        assert "Mesh routing" in body
        assert "AppStack" not in body
        pipeline.package("2026-10-05T00:00:00Z", "2026-10-05T00:00:00Z")
        names = (
            "quality-report.json",
            "quality-report.md",
            "manifest.json",
            "SHA256SUMS",
            "html-to-markdown-content.tar.gz",
            "html-to-markdown-content.tar.gz.sha256",
            "curation-audit.json",
        )
        files = [*output.glob("content/**/*/index.md"), *(output / name for name in names)]
        artifacts = {path.relative_to(output).as_posix(): path.read_bytes() for path in files}
        pipeline.store.close()
        return artifacts

    assert await build(tmp_path / "first") == await build(tmp_path / "second")
