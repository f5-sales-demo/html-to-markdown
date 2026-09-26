"""Typed contracts shared by adapters and the pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PageStatus(StrEnum):
    DISCOVERED = "discovered"
    FETCHING = "fetching"
    SUCCESS = "success"
    FAILED = "failed"
    REMOVED_NOT_FOUND = "removed_not_found"
    REMOVED_NAVIGATION = "removed_navigation"
    AUTHENTICATION_WALL = "authentication_wall"

    @property
    def terminal_removal(self) -> bool:
        return self in {self.REMOVED_NOT_FOUND, self.REMOVED_NAVIGATION}


class PageMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_id: str = Field(alias="sourceId")
    title: str
    slug: str
    url: str
    category: str
    publication_date: str | None = None
    modification_date: str | None = None
    content_hash: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    subcategory: str | None = None
    breadcrumb: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def sorted_tags(cls, value: list[str]) -> list[str]:
        return sorted({tag.strip() for tag in value if tag.strip()}, key=str.casefold)


class DiscoveredPage(BaseModel):
    source_id: str
    url: str
    source_last_modified: str | None = None


class ExtractedPage(BaseModel):
    metadata: PageMetadata
    html: str


class AssetReference(BaseModel):
    url: str
    placeholder: str
    alt: str = ""


class RenderedPage(BaseModel):
    body: str
    assets: list[AssetReference] = Field(default_factory=list)


class FetchResult(BaseModel):
    url: str
    final_url: str
    status_code: int
    html: str
    headers: dict[str, str] = Field(default_factory=dict)


class DocumentRecord(BaseModel):
    source_id: str
    url: str
    output_path: Path
    content_hash: str
    asset_hashes: list[str] = Field(default_factory=list)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
