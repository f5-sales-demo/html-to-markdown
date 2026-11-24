"""Seal private source-admission inputs and verify their complete replay chain."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS
from .content_inventory import atomic_private
from .curation import corpus_digest, json_bytes
from .enrichment.analysis import sha
from .source_provenance_chain import verify_provenance_transition
from .source_review_jobs import verify_reviews

MARKER = "source-review-receipt.json"
MAX_METADATA_BYTES = 64 * 1024 * 1024


def _metadata_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate source admission metadata key")
        result[key] = value
    return result


def _metadata_bytes(root: Path, name: str) -> bytes:
    # Resolve the same private relative paths as source-reference replay.
    from .reference_repair_inputs import safe_file  # pylint: disable=import-outside-toplevel

    path = safe_file(root, name)
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("source admission metadata exceeds size bound")
    with path.open("rb") as stream:
        data = stream.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("source admission metadata exceeds size bound")
    return data


def read_source_metadata(output: Path, digest: str) -> dict[str, Any]:
    """Ground selection metadata in captured inputs; this never grants admission."""
    raw = _metadata_bytes(output, MARKER)
    if sha(raw) != digest:
        raise ValueError("source admission receipt digest mismatch")
    receipt = json.loads(raw, object_pairs_hook=_metadata_object)
    if not isinstance(receipt, dict):
        raise ValueError("invalid source admission selection metadata")
    schema = receipt.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != 1:
        raise ValueError("invalid source admission selection metadata")
    source = receipt.get("source")
    if (
        not isinstance(source, str)
        or source not in ADAPTERS
        or not isinstance(receipt.get("documents"), list)
        or any(
            not isinstance(entry, dict) or not isinstance(entry.get("disposition"), str)
            for entry in receipt["documents"]
        )
    ):
        raise ValueError("invalid source admission selection metadata")
    ended_at = receipt.get("ended_at")
    if (
        not isinstance(ended_at, str)
        or datetime.fromisoformat(ended_at.replace("Z", "+00:00")).tzinfo is None
    ):
        raise ValueError("invalid source admission capture time")
    captured = _metadata_bytes(output, ".admission/capture/inventory.json")
    if sha(captured) != receipt["inventory_sha256"]:
        raise ValueError("source capture inventory changed")
    inventory = json.loads(captured, object_pairs_hook=_metadata_object)
    if (
        receipt["source"] != inventory["binding"]["source"]
        or receipt["capture_binding"] != inventory["binding"]
        or receipt["ended_at"] != inventory["ended_at"]
        or receipt["enumeration_complete"] != inventory["completeness"]["enumeration"]
        or receipt["capture_complete"] != inventory["completeness"]["capture"]
    ):
        raise ValueError("source admission capture metadata changed")
    return dict(receipt)


def _snapshot_file(source: str, destination: str) -> str:
    """Snapshots share immutable bytes; subsequent checkpoints replace inodes."""
    if Path(source).is_symlink():
        raise ValueError("private snapshot cannot contain symlinks")
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def seal_source(capture: Path, review: Path, output: Path) -> dict[str, Any]:
    for directory in (capture, review):
        if directory.is_symlink() or any(path.is_symlink() for path in directory.rglob("*")):
            raise ValueError("private snapshot cannot contain symlinks")
    verified = verify_reviews(capture, review)
    if output.exists():
        raise ValueError("source sealing requires a fresh directory")
    output.mkdir(parents=True, mode=0o700)
    root = output / ".admission"
    shutil.copytree(capture, root / "capture", copy_function=_snapshot_file)
    shutil.copytree(
        review,
        root / "review",
        ignore=shutil.ignore_patterns("journal", "history", "error-history", "*.private.log"),
        copy_function=_snapshot_file,
    )
    if (review / "reviewed" / "content").exists():
        shutil.copytree(
            review / "reviewed" / "content", output / "content", copy_function=_snapshot_file
        )
    inventory = json.loads((capture / "inventory.json").read_bytes())
    review_inventory = json.loads((review / "review-inventory.json").read_bytes())
    records = []
    for entry in review_inventory["documents"]:
        if "path" not in entry:
            records.append(entry)
            continue
        record = root / "review" / entry["path"]
        saved = json.loads(record.read_bytes())
        item = saved["item"]
        records.append(
            {
                "identity": item["identity"],
                "url": item["url"],
                "sourceId": item["sourceId"],
                "record": entry["path"],
                "record_sha256": sha(record.read_bytes()),
                "disposition": saved["disposition"],
                "reason": saved["reason"],
                "captured_at": item.get("captured_at"),
                "article_sha256": item["article_sha256"],
                "source_html_sha256": item["source_html_sha256"],
                "published_path": str(
                    Path(item.get("published_path", item["article_path"])).relative_to("articles")
                ),
            }
        )
    receipt = {
        "schema_version": 1,
        "source": inventory["binding"]["source"],
        "capture_binding": inventory["binding"],
        "inventory_sha256": sha((capture / "inventory.json").read_bytes()),
        "review_inventory_sha256": verified["inventory_sha256"],
        "documents": records,
        "accepted_files": verified["accepted_files"],
        "content_sha256": corpus_digest(output),
        "enumeration_complete": inventory["completeness"]["enumeration"],
        "capture_complete": inventory["completeness"]["capture"],
        "review_complete": all(
            entry.get("disposition") in {"accepted", "policy_excluded"} for entry in records
        ),
        "ended_at": inventory["ended_at"],
    }
    if "review_compatibility" in verified:
        receipt["review_compatibility"] = verified["review_compatibility"]
    atomic_private(output / MARKER, json_bytes(receipt))
    verify_source(output, sha((output / MARKER).read_bytes()))
    return receipt


def verify_source(output: Path, digest: str) -> dict[str, Any]:
    receipt = read_source_metadata(output, digest)
    root = output / ".admission"
    verified = verify_reviews(root / "capture", root / "review")
    contexts: set[str] = set()
    if (
        verified["inventory_sha256"] != receipt["review_inventory_sha256"]
        or verified["accepted_files"] != receipt["accepted_files"]
    ):
        raise ValueError("source independent review inventory changed")
    for entry in receipt["documents"]:
        if "record" not in entry:
            continue
        path = root / "review" / entry["record"]
        if sha(path.read_bytes()) != entry["record_sha256"]:
            raise ValueError("source admission decision changed")
        record = json.loads(path.read_bytes())
        if record["disposition"] in {"accepted", "policy_excluded"}:
            contexts.add(record["binding"]["request_context_sha256"])
        if (
            record["disposition"] != entry["disposition"]
            or record["item"]["identity"] != entry["identity"]
        ):
            raise ValueError("source admission disposition changed")
    actual = {
        path.relative_to(output).as_posix(): sha(path.read_bytes())
        for path in (output / "content").rglob("*")
        if path.is_file()
    }
    if actual != verified["accepted_files"] or corpus_digest(output) != receipt["content_sha256"]:
        raise ValueError("source admitted content or media changed")
    transition = {}
    if "review_compatibility" in receipt:
        transition = verify_provenance_transition(
            receipt["review_compatibility"], verified.get("review_compatibility"), contexts, digest
        )
    return {
        **receipt,
        **({"review_compatibility_transition": transition} if transition else {}),
        **(
            {"review_compatibility": verified["review_compatibility"]}
            if "review_compatibility" in verified
            else {}
        ),
    }
