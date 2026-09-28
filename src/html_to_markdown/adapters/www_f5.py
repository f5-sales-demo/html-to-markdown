"""Scoped HTTP adapter for F5 Distributed Cloud marketing pages."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from html_to_markdown.errors import (
    AuthenticationWallError,
    NavigationOnlyError,
    NotFoundError,
    ScrapeError,
)
from html_to_markdown.models import DiscoveredPage, ExtractedPage, FetchResult, PageMetadata
from html_to_markdown.urls import WWW_F5_SOLUTION_URLS, validate_source_url

from .base import SourceAdapter


class WwwF5Adapter(SourceAdapter):
    source_id = "www-f5-com"
    root_url = "https://www.f5.com/products/distributed-cloud-services"
    sitemap_url = "https://www.f5.com/landing-pages-sitemap.xml"

    def needs_browser(self, html: str) -> bool:
        return False

    async def discover(self, fetcher: Any) -> list[DiscoveredPage]:
        try:
            response = await fetcher._http.get(
                self.sitemap_url, headers={"User-Agent": "f5-html-to-markdown/1.0"}
            )
            if response.status_code != 200 or str(response.url) != self.sitemap_url:
                raise ScrapeError("marketing sitemap is unavailable or redirected")
            root = ElementTree.fromstring(response.content)
        except (
            httpx.HTTPError,
            OSError,
            ValueError,
            ElementTree.ParseError,
            DefusedXmlException,
        ) as error:
            raise ScrapeError("marketing sitemap is unavailable or malformed") from error
        if root.tag != "{http://www.sitemaps.org/schemas/sitemap/0.9}urlset":
            raise ScrapeError("marketing sitemap is malformed")
        pages: dict[str, DiscoveredPage] = {}
        for entry in root:
            if entry.tag != "{http://www.sitemaps.org/schemas/sitemap/0.9}url":
                raise ScrapeError("marketing sitemap contains an invalid entry")
            location = entry.findtext("{*}loc")
            if not location:
                raise ScrapeError("marketing sitemap contains an empty location")
            if location == self.root_url or location.startswith(f"{self.root_url}/"):
                url = validate_source_url(self.source_id, location)
                pages[url] = DiscoveredPage(
                    source_id=self.source_id,
                    url=url,
                    source_last_modified=entry.findtext("{*}lastmod"),
                )
        if not pages or self.root_url not in pages:
            raise ScrapeError("marketing sitemap contains no Distributed Cloud product root")
        for url in WWW_F5_SOLUTION_URLS:
            pages[url] = DiscoveredPage(source_id=self.source_id, url=url)
        return [pages[url] for url in sorted(pages)]

    def readiness_script(self) -> str | None:
        return None

    def classify_page(self, page: FetchResult) -> None:
        soup = BeautifulSoup(page.html, "html.parser")
        title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
        main = soup.select_one("main")
        text = main.get_text(" ", strip=True).casefold() if isinstance(main, Tag) else ""
        if page.status_code == 404 or re.search(r"\b(404|page not found)\b", title):
            raise NotFoundError(f"not found: {page.final_url}")
        if any(term in text[:1000] for term in ("page not found", "we can't find the page")):
            raise NotFoundError(f"soft 404: {page.final_url}")
        if (
            any(
                term in text[:1000]
                for term in (
                    "sign in to access this content",
                    "register to view this content",
                    "complete the form to access this content",
                )
            )
            and len(text) < 500
        ):
            raise AuthenticationWallError(f"gated marketing page: {page.final_url}")

    async def rendered_html(self, playwright_page: Any) -> str:
        return str(await playwright_page.content())

    def extract(self, page: FetchResult) -> ExtractedPage:
        self.classify_page(page)
        soup = BeautifulSoup(page.html, "html.parser")
        main = soup.select_one("main")
        if not isinstance(main, Tag):
            raise NavigationOnlyError(f"no marketing main content: {page.final_url}")
        clone = BeautifulSoup(str(main), "html.parser")
        for selector in (
            "nav",
            "header",
            "footer",
            "form",
            "button",
            "aside",
            '[role="navigation"]',
            '[role="dialog"]',
            '[aria-label*="cookie" i]',
            '[class*="cookie" i]',
            '[class*="sticky" i]',
            '[class*="breadcrumb" i]',
            '[class*="promo" i]',
            '[class*="gated" i]',
            '[data-testid*="gated" i]',
            '[class*="related-resource" i]',
            '[class*="cta-" i]',
            '[data-testid*="cta" i]',
            "#f5-footer",
        ):
            for element in clone.select(selector):
                element.decompose()
        for section in clone.select("section"):
            heading = section.find(["h2", "h3"])
            if heading and heading.get_text(" ", strip=True).casefold() == "next steps":
                section.decompose()
        for anchor in clone.select("a"):
            if anchor.attrs is None:
                continue
            classes = " ".join(anchor.get("class", []))
            label = anchor.get_text(" ", strip=True).casefold()
            if (
                re.search(r"\b(button|cta)\b", classes, re.I)
                or ("bg-primary" in classes and "inline-flex" in classes)
                or label.startswith(("request a trial", "contact us", "try it now"))
            ):
                anchor.decompose()
        seen_images: set[str] = set()
        for image in clone.select("img"):
            source = str(image.get("src") or image.get("data-src") or "")
            if source in seen_images:
                image.decompose()
            else:
                seen_images.add(source)
        for heading in clone.select("h1, h2, h3, h4, h5, h6"):
            if not heading.get_text(" ", strip=True) and not heading.find("img"):
                heading.decompose()
        if len(clone.get_text(" ", strip=True)) < 100:
            raise NavigationOnlyError(
                f"marketing page has no substantive content: {page.final_url}"
            )
        return ExtractedPage(metadata=self.normalize_metadata(page, str(clone)), html=str(clone))

    def normalize_metadata(self, page: FetchResult, html: str) -> PageMetadata:
        soup = BeautifulSoup(page.html, "html.parser")
        path = urlsplit(page.final_url).path.strip("/").split("/")
        product = path[0] == "products"
        slug = path[-1]
        title = self._meta(soup, "og:title") or self._meta(soup, "twitter:title")
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        if not title:
            heading = soup.select_one("main h1")
            title = heading.get_text(" ", strip=True) if isinstance(heading, Tag) else None
        labels = [item.replace("-", " ").title() for item in path]
        dates = self._json_ld_dates(soup)
        return PageMetadata(
            sourceId=self.source_id,
            title=title or labels[-1],
            slug=slug,
            url=page.final_url,
            category="Products" if product else "Solutions",
            subcategory=None,
            breadcrumb=labels,
            publication_date=dates[0],
            modification_date=dates[1],
            description=self._meta(soup, "description") or self._meta(soup, "og:description"),
            tags=["f5-distributed-cloud", "product" if product else "solution"],
        )

    @staticmethod
    def _meta(soup: BeautifulSoup, key: str) -> str | None:
        node = soup.select_one(f'meta[name="{key}"], meta[property="{key}"]')
        value = node.get("content") if isinstance(node, Tag) else None
        if value is None:
            return None
        return str(value).strip() or None

    @classmethod
    def _json_ld_dates(cls, soup: BeautifulSoup) -> tuple[str | None, str | None]:
        published: str | None = None
        modified: str | None = None
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text())
            except (ValueError, TypeError):
                continue
            for node in cls._nodes(data):
                published = published or cls._date(node.get("datePublished"))
                modified = modified or cls._date(node.get("dateModified"))
        return published, modified

    @classmethod
    def _nodes(cls, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value, *cls._nodes(value.get("@graph"))]
        if isinstance(value, list):
            return [node for item in value for node in cls._nodes(item)]
        return []

    @staticmethod
    def _date(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                return None
