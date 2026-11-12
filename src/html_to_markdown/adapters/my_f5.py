"""Adapter for the Salesforce Lightning site at my.f5.com/manage/s."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from html_to_markdown.errors import AuthenticationWallError, NotFoundError, ScrapeError
from html_to_markdown.models import DiscoveredPage, ExtractedPage, FetchResult, PageMetadata

from .base import SourceAdapter


class MyF5Adapter(SourceAdapter):
    source_id = "my-f5-com"
    root_url = "https://my.f5.com/manage/s"
    browser_only = True
    document_types = ("Support Solution", "Operations Guide", "Knowledge", "Policy")
    search_terms = ("F5 Distributed Cloud", "Volterra", "VoltMesh", "VoltStack")
    pagination_limit = 100

    async def discover(self, fetcher: Any) -> list[DiscoveredPage]:
        filters = ",".join(self.document_types)
        found: set[str] = set()
        for term in self.search_terms:
            search = (
                f"{self.root_url}/global-search/%40uri#q={quote(term)}"
                f"&f-f5_document_type={filters}&aq=%40f5_archived&numberOfResults=100"
            )
            async with fetcher.browser_page() as page:
                await page.goto(search, wait_until="domcontentloaded")
                await page.wait_for_function(
                    """() => /K\\d{6,}/.test(document.body?.innerText || '') ||
                      /no results/i.test(document.body?.innerText || '')"""
                )
                found.update(await self._search_pages(page, fetcher, search))
        return [DiscoveredPage(source_id=self.source_id, url=url) for url in sorted(found)]

    async def _search_pages(self, page: Any, fetcher: Any, origin: str) -> set[str]:
        found: set[str] = set()
        seen: set[tuple[str, ...]] = set()
        record = getattr(fetcher, "record_discovery", None)
        for number in range(self.pagination_limit):
            # Read the newly rendered page BEFORE inspecting its terminal control.
            text = await page.locator("body").inner_text()
            ids = tuple(sorted(set(re.findall(r"\bK\d{6,}\b", text))))
            if not ids and (number or not re.search(r"\bno results\b", text, re.I)):
                raise ScrapeError("MyF5 pagination returned unexpected empty results")
            if ids in seen:
                raise ScrapeError("MyF5 pagination loop repeated a result page")
            seen.add(ids)
            urls = [f"{self.root_url}/article/{identifier}" for identifier in ids]
            found.update(urls)
            if record is not None:
                record(source=self.source_id, origin=origin, urls=urls, page=number, complete=False)
            next_button = page.locator(
                'button[aria-label^="Next" i], button[title^="Next" i], button:has-text("Next")'
            ).last
            count = await next_button.count()
            terminal = not ids
            if count:
                terminal = terminal or await next_button.is_disabled()
                terminal = terminal or await next_button.get_attribute("aria-disabled") == "true"
                terminal = terminal or bool(
                    re.search(r"\bdisabled\b", await next_button.get_attribute("class") or "")
                )
            elif ids:
                summary = re.search(
                    r"(?:results?\s+)?([\d,]+)\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)", text, re.I
                )
                terminal = bool(
                    summary and int(summary[2].replace(",", "")) == int(summary[3].replace(",", ""))
                )
                if not terminal:
                    raise ScrapeError("MyF5 pagination has no completion evidence")
            if terminal:
                if record is not None:
                    record(
                        source=self.source_id, origin=origin, urls=urls, page=number, complete=True
                    )
                return found
            await next_button.click()
            try:
                await page.wait_for_function(
                    "before => { const text = document.body?.innerText || ''; "
                    "const ids = t => JSON.stringify([...new Set(t.match(/\\bK\\d{6,}\\b/g) || [])].sort()); "
                    "return ids(text) !== ids(before) && !document.querySelector('[aria-busy=\"true\"]') "
                    "&& (/K\\d{6,}/.test(text) || /no results/i.test(text)); }",
                    arg=text,
                )
            except Exception as error:
                raise ScrapeError("MyF5 pagination result change was not observed") from error
        raise ScrapeError("MyF5 pagination exhausted its limit without completion")

    def readiness_script(self) -> str | None:
        return """() => {
          const flatten = node => {
            const clone = node.cloneNode(false);
            for (const child of (node.shadowRoot || node).childNodes)
              clone.appendChild(child.nodeType === Node.ELEMENT_NODE ? flatten(child) : child.cloneNode(true));
            return clone;
          };
          const root = document.querySelector('c-site-article-detail-container, article');
          if (!root) return false;
          const clone = flatten(root);
          if (clone.querySelector('[aria-busy="true"], lightning-spinner, .slds-spinner, [data-loading="true"]')) return false;
          if (!clone.querySelector('h1')?.textContent.trim()) return false;
          const markers = ['Answer', 'Cause', 'Resolution', 'Procedure', 'Description', 'What Happened'];
          const headings = [...clone.querySelectorAll('h2,h3,h4,[role="heading"]')];
          const primary = headings.filter(h => markers.some(m => h.textContent.trim().startsWith(m)));
          const content = [...clone.querySelectorAll('p,li,pre,table,code')]
            .filter(node => !node.closest('nav,footer,aside,[role="navigation"]'));
          const level = h => Number(h.getAttribute('aria-level') || h.tagName.slice(1)) || 3;
          // Lightning can render intentionally empty Description/Cause labels
          // beside populated authored fields, including same-level descendants.
          // Read the scoped fields without requiring every label to have content.
          const fields = [...clone.querySelectorAll('.article-text-block.slds-rich-text-editor__output, .article-text-block .slds-rich-text-editor__output')];
          if (fields.length) return fields.some(field => [...field.querySelectorAll('p,li,pre,table,code')]
            .some(node => node.textContent.trim() && !node.closest('nav,footer,aside,[role="navigation"]')));
          if (primary.length === 0) return false;
          return primary.every(h => {
            const next = headings.slice(headings.indexOf(h) + 1).find(n => level(n) <= level(h));
            return content.some(node => node.textContent.trim() &&
              (h.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) &&
              (!next || (next.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_PRECEDING)));
          });
        }"""

    def classify_page(self, page: FetchResult) -> None:
        soup = BeautifulSoup(page.html, "html.parser")
        text = soup.get_text(" ", strip=True).casefold()
        article = soup.select_one("c-site-article-detail-container, article")
        structured = (
            article is not None
            and article.select_one("h1") is not None
            and (
                any(
                    marker in article.get_text(" ", strip=True).casefold()
                    for marker in (
                        "answer",
                        "environment",
                        "resolution",
                        "procedure",
                        "description",
                        "cause",
                    )
                )
                or any(
                    field.get_text(" ", strip=True)
                    for field in article.select(
                        ".article-text-block.slds-rich-text-editor__output, .article-text-block .slds-rich-text-editor__output"
                    )
                )
            )
        )
        if page.status_code in {401, 403} or (
            not structured
            and any(term in text[:1600] for term in ("log in", "sign in", "forgot your password"))
        ):
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
        candidate_links = [str(anchor["href"]) for anchor in container.select("a[href]")]
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
