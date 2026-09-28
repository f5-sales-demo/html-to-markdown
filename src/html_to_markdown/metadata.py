"""Reviewed deterministic snapshot metadata enrichment."""

from __future__ import annotations

from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from sqlite3 import Row
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .models import ContentType, Lifecycle, PageMetadata, PageStatus, RelatedDocument, TaskType
from .render import serialize_document, split_document
from .state import StateStore
from .urls import canonicalize_url, infer_source, validate_source_url


class ClassificationRule(BaseModel):
    source_id: str
    path_prefix: str
    product: str | None
    content_type: ContentType
    task_type: TaskType

    @field_validator("path_prefix")
    @classmethod
    def normalized_path_prefix(cls, value: str) -> str:
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValueError("path_prefix must be an absolute safe URL path")
        return value.rstrip("/") or "/"


class AliasGroup(BaseModel):
    product: str
    aliases: list[str]

    @field_validator("aliases")
    @classmethod
    def normalized_aliases(cls, value: list[str]) -> list[str]:
        aliases = sorted({item.strip() for item in value if item.strip()}, key=str.casefold)
        if not aliases:
            raise ValueError("alias group cannot be empty")
        return aliases


class TaskPathRule(BaseModel):
    source_id: str
    path_marker: str
    content_type: ContentType
    task_type: TaskType

    @field_validator("path_marker")
    @classmethod
    def normalized_path_marker(cls, value: str) -> str:
        if not value.startswith("/") or not value.endswith("/") or ".." in value.split("/"):
            raise ValueError("path_marker must be a safe slash-delimited path segment")
        return value


class MetadataOverride(BaseModel):
    url: str
    aliases: list[str] = Field(default_factory=list)
    lifecycle: Lifecycle = Lifecycle.CURRENT
    replacement_url: str | None = None

    @field_validator("aliases")
    @classmethod
    def normalized_aliases(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()}, key=str.casefold)

    @model_validator(mode="after")
    def validate_replacement(self) -> MetadataOverride:
        if self.lifecycle == Lifecycle.SUPERSEDED and not self.replacement_url:
            raise ValueError("replacement_url is required for superseded overrides")
        if self.lifecycle != Lifecycle.SUPERSEDED and self.replacement_url is not None:
            raise ValueError("replacement_url is only valid for superseded overrides")
        return self


class CuratedRelationship(BaseModel):
    source_url: str
    target_url: str


class MetadataPolicy(BaseModel):
    schema_version: int
    classification_rules: list[ClassificationRule]
    task_path_rules: list[TaskPathRule] = Field(default_factory=list)
    alias_groups: list[AliasGroup] = Field(default_factory=list)
    overrides: list[MetadataOverride] = Field(default_factory=list)
    relationships: list[CuratedRelationship] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_policy(self) -> MetadataPolicy:
        if self.schema_version != 1:
            raise ValueError("metadata policy schema version must be 1")
        rule_keys: set[tuple[str, str]] = set()
        for rule in self.classification_rules:
            key = (rule.source_id, rule.path_prefix)
            if key in rule_keys:
                raise ValueError(
                    f"conflicting classification rule: {rule.source_id}{rule.path_prefix}"
                )
            rule_keys.add(key)
        aliases: set[str] = set()
        for group in self.alias_groups:
            if group.product in aliases:
                raise ValueError(f"conflicting alias group: {group.product}")
            aliases.add(group.product)
        task_rule_keys: set[tuple[str, str]] = set()
        for task_rule in self.task_path_rules:
            key = (task_rule.source_id, task_rule.path_marker)
            if key in task_rule_keys:
                raise ValueError(
                    f"conflicting task path rule: {task_rule.source_id}{task_rule.path_marker}"
                )
            task_rule_keys.add(key)
        overrides: set[str] = set()
        for override in self.overrides:
            override.url = _policy_url(override.url)
            if override.url in overrides:
                raise ValueError(f"conflicting metadata override: {override.url}")
            overrides.add(override.url)
            if override.replacement_url:
                override.replacement_url = _policy_url(override.replacement_url)
        relationships: set[tuple[str, str]] = set()
        for relationship in self.relationships:
            relationship.source_url = _policy_url(relationship.source_url)
            relationship.target_url = _policy_url(relationship.target_url)
            key = (relationship.source_url, relationship.target_url)
            if relationship.source_url == relationship.target_url:
                raise ValueError("curated relationship cannot target itself")
            if key in relationships:
                raise ValueError(f"conflicting curated relationship: {key}")
            relationships.add(key)
        return self


def _policy_url(value: str) -> str:
    source = infer_source(value)
    if source is None:
        raise ValueError(f"policy URL is outside the corpus: {value}")
    return validate_source_url(source, value)


def load_metadata_policy(path: Path | None = None) -> MetadataPolicy:
    target = path or Path(__file__).with_name("metadata_rules.yaml")
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return MetadataPolicy.model_validate(raw)


def classify_metadata(policy: MetadataPolicy, source_id: str, url: str) -> ClassificationRule:
    path = urlsplit(validate_source_url(source_id, url)).path.rstrip("/") or "/"
    matches = [
        rule
        for rule in policy.classification_rules
        if rule.source_id == source_id
        and (path == rule.path_prefix or path.startswith(f"{rule.path_prefix}/"))
    ]
    if not matches:
        raise ValueError(f"no reviewed classification rule for {source_id}: {path}")
    selected = max(matches, key=lambda rule: len(rule.path_prefix))
    task_matches = [
        rule
        for rule in policy.task_path_rules
        if rule.source_id == source_id and rule.path_marker in f"{path}/"
    ]
    if not task_matches:
        return selected
    task = max(task_matches, key=lambda rule: len(rule.path_marker))
    return ClassificationRule(
        source_id=selected.source_id,
        path_prefix=selected.path_prefix,
        product=selected.product,
        content_type=task.content_type,
        task_type=task.task_type,
    )


def normalize_source_date(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    try:
        return date.fromisoformat(candidate[:10]).isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(candidate).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _accepted(row: Row) -> bool:
    return PageStatus(row["status"]) in {
        PageStatus.SUCCESS,
        PageStatus.CARRIED_FORWARD,
        PageStatus.REMOVAL_CANDIDATE,
    }


# The enrichment transaction intentionally retains the complete inventory and policy indexes.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def enrich_snapshot(output: Path, store: StateStore, policy: MetadataPolicy | None = None) -> None:
    """Apply reviewed metadata only after the complete snapshot inventory exists."""
    active_policy = policy or load_metadata_policy()
    rows = [row for row in store.rows() if _accepted(row)]
    documents: dict[str, tuple[Path, PageMetadata, str, Row]] = {}
    for row in rows:
        url = validate_source_url(str(row["source"]), str(row["canonical_url"]))
        path = output / str(row["output_path"])
        raw_metadata, body = split_document(path.read_text(encoding="utf-8"))
        metadata = PageMetadata.model_validate(raw_metadata)
        documents[url] = (path, metadata, body, row)

    alias_groups = {group.product: group.aliases for group in active_policy.alias_groups}
    overrides = {override.url: override for override in active_policy.overrides}
    curated: dict[str, set[str]] = {}
    for relationship in active_policy.relationships:
        curated.setdefault(relationship.source_url, set()).add(relationship.target_url)

    canonical_groups: dict[str, list[str]] = {}
    for url, (_, metadata, _, _) in sorted(documents.items()):
        canonical = validate_source_url(metadata.source_id, metadata.canonical_url or url)
        canonical_groups.setdefault(canonical, []).append(url)
    canonical_documents = {
        canonical: urls[0] for canonical, urls in canonical_groups.items() if len(urls) == 1
    }
    classifications = {
        url: classify_metadata(
            active_policy,
            metadata.source_id,
            validate_source_url(metadata.source_id, metadata.canonical_url or url),
        )
        for url, (_, metadata, _, _) in documents.items()
    }
    for override in active_policy.overrides:
        if override.url not in canonical_documents:
            continue
        if override.replacement_url and override.replacement_url not in canonical_documents:
            raise ValueError(f"replacement target is not in snapshot: {override.replacement_url}")
    for relationship in active_policy.relationships:
        if (
            relationship.source_url in canonical_documents
            and relationship.target_url not in canonical_documents
        ):
            raise ValueError(
                "curated relationship target is not in snapshot: "
                f"{relationship.source_url} -> {relationship.target_url}"
            )

    enriched: dict[str, PageMetadata] = {}
    for url, (_, metadata, _, row) in documents.items():
        rule = classifications[url]
        canonical_url = validate_source_url(metadata.source_id, metadata.canonical_url or url)
        document_override = overrides.get(canonical_url)
        metadata.metadata_schema = 1
        metadata.product = rule.product
        metadata.content_type = rule.content_type
        metadata.task_type = rule.task_type
        metadata.canonical_url = canonicalize_url(canonical_url)
        metadata.last_updated = (
            normalize_source_date(metadata.modification_date)
            or normalize_source_date(str(row["source_last_modified"] or ""))
            or normalize_source_date(metadata.publication_date)
        )
        metadata.language = "en"
        aliases = list(alias_groups.get(rule.product or "", []))
        if document_override:
            aliases.extend(document_override.aliases)
            metadata.lifecycle = document_override.lifecycle
            metadata.replacement_url = document_override.replacement_url
        else:
            metadata.lifecycle = Lifecycle.CURRENT
            metadata.replacement_url = None
        metadata.aliases = aliases
        enriched[url] = metadata

    for url, metadata in enriched.items():
        canonical_url = metadata.canonical_url or url
        targets = set(store.candidate_links(url)) | curated.get(canonical_url, set())
        related: list[RelatedDocument] = []
        for target_url in targets:
            try:
                source_id = infer_source(target_url)
                canonical_target = (
                    validate_source_url(source_id, target_url) if source_id is not None else None
                )
            except ValueError:
                continue
            target_key = canonical_documents.get(canonical_target or "")
            if canonical_target in enriched:
                redirected = enriched[canonical_target].canonical_url or canonical_target
                if canonical_documents.get(redirected) == canonical_target:
                    target_key = canonical_target
            if canonical_target is None or canonical_target == canonical_url or target_key is None:
                continue
            target_path, _, _, _ = documents[target_key]
            target = enriched[target_key]
            if target.task_type is None:
                raise ValueError(f"related target has no task type: {canonical_target}")
            related.append(
                RelatedDocument(
                    relation=target.task_type,
                    title=target.title,
                    canonical_url=target.canonical_url or canonical_target,
                    source_id=target.source_id,
                    stable_path=target_path.relative_to(
                        output / "content" / target.source_id
                    ).parent.as_posix(),
                )
            )
        metadata.related_documents = related
        enriched[url] = PageMetadata.model_validate(metadata.model_dump(mode="json"))

    for url, (path, _, body, _) in documents.items():
        path.write_text(serialize_document(enriched[url], body), encoding="utf-8", newline="\n")
