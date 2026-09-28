"""Adapter for the Salesforce Lightning site at my.f5.com/manage/s."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from html_to_markdown.errors import AuthenticationWallError, NotFoundError
from html_to_markdown.models import DiscoveredPage, ExtractedPage, FetchResult, PageMetadata

from .base import SourceAdapter


class MyF5Adapter(SourceAdapter):
    source_id = "my-f5-com"
    root_url = "https://my.f5.com/manage/s"
    browser_only = True
    document_types = ("Support Solution", "Operations Guide", "Knowledge", "Policy")

    async def discover(self, fetcher: object) -> list[DiscoveredPage]:
        filters = ",".join(self.document_types)
        search = (
            f"{self.root_url}/global-search/%40uri#q={quote('F5 Distributed Cloud')}"
            f"&f-f5_document_type={filters}&cf-f5_version={quote('F5 Distributed Cloud')}"
            "&aq=%40f5_archived&numberOfResults=100"
        )
        found: set[str] = set()
        async with fetcher.browser_page() as page:  # type: ignore[attr-defined]
            await page.goto(search, wait_until="domcontentloaded")
            await page.wait_for_function(
                """() => /K\\d{6,}/.test(document.body?.innerText || '') ||
                  /no results/i.test(document.body?.innerText || '')"""
            )
            for _ in range(100):
                text = await page.locator("body").inner_text()
                for article_id in re.findall(r"K\d{6,}", text):
                    found.add(f"{self.root_url}/article/{article_id}")
                next_button = page.locator(
                    'button[aria-label="Next"], button[title="Next"], button:has-text("Next")'
                ).last
                if await next_button.count() == 0 or await next_button.is_disabled():
                    break
                before = sorted(found)
                await next_button.click()
                await page.wait_for_timeout(1000)
                if sorted(found) == before and "disabled" in (
                    await next_button.get_attribute("class") or ""
                ):
                    break
        return [DiscoveredPage(source_id=self.source_id, url=url) for url in sorted(found)]

    def readiness_script(self) -> str | None:
        return """() => {
          const text = document.body?.innerText || '';
          const markers = ['Environment', 'Answer', 'Cause', 'Resolution', 'Procedure',
            'Description', 'Related Content', 'What Happened'];
          return markers.some(marker => text.includes(marker)) && text.length > 1000;
        }"""

    def classify_page(self, page: FetchResult) -> None:
        text = BeautifulSoup(page.html, "html.parser").get_text(" ", strip=True).casefold()
        if any(term in text[:1600] for term in ("log in", "sign in", "forgot your password")):
            raise AuthenticationWallError(f"authentication wall: {page.final_url}")
        soft_404 = ("article not found", "page not found", "we can't find the page")
        if page.status_code == 404 or any(term in text[:1200] for term in soft_404):
            raise NotFoundError(f"not found: {page.final_url}")
        if "css error" in text[:1000] or "sorry to interrupt" in text[:1000]:
            raise ValueError(f"Lightning error page: {page.final_url}")

    async def rendered_html(self, playwright_page: object) -> str:
        return str(
            await playwright_page.evaluate(  # type: ignore[attr-defined]
                """() => {
                  const flatten = (node) => {
                    const clone = node.cloneNode(false);
                    const children = node.shadowRoot ? node.shadowRoot.childNodes : node.childNodes;
                    for (const child of children) {
                      if (child.nodeType === Node.ELEMENT_NODE) clone.appendChild(flatten(child));
                      else clone.appendChild(child.cloneNode(true));
                    }
                    return clone;
                  };
                  const root = document.querySelector('c-site-article-detail-container') ||
                    document.querySelector('article') || document.body;
                  return flatten(root).outerHTML;
                }"""
            )
        )

    def extract(self, page: FetchResult) -> ExtractedPage:
        soup = BeautifulSoup(page.html, "html.parser")
        container = soup.select_one("c-site-article-detail-container, article") or soup
        candidate_links = [str(anchor["href"]) for anchor in container.select("a[href]")]
        for selector in (
            "header",
            "footer",
            "nav",
            "button",
            ".article-type",
            ".article-header",
            ".article-dates",
            ".share-article",
            ".recommendation-navigation-link",
            ".slds-assistive-text",
            "c-helper-icon",
            '[class*="slds-button"]',
            'img[role="presentation"]',
        ):
            for element in container.select(selector):
                element.decompose()
        for heading in container.find_all(["h1", "h2", "h3", "h4"]):
            if heading.get_text(" ", strip=True) in {"AI Recommended Content", "Return to Top"}:
                for sibling in list(heading.find_next_siblings()):
                    sibling.decompose()
                heading.decompose()
                break
        for anchor in list(container.find_all("a")):
            if anchor.get_text(" ", strip=True).casefold() != "learn more":
                continue
            card = anchor.find_parent(["section", "li", "div"])
            card_text = card.get_text(" ", strip=True) if card else ""
            if "related content" not in card_text.casefold():
                (card or anchor).decompose()
        ui_patterns = (
            "published date:",
            "updated date:",
            "download article",
            "show social share buttons",
            "toggle showing",
            "applies to:",
            "was this information helpful",
        )
        for element in list(container.find_all(["p", "span", "div"])):
            text = element.get_text(" ", strip=True).casefold()
            if len(text) < 200 and any(pattern in text for pattern in ui_patterns):
                element.decompose()
        return ExtractedPage(
            metadata=self.normalize_metadata(page, str(container)),
            html=str(container),
            candidate_links=candidate_links,
        )

    def normalize_metadata(self, page: FetchResult, html: str) -> PageMetadata:
        soup = BeautifulSoup(page.html, "html.parser")
        text = soup.get_text(" ", strip=True)
        article = re.search(r"K\d{6,}", page.final_url, re.IGNORECASE)
        article_id = article.group(0).upper() if article else "article"
        title_node = soup.select_one("h1")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        content_type = next(
            (item for item in self.document_types if item in text[:2500]), "Knowledge"
        )
        category = re.sub(r"[^a-z0-9]+", "-", content_type.casefold()).strip("-")
        return PageMetadata(
            sourceId=self.source_id,
            title=re.sub(rf"^\s*{re.escape(article_id)}\s*[-:|]?\s*", "", title) or article_id,
            slug=article_id.casefold(),
            url=page.final_url,
            category=category,
            publication_date=self.parse_date(text, "Published Date"),
            modification_date=self.parse_date(text, "Updated Date"),
            tags=["f5-distributed-cloud", category],
        )

    @staticmethod
    def parse_date(text: str, label: str) -> str | None:
        match = re.search(rf"{re.escape(label)}:\s*([A-Za-z]{{3,9}}\s+\d{{1,2}},\s+\d{{4}})", text)
        if not match:
            return None
        for pattern in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(match.group(1), pattern).date().isoformat()
            except ValueError:
                pass
        return None
