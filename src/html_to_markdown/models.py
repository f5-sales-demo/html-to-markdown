"""Typed contracts shared by adapters and the pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContentType(StrEnum):
    PRODUCT_OVERVIEW = "product_overview"
    SOLUTION_OVERVIEW = "solution_overview"
    CONCEPT = "concept"
    HOW_TO = "how_to"
    REFERENCE = "reference"
    KNOWLEDGE_ARTICLE = "knowledge_article"


class TaskType(StrEnum):
    CONCEPT = "concept"
    CONFIGURE = "configure"
    TROUBLESHOOT = "troubleshoot"
    SUPPORT = "support"
    REFERENCE = "reference"


class Lifecycle(StrEnum):
    CURRENT = "current"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class RelatedDocument(BaseModel):
    relation: TaskType
    title: str
    canonical_url: str
    source_id: str
    stable_path: str


class PageStatus(StrEnum):
    DISCOVERED = "discovered"
    FETCHING = "fetching"
    SUCCESS = "success"
    FAILED = "failed"
    REMOVED_NOT_FOUND = "removed_not_found"
    REMOVED_NAVIGATION = "removed_navigation"
    AUTHENTICATION_WALL = "authentication_wall"
    CARRIED_FORWARD = "carried_forward"
    UNAVAILABLE = "unavailable"
    REMOVAL_CANDIDATE = "removal_candidate"
    CONFIRMED_REMOVAL = "confirmed_removal"

    @property
    def terminal_removal(self) -> bool:
        return self in {
            self.REMOVED_NOT_FOUND,
            self.REMOVED_NAVIGATION,
            self.CONFIRMED_REMOVAL,
        }


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
    metadata_schema: int = 1
    product: str | None = None
    content_type: ContentType | None = None
    task_type: TaskType | None = None
    canonical_url: str | None = None
    last_updated: str | None = None
    language: str = "en"
    aliases: list[str] = Field(default_factory=list)
    lifecycle: Lifecycle = Lifecycle.CURRENT
    replacement_url: str | None = None
    related_documents: list[RelatedDocument] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def sorted_tags(cls, value: list[str]) -> list[str]:
        return sorted({tag.strip() for tag in value if tag.strip()}, key=str.casefold)

    @field_validator("aliases")
    @classmethod
    def sorted_aliases(cls, value: list[str]) -> list[str]:
        unique = {alias.strip(): None for alias in value if alias.strip()}
        return sorted(unique, key=lambda alias: (alias.casefold(), alias))

    @field_validator("related_documents")
    @classmethod
    def sorted_related_documents(cls, value: list[RelatedDocument]) -> list[RelatedDocument]:
        unique = {item.canonical_url: item for item in value}
        return sorted(
            unique.values(),
            key=lambda item: (
                item.relation.value,
                item.title.casefold(),
                item.canonical_url,
                item.source_id,
                item.stable_path,
            ),
        )

    @model_validator(mode="after")
    def validate_lifecycle(self) -> PageMetadata:
        if self.lifecycle == Lifecycle.SUPERSEDED and not self.replacement_url:
            raise ValueError("replacement_url is required for superseded documents")
        if self.lifecycle != Lifecycle.SUPERSEDED and self.replacement_url is not None:
            raise ValueError("replacement_url is only valid for superseded documents")
        return self


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
