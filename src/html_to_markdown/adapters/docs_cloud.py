"""Adapter for docs.cloud.f5.com/docs-v2."""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from html_to_markdown.errors import AuthenticationWallError, NavigationOnlyError, NotFoundError
from html_to_markdown.models import DiscoveredPage, ExtractedPage, FetchResult, PageMetadata
from html_to_markdown.urls import validate_source_url

from .base import SourceAdapter


class DocsCloudAdapter(SourceAdapter):
    source_id = "docs-cloud-f5-com"
    root_url = "https://docs.cloud.f5.com/docs-v2"

    def needs_browser(self, html: str) -> bool:
        if super().needs_browser(html):
            return True
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        return " API for " in title and soup.select_one(".api-info") is None

    async def discover(self, fetcher: object) -> list[DiscoveredPage]:
        urls: set[str] = {self.root_url}
        async with fetcher.browser_page() as page:  # type: ignore[attr-defined]
            await page.goto(self.root_url, wait_until="domcontentloaded")
            service_urls = await page.eval_on_selector_all(
                'a[href*="/docs-v2/"]', "els => [...new Set(els.map(e => e.href.split('#')[0]))]"
            )
            for service_url in service_urls:
                try:
                    service_url = validate_source_url(self.source_id, service_url)
                    urls.add(service_url)
                    await page.goto(service_url, wait_until="domcontentloaded")
                    for _ in range(20):
                        expanded = await page.evaluate(
                            """() => {
                          let clicked = 0;
                          [...document.querySelectorAll('button')]
                            .filter(b => /Navigation Menu/i.test(
                              b.textContent || b.getAttribute('aria-label') || ''))
                            .forEach(b => { b.click(); clicked += 1; });
                          [...document.querySelectorAll('[role="treeitem"][aria-expanded="false"]')]
                            .forEach(i => {
                              (i.querySelector(':scope > div') || i).click();
                              clicked += 1;
                            });
                          return clicked;
                        }"""
                        )
                        if not isinstance(expanded, int) or expanded == 0:
                            break
                        wait = getattr(page, "wait_for_timeout", None)
                        if wait is not None:
                            await wait(100)
                    links = await page.eval_on_selector_all(
                        '[role="tree"] a[href*="/docs-v2/"], main a[href*="/docs-v2/"]',
                        "els => [...new Set(els.map(e => e.href.split('#')[0]))]",
                    )
                    urls.update(validate_source_url(self.source_id, item) for item in links)
                    if not links:
                        urls.add(service_url)
                except Exception:  # nosec B112
                    continue
        try:
            spec_url = f"{self.root_url}/downloads/f5-distributed-cloud-open-api.zip"
            response = await fetcher._http.get(spec_url)  # type: ignore[attr-defined]  # noqa: SLF001
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                for name in archive.namelist():
                    match = re.match(
                        r"docs-cloud-f5-com\..*\.schema\.(.+?)\.ves-swagger\.json$", name
                    )
                    if not match:
                        continue
                    schema = match.group(1).replace(".", "_")
                    urls.add(
                        f"{self.root_url}/platform/reference/api-ref/ves-io-schema-{schema}-api-create"
                    )
        except (OSError, zipfile.BadZipFile):
            pass
        return [DiscoveredPage(source_id=self.source_id, url=url) for url in sorted(urls)]

    def readiness_script(self) -> str | None:
        return "() => document.querySelector('main, article, .api-info') !== null"

    def classify_page(self, page: FetchResult) -> None:
        soup = BeautifulSoup(page.html, "html.parser")
        text = soup.get_text(" ", strip=True).casefold()
        title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
        if any(
            term in text[:2000] for term in ("sign in to your account", "authentication required")
        ):
            raise AuthenticationWallError(f"authentication wall: {page.final_url}")
        if page.status_code == 404 or any(term in title for term in ("404", "page not found")):
            raise NotFoundError(f"not found: {page.final_url}")
        if (
            "page not found" in text[:600]
            or "the page you requested could not be found" in text[:1200]
        ):
            raise NotFoundError(f"soft 404: {page.final_url}")

    async def rendered_html(self, playwright_page: object) -> str:
        return str(await playwright_page.content())  # type: ignore[attr-defined]

    def extract(self, page: FetchResult) -> ExtractedPage:
        soup = BeautifulSoup(page.html, "html.parser")
        container = (
            soup.select_one(".api-info") or soup.select_one("main") or soup.select_one("article")
        )
        if not isinstance(container, Tag):
            raise ValueError("no docs-cloud content container")
        clone = BeautifulSoup(str(container), "html.parser")
        for selector in (
            "nav",
            "header",
            "footer",
            '[role="tree"]',
            '[class*="find-in-page"]',
            '[id*="select-service"]',
            ".MuiGrid-grid-xs-3",
            "button",
            '[aria-label="On this page"]',
        ):
            for element in clone.select(selector):
                element.decompose()
        for text_node in clone.find_all(
            string=re.compile(r"^\s*Select Service\s*$", re.IGNORECASE)
        ):
            if text_node.parent:
                text_node.parent.decompose()
        links = [item for item in clone.select('a[href*="/docs-v2/"]') if item.get("href")]
        content_nodes = clone.select("p, ul, ol, table, pre, blockquote, h2, h3")
        text_without_links = clone.get_text(" ", strip=True)
        for link in links:
            text_without_links = text_without_links.replace(link.get_text(" ", strip=True), "")
        if len(links) >= 3 and len(content_nodes) < 5 and len(text_without_links.strip()) < 300:
            raise NavigationOnlyError(f"navigation-only page: {page.final_url}")
        return ExtractedPage(metadata=self.normalize_metadata(page, str(clone)), html=str(clone))

    def normalize_metadata(self, page: FetchResult, html: str) -> PageMetadata:
        soup = BeautifulSoup(page.html, "html.parser")
        path = [
            item for item in urlsplit(page.final_url).path.split("/") if item and item != "docs-v2"
        ]
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", path[-1] if path else "index").strip("-")
        title_node = soup.select_one("main h1, article h1, h1")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        description = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description_value = description.get("content") if isinstance(description, Tag) else None
        text = soup.get_text(" ", strip=True)
        return PageMetadata(
            sourceId=self.source_id,
            title=title or slug.replace("-", " ").title(),
            slug=slug,
            url=page.final_url,
            category=path[0] if path else "documentation",
            subcategory=path[1] if len(path) > 1 else None,
            breadcrumb=[item.replace("-", " ").title() for item in path] or None,
            publication_date=self._date(text, r"Published\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})"),
            modification_date=self._date(text, r"Last modified\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})"),
            description=str(description_value) if description_value is not None else None,
            tags=path[:2],
        )

    @staticmethod
    def _date(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            return None
