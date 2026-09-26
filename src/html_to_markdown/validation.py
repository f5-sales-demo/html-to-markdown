"""Document and complete-snapshot validation."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import PublicationBlockedError, ValidationError
from .models import PageStatus
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
    if not isinstance(tags, list) or tags != sorted(
        set(str(tag) for tag in tags), key=str.casefold
    ):
        errors.append(f"{path}: tags are not sorted and deduplicated")
    chrome = re.findall(
        r"(?im)^(?:.*(?:Return to Top|Show social share buttons|Cookie Settings).*)"
        r"|(?:\s*Select Service\s*)$",
        body,
    )
    if chrome:
        errors.append(f"{path}: site chrome remains")
    for target in re.findall(r"!\[[^]]*]\(([^)]+)\)", body):
        if (
            not target.startswith(("http://", "https://"))
            and not (path.parent / target).resolve().is_file()
        ):
            errors.append(f"{path}: broken local asset: {target}")
    return errors


def validate_snapshot(
    output: Path,
    store: StateStore,
    *,
    previous_manifest: Path | None = None,
    acknowledge_page_drop: bool = False,
) -> None:
    errors = [
        error for path in output.glob("content/*/**/index.md") for error in validate_document(path)
    ]
    rows = store.rows()
    blocked = [
        row
        for row in rows
        if PageStatus(row["status"])
        not in {PageStatus.SUCCESS, PageStatus.REMOVED_NOT_FOUND, PageStatus.REMOVED_NAVIGATION}
    ]
    if blocked:
        errors.append(f"{len(blocked)} pages have unclassified or failed status")
    if previous_manifest and previous_manifest.exists() and not acknowledge_page_drop:
        previous = json.loads(previous_manifest.read_text(encoding="utf-8"))
        prior_count = int(previous.get("page_count", 0))
        current_count = sum(1 for row in rows if row["status"] == PageStatus.SUCCESS)
        if prior_count and current_count < prior_count * 0.95:
            errors.append(f"page count dropped from {prior_count} to {current_count} (>5%)")
    if errors:
        raise PublicationBlockedError("snapshot validation failed:\n- " + "\n- ".join(errors))


def require_valid_document(path: Path) -> None:
    errors = validate_document(path)
    if errors:
        raise ValidationError("\n".join(errors))
