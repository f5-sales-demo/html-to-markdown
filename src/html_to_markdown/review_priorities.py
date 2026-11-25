"""Private scheduling hints never grant source admission or editorial allowance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .enrichment.analysis import sha

FILENAME = "review-priorities.private.json"
IDENTITY = re.compile(
    r"(?:docs-cloud-f5-com|community-f5-com|my-f5-com|www-f5-com):[A-Za-z0-9][A-Za-z0-9._/%-]*"
)
HOSTS = {"docs.cloud.f5.com", "community.f5.com", "my.f5.com", "www.f5.com"}


def _safe_file(root: Path, relative: str) -> Path:
    from .reference_repair_inputs import safe_file  # pylint: disable=import-outside-toplevel

    return safe_file(root, relative)


@dataclass(frozen=True)
class ReviewPriorities:
    identities: frozenset[str] = frozenset()
    urls: frozenset[str] = frozenset()
    sha256: str | None = None

    def key(self, item: dict[str, Any]) -> tuple[bool, bool, str]:
        return (
            item["identity"] not in self.identities and item["url"] not in self.urls,
            "previous_publication" not in item.get("origins", []),
            item["url"],
        )


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate review priority input key")
        result[key] = value
    return result


def _values(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("review priorities must be string lists")
    if len(set(value)) != len(value):
        raise ValueError("duplicate review priority value")
    return value


def _public_url(url: str) -> bool:
    parsed = urlsplit(url)
    decoded = unquote(parsed.path)
    if parsed.scheme != "https" or parsed.netloc not in HOSTS or not parsed.path:
        return False
    if parsed.query or parsed.fragment or "\\" in decoded or ".." in decoded.split("/"):
        return False
    return not any(ord(character) < 33 or character in "?:@#" for character in decoded)


def load_priorities(path: Path, *, expected_sha256: str | None = None) -> ReviewPriorities:
    if path.is_symlink():
        raise ValueError("review priority input cannot be a link")
    if not path.exists():
        if expected_sha256 is not None:
            raise ValueError("pinned review priority input is missing")
        return ReviewPriorities()
    checked = _safe_file(path.parent, path.name)
    if checked.stat().st_size > 65536:
        raise ValueError("review priority input exceeds size bound")
    payload = checked.read_bytes()
    if len(payload) > 65536:
        raise ValueError("review priority input exceeds size bound")
    digest = sha(payload)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("review priority input digest mismatch")
    return parse_priorities(payload)


def parse_priorities(payload: bytes) -> ReviewPriorities:
    """Validate a bounded scheduling payload without writing or changing review evidence."""
    if len(payload) > 65536:
        raise ValueError("review priority input exceeds size bound")
    digest = sha(payload)
    try:
        data = json.loads(payload, object_pairs_hook=_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise ValueError("invalid review priority JSON") from failure
    if not isinstance(data, dict) or set(data) != {"schema_version", "identities", "urls"}:
        raise ValueError("invalid review priority schema")
    version = data["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("unsupported review priority schema version")
    identities, urls = _values(data["identities"]), _values(data["urls"])
    for identity in identities:
        decoded_identity = unquote(identity.split(":", 1)[-1])
        if (
            not IDENTITY.fullmatch(identity)
            or ".." in decoded_identity.split("/")
            or any(ord(character) < 33 or character in "\\?:@#" for character in decoded_identity)
        ):
            raise ValueError("invalid review priority identity")
    for url in urls:
        if not _public_url(url):
            raise ValueError("review priority URL must be a canonical public source URL")
    return ReviewPriorities(frozenset(identities), frozenset(urls), digest)


def select_records(
    output: Path, limit: int, priorities: ReviewPriorities | None = None
) -> list[Path]:
    if limit < 1:
        raise ValueError("review batches require a positive limit")
    order = priorities or ReviewPriorities()
    inventory = json.loads(_safe_file(output, "review-inventory.json").read_bytes())
    eligible = []
    for entry in inventory["documents"]:
        if "path" not in entry:
            continue
        path = _safe_file(output, entry["path"])
        saved = json.loads(path.read_bytes())
        if saved["disposition"] not in {"accepted", "policy_excluded"} and (
            saved.get("attempted_binding_sha256") != saved["binding_sha256"]
            or saved.get("retryable")
            or saved.get("cross_queue_storage_retryable")
        ):
            eligible.append((*order.key(saved["item"]), path))
    return [entry[-1] for entry in sorted(eligible)[:limit]]
