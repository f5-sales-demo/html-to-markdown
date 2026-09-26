"""Document and complete-snapshot validation."""

from __future__ import annotations

import re
from pathlib import Path

from .errors import PublicationBlockedError, ValidationError
from .render import content_hash, split_document
from .state import StateStore

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


def validate_document(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        metadata, body = split_document(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"{path}: {error}"]
    missing = [field for field in REQUIRED_FIELDS if field not in metadata]
    if missing:
        errors.append(f"{path}: missing fields: {', '.join(missing)}")
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
    for target in re.findall(r"!\[[^]]*]\(([^)]+)\)", body):
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
    for path in output.glob("content/*/**/index.md"):
        errors.extend(validate_document(path))
    if errors:
        raise PublicationBlockedError("snapshot validation failed:\n- " + "\n- ".join(errors))


def require_valid_document(path: Path) -> None:
    errors = validate_document(path)
    if errors:
        raise ValidationError("\n".join(errors))
