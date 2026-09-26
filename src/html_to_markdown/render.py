"""Deterministic HTML cleanup, Markdown rendering, and frontmatter."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from .models import AssetReference, PageMetadata, RenderedPage

FRONTMATTER_ORDER = (
    "sourceId",
    "title",
    "slug",
    "url",
    "category",
    "publication_date",
    "modification_date",
    "content_hash",
    "tags",
    "description",
    "subcategory",
    "breadcrumb",
)

CHROME_SELECTORS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "form",
    "button",
    '[role="navigation"]',
    '[role="search"]',
    '[aria-label*="cookie" i]',
    '[class*="cookie" i]',
    '[class*="share" i]',
    '[class*="recommend" i]',
    '[class*="search" i]',
    ".breadcrumbs + aside",
)


class DeterministicConverter(MarkdownConverter):
    """Markdownify with stable fenced code and callout behavior."""

    def convert_pre(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        candidate = el.find("code")
        code = candidate if isinstance(candidate, Tag) else None
        raw = code.get_text() if code else el.get_text()
        language = ""
        classes = code.get("class", []) if code else []
        for value in classes:
            if str(value).startswith(("language-", "lang-")):
                language = str(value).split("-", 1)[1]
                break
        return f"\n```{language}\n{raw.rstrip()}\n```\n"

    def convert_div(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        classes = " ".join(str(item) for item in el.get("class", []))
        if re.search(r"callout|admonition|alert", classes, re.IGNORECASE):
            lines = [f"> {line}" if line else ">" for line in text.strip().splitlines()]
            return "\n" + "\n".join(lines) + "\n"
        return text


def normalize_body(body: str) -> str:
    value = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.splitlines()]
    value = "\n".join(lines)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return f"{value}\n"


def content_hash(body: str) -> str:
    return hashlib.sha256(normalize_body(body).encode()).hexdigest()


def render_html(html: str, base_url: str) -> RenderedPage:
    soup = BeautifulSoup(html, "html.parser")
    for selector in CHROME_SELECTORS:
        for element in soup.select(selector):
            element.decompose()
    assets: list[AssetReference] = []
    for index, image in enumerate(soup.find_all("img")):
        if not isinstance(image, Tag):
            continue
        raw_source = image.get("src") or image.get("data-src")
        source = str(raw_source) if raw_source else ""
        if not source or source.startswith("data:") or image.get("role") == "presentation":
            image.decompose()
            continue
        absolute = urljoin(base_url, source)
        placeholder = f"asset://{index}"
        image["src"] = placeholder
        raw_alt = image.get("alt", "")
        assets.append(AssetReference(url=absolute, placeholder=placeholder, alt=str(raw_alt)))
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if href:
            anchor["href"] = urljoin(base_url, href)
    markdown = DeterministicConverter(
        heading_style="ATX", bullets="-", strong_em_symbol="*", strip=["span"]
    ).convert_soup(soup)
    return RenderedPage(body=normalize_body(markdown), assets=assets)


def serialize_document(metadata: PageMetadata, body: str) -> str:
    normalized = normalize_body(body)
    metadata.content_hash = content_hash(normalized)
    raw = metadata.model_dump(by_alias=True)
    ordered = {
        key: raw[key]
        for key in FRONTMATTER_ORDER
        if raw.get(key) is not None or key in {"publication_date", "modification_date", "tags"}
    }
    frontmatter = yaml.safe_dump(
        ordered, allow_unicode=True, default_flow_style=False, sort_keys=False, width=1000
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{normalized}"


def split_document(document: str) -> tuple[dict[str, object], str]:
    match = re.match(r"^---\n(.*?)\n---\n\n?(.*)$", document, re.DOTALL)
    if not match:
        raise ValueError("document has no YAML frontmatter")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a mapping")
    return data, normalize_body(match.group(2))
