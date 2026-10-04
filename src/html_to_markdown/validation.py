"""Document and complete-snapshot validation."""

from __future__ import annotations

import re
from pathlib import Path

from .errors import AllowlistError, PublicationBlockedError, ValidationError
from .models import Lifecycle, PageMetadata
from .render import content_hash, split_document
from .state import StateStore
from .urls import validate_source_url

REQUIRED_FIELDS = (
    "sourceId",
    "title",
    "slug",
    "url",
    "category",
    "publication_date",
    "modification_date",
    "content_hash",
    "tags",
)

ENRICHED_FIELDS = (
    "metadata_schema",
    "product",
    "content_type",
    "task_type",
    "canonical_url",
    "last_updated",
    "language",
    "aliases",
    "lifecycle",
    "replacement_url",
    "related_documents",
)

LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
KnownDocument = tuple[str, str, str, str]


# Keep all public metadata invariants in one audit so callers cannot omit a subset.
# pylint: disable-next=too-many-branches
def validate_enriched_metadata(
    metadata: dict[str, object], label: str, known_urls: set[str] | None = None
) -> list[str]:
    errors: list[str] = []
    missing = [field for field in ENRICHED_FIELDS if field not in metadata]
    if missing:
        return [f"{label}: missing enriched fields: {', '.join(missing)}"]
    try:
        parsed = PageMetadata.model_validate(metadata)
    except ValueError as error:
        return [f"{label}: invalid enriched metadata: {error}"]
    if parsed.metadata_schema != 1:
        errors.append(f"{label}: metadata_schema must be 1")
    if parsed.content_type is None or parsed.task_type is None:
        errors.append(f"{label}: content_type and task_type are required")
    if not LANGUAGE_PATTERN.fullmatch(parsed.language):
        errors.append(f"{label}: language is not a normalized BCP-47 tag")
    if parsed.canonical_url is None:
        errors.append(f"{label}: canonical_url is required")
    else:
        try:
            canonical = validate_source_url(parsed.source_id, parsed.canonical_url)
            if canonical != parsed.canonical_url:
                errors.append(f"{label}: canonical_url is not normalized")
        except (AllowlistError, ValueError) as error:
            errors.append(f"{label}: invalid canonical_url: {error}")
    if parsed.last_updated is not None and not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", parsed.last_updated
    ):
        errors.append(f"{label}: last_updated must be YYYY-MM-DD or null")
    if (
        parsed.lifecycle == Lifecycle.SUPERSEDED
        and known_urls is not None
        and parsed.replacement_url not in known_urls
    ):
        errors.append(f"{label}: replacement_url is outside the snapshot")
    for related in parsed.related_documents:
        if known_urls is not None and related.canonical_url not in known_urls:
            errors.append(
                f"{label}: related target is outside the snapshot: {related.canonical_url}"
            )
        if any(part in {"", ".", ".."} for part in related.stable_path.split("/")):
            errors.append(f"{label}: related stable_path is unsafe: {related.stable_path}")
        if related.canonical_url == parsed.canonical_url:
            errors.append(f"{label}: related document cannot target itself")
    return errors


def validate_related_targets(
    metadata: PageMetadata,
    label: str,
    known_documents: dict[str, KnownDocument],
) -> list[str]:
    errors: list[str] = []
    for related in metadata.related_documents:
        target = known_documents.get(related.canonical_url)
        if target != (
            related.source_id,
            related.stable_path,
            related.title,
            related.relation.value,
        ):
            errors.append(f"{label}: related target metadata mismatch: {related.canonical_url}")
    return errors


def validate_document(
    path: Path, *, require_enriched: bool = False, known_urls: set[str] | None = None
) -> list[str]:
    errors: list[str] = []
    try:
        metadata, body = split_document(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"{path}: {error}"]
    missing = [field for field in REQUIRED_FIELDS if field not in metadata]
    if missing:
        errors.append(f"{path}: missing fields: {', '.join(missing)}")
    if require_enriched:
        errors.extend(validate_enriched_metadata(metadata, str(path), known_urls))
    if metadata.get("content_hash") != content_hash(body):
        errors.append(f"{path}: content_hash mismatch")
    tags = metadata.get("tags")
    sorted_tags: list[str] = []
    if isinstance(tags, list):
        sorted_tags = sorted(set(str(tag) for tag in tags), key=str.casefold)
    if not isinstance(tags, list) or tags != sorted_tags:
        errors.append(f"{path}: tags are not sorted and deduplicated")
    chrome = re.findall(
        r"(?im)^(?:.*(?:Return to Top|Show social share buttons|Cookie Settings).*)"
        r"|(?:\s*Select Service\s*)$",
        body,
    )
    if chrome:
        errors.append(f"{path}: site chrome remains")
    for target in re.findall(
        r"""!\[[^]]*]\((<[^>]+>|[^\s)]+)(?:\s+(?:"[^"]*"|'[^']*'))?\)""", body
    ):
        # Markdown permits a quoted title after the image destination.
        target = re.sub(r"""\s+([\"']).*\1$""", "", target).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        is_remote = target.startswith(("http://", "https://"))
        asset_exists = (path.parent / target).resolve().is_file()
        if not is_remote and not asset_exists:
            errors.append(f"{path}: broken local asset: {target}")
    return errors


def validate_snapshot(
    output: Path,
    store: StateStore,
) -> None:
    """Validate only artifact integrity; crawl and quality findings are advisory."""
    errors: list[str] = []
    paths = sorted(output.glob("content/*/**/index.md"))
    known_urls: set[str] = set()
    known_documents: dict[str, KnownDocument] = {}
    for path in paths:
        try:
            metadata, _ = split_document(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        canonical = metadata.get("canonical_url")
        if isinstance(canonical, str):
            known_urls.add(canonical)
            source_id = metadata.get("sourceId")
            title = metadata.get("title")
            task_type = metadata.get("task_type")
            if all(isinstance(value, str) for value in (source_id, title, task_type)):
                stable = path.relative_to(output / "content" / str(source_id)).parent.as_posix()
                known_documents[canonical] = (str(source_id), stable, str(title), str(task_type))
    for path in paths:
        errors.extend(validate_document(path, require_enriched=True, known_urls=known_urls))
        try:
            metadata, _ = split_document(path.read_text(encoding="utf-8"))
            parsed = PageMetadata.model_validate(metadata)
        except (OSError, ValueError):
            continue
        errors.extend(validate_related_targets(parsed, str(path), known_documents))
    if errors:
        raise PublicationBlockedError("snapshot validation failed:\n- " + "\n- ".join(errors))


def require_valid_document(path: Path) -> None:
    errors = validate_document(path)
    if errors:
        raise ValidationError("\n".join(errors))
