"""Typed source adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from html_to_markdown.models import DiscoveredPage, ExtractedPage, FetchResult, PageMetadata

if TYPE_CHECKING:
    from html_to_markdown.fetcher import Fetcher


class SourceAdapter(ABC):
    source_id: str
    root_url: str
    browser_only: bool = False

    @abstractmethod
    async def discover(self, fetcher: Fetcher) -> list[DiscoveredPage]: ...

    @abstractmethod
    def readiness_script(self) -> str | None: ...

    @abstractmethod
    def classify_page(self, page: FetchResult) -> None: ...

    @abstractmethod
    async def rendered_html(self, playwright_page: Any) -> str: ...

    @abstractmethod
    def extract(self, page: FetchResult) -> ExtractedPage: ...

    @abstractmethod
    def normalize_metadata(self, page: FetchResult, html: str) -> PageMetadata: ...

    def needs_browser(self, html: str) -> bool:
        return self.browser_only or len(html) < 500
