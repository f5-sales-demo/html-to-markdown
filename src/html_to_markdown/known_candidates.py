"""Pinned historical discovery inputs; they confer no publication eligibility."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .curation import json_bytes
from .enrichment.analysis import sha
from .errors import AllowlistError
from .private_storage import atomic_write
from .urls import SOURCE_ROOTS, validate_source_url


def discovery_time(value: Any) -> datetime | None:
    """Require an explicit timezone for preserved discovery timestamps."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("known candidate discovery date is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as failure:
        raise ValueError("known candidate discovery date is invalid") from failure
    if result.tzinfo is None:
        raise ValueError("known candidate discovery date needs a timezone")
    return result.astimezone(UTC)


def _candidate(item: Any, selected: str) -> dict[str, Any]:
    if not isinstance(item, dict) or item.get("sourceId") not in SOURCE_ROOTS:
        raise ValueError("known candidate source is invalid")
    source = item["sourceId"]
    if selected != "all" and source != selected:
        raise ValueError("known candidate source binding differs")
    if not isinstance(item.get("url"), str):
        raise ValueError("known candidate URL is invalid")
    raw_url = urlsplit(item["url"])
    if (
        raw_url.username
        or raw_url.password
        or raw_url.netloc.lower() != raw_url.hostname
        or raw_url.fragment
        or any(part in {".", ".."} for part in unquote(raw_url.path).split("/"))
    ):
        raise ValueError("known candidate URL authority or path is unsafe")
    try:
        url = validate_source_url(source, item["url"])
    except (AllowlistError, ValueError) as failure:
        raise ValueError("known candidate URL is outside its source") from failure
    observed = discovery_time(item.get("discovered_at"))
    return {
        "sourceId": source,
        "url": url,
        "discovered_at": observed.isoformat().replace("+00:00", "Z") if observed else None,
    }


def parse_known_candidates(raw: bytes, source: str) -> list[dict[str, Any]]:
    inventory = json.loads(raw)
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 1:
        raise ValueError("known candidate inventory schema is invalid")
    if inventory.get("source") != source or not isinstance(inventory.get("documents"), list):
        raise ValueError("known candidate inventory source binding differs")
    documents = [_candidate(item, source) for item in inventory["documents"]]
    identities = [(item["sourceId"], item["url"]) for item in documents]
    if len(set(identities)) != len(identities):
        raise ValueError("known candidate inventory has duplicate identities")
    return documents


def preserved_known_candidates(
    output: Path, source: str, digest: str
) -> tuple[bytes, list[dict[str, Any]]]:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("known candidate input pin is invalid")
    path = output / "known-candidates" / (digest + ".json")
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ValueError("known candidate input path is unsafe")
    raw = path.read_bytes()
    if sha(raw) != digest:
        raise ValueError("known candidate preserved input digest changed")
    return raw, parse_known_candidates(raw, source)


def prepare_known_candidates(
    output: Path, source: str, supplied: Path | None, *, resume: bool, expected: str | None
) -> tuple[str | None, list[dict[str, Any]]]:
    if supplied is None:
        if not expected:
            return None, []
        _, documents = preserved_known_candidates(output, source, expected)
        return expected, documents
    raw = supplied.read_bytes()
    digest = sha(raw)
    if resume and digest != expected:
        raise ValueError("resume binding differs from known candidate input")
    documents = parse_known_candidates(raw, source)
    path = output / "known-candidates" / (digest + ".json")
    if path.exists():
        preserved_known_candidates(output, source, digest)
    elif resume:
        raise ValueError("known candidate preserved input is missing")
    else:
        atomic_write(path, raw)
    return digest, documents


def snapshot_known_candidates(root: Path, source: str) -> dict[str, Any]:
    """Merge consistent prior discovery snapshots, including unfinished captures."""
    if source not in SOURCE_ROOTS:
        raise ValueError("known candidate snapshot source is invalid")
    documents: dict[str, dict[str, Any]] = {}
    inputs = []
    for binding_path in sorted(root.glob("*/" + source + "/capture-binding.json")):
        directory = binding_path.parent
        database = directory / "inventory.sqlite"
        if any(
            parent.is_symlink()
            for parent in [binding_path, database, directory, *directory.parents]
        ):
            raise ValueError("known candidate historical input path is unsafe")
        binding = json.loads(binding_path.read_bytes())
        if binding.get("request", {}).get("source") != source:
            raise ValueError("known candidate historical source binding differs")
        if not database.exists():
            continue
        digest_parts = [sha(binding_path.read_bytes())]
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            connection.execute("BEGIN")
            for url, raw in connection.execute("SELECT url,data FROM documents ORDER BY url"):
                item = _candidate(json.loads(raw), source)
                if item["url"] != url:
                    raise ValueError("known candidate historical URL binding differs")
                digest_parts.append(sha(json_bytes([url, raw])))
                existing = documents.get(url)
                observed = discovery_time(item["discovered_at"])
                earlier = discovery_time(existing["discovered_at"]) if existing else None
                if existing is None or (
                    observed is not None and (earlier is None or observed < earlier)
                ):
                    documents[url] = item
        inputs.append(
            {
                "queue": directory.relative_to(root).as_posix(),
                "snapshot_sha256": sha(json_bytes(digest_parts)),
            }
        )
    return {
        "schema_version": 1,
        "source": source,
        "documents": [documents[url] for url in sorted(documents)],
        "inputs": inputs,
    }
